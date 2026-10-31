"""阈值在线调整与变更审计测试（M7-8 / M7-14）。

覆盖四层：
- store：risk_overrides 追加、取生效行、撤销、分页查询
- RiskOverrideService：校验、Redis/审计双写、审计失败回滚、清除
- worker：心跳重读覆盖层，不重启即生效；Redis 读失败保留上一轮值
- API：读接口、写接口守卫、非法值 400、删除回落
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest

from quant.dashboard.app import create_app
from quant.dashboard.service import DashboardService
from quant.execution.executor import Executor
from quant.execution.worker import Worker
from quant.foundation.config import OKXSettings, Settings
from quant.queue.signal_queue import SignalQueue
from quant.risk.checker import RiskChecker
from quant.risk.config import RiskConfig
from quant.risk.limits import RiskLimits, resolve_limits
from quant.risk.overrides import OverrideError, RiskOverrideService
from quant.state.sqlite_store import SQLiteStore

STRATEGY = "sma_cross"


def _redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p"),
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "data" / "state" / "quant.db"},
    )


@pytest.fixture
def env(tmp_path):
    store = SQLiteStore(tmp_path / "quant.db")
    redis = _redis()
    yield store, redis, RiskOverrideService(store, redis)
    store.close()


class TestStore:
    async def test_insert_and_read_active(self, env) -> None:
        store, _, _ = env

        override_id = store.insert_risk_override(
            STRATEGY,
            "max_order_qty",
            "5000",
            changed_at=1000,
            changed_by="owner",
            reason="测试",
            old_value=None,
        )

        assert override_id > 0
        active = store.get_active_risk_override(STRATEGY, "max_order_qty")
        assert active is not None
        assert active["new_value"] == "5000"
        assert active["changed_by"] == "owner"

    async def test_latest_row_wins(self, env) -> None:
        store, _, _ = env
        store.insert_risk_override(
            STRATEGY, "max_total_pct", "0.6", changed_at=1000,
            changed_by="owner",
        )
        store.insert_risk_override(
            STRATEGY, "max_total_pct", "0.5", changed_at=2000,
            changed_by="owner", old_value="0.6",
        )

        active = store.get_active_risk_override(STRATEGY, "max_total_pct")

        assert active is not None and active["new_value"] == "0.5"

    async def test_revoke_clears_active(self, env) -> None:
        store, _, _ = env
        store.insert_risk_override(
            STRATEGY, "max_order_qty", "5000", changed_at=1000,
            changed_by="owner",
        )

        revoked = store.revoke_risk_overrides(
            STRATEGY, "max_order_qty", revoked_at=2000, revoked_by="owner"
        )

        assert revoked == 1
        assert store.get_active_risk_override(STRATEGY, "max_order_qty") is None
        # 撤销不删行：审计要能查到「曾经改过什么」
        rows, total = store.list_risk_overrides(strategy_id=STRATEGY)
        assert total == 1
        assert rows[0]["revoked_at"] == 2000

    async def test_list_filters_revoked(self, env) -> None:
        store, _, _ = env
        store.insert_risk_override(
            STRATEGY, "max_order_qty", "5000", changed_at=1000,
            changed_by="owner",
        )
        store.insert_risk_override(
            "other", "max_order_qty", "6000", changed_at=2000,
            changed_by="owner",
        )

        active, total = store.list_risk_overrides(
            include_revoked=False
        )
        scoped, scoped_total = store.list_risk_overrides(
            strategy_id=STRATEGY
        )

        assert total == 2 and len(active) == 2
        assert scoped_total == 1 and scoped[0]["strategy_id"] == STRATEGY


class TestServiceSet:
    async def test_set_writes_redis_and_audit(self, env) -> None:
        store, redis, service = env

        result = await service.set(
            STRATEGY, "max_order_qty", "5000", reason="降杠杆", by="owner"
        )

        assert result["new_value"] == "5000"
        assert result["old_value"] is None
        assert await redis.hget(service._key, f"{STRATEGY}:max_order_qty") == (
            "5000"
        )
        rows, total = store.list_risk_overrides(strategy_id=STRATEGY)
        assert total == 1
        assert rows[0]["reason"] == "降杠杆"
        assert rows[0]["changed_by"] == "owner"

    async def test_set_records_previous_value(self, env) -> None:
        _, _, service = env
        await service.set(
            STRATEGY, "max_order_qty", "5000", reason="第一次", by="owner"
        )

        result = await service.set(
            STRATEGY, "max_order_qty", "4000", reason="再收紧", by="owner"
        )

        assert result["old_value"] == "5000"
        assert await service.effective(STRATEGY) == {"max_order_qty": "4000"}

    async def test_unknown_param_rejected(self, env) -> None:
        _, _, service = env
        with pytest.raises(OverrideError, match="unknown risk param"):
            await service.set(STRATEGY, "max_leverage", "2", reason="x")

    async def test_reason_required(self, env) -> None:
        _, _, service = env
        with pytest.raises(OverrideError, match="reason is required"):
            await service.set(STRATEGY, "max_order_qty", "5000", reason="  ")

    async def test_pct_above_one_rejected(self, env) -> None:
        _, _, service = env
        with pytest.raises(OverrideError, match=r"within \(0, 1\]"):
            await service.set(STRATEGY, "max_total_pct", "1.5", reason="x")

    async def test_non_positive_rejected(self, env) -> None:
        _, _, service = env
        with pytest.raises(OverrideError, match="positive"):
            await service.set(STRATEGY, "daily_loss_limit", "0", reason="x")

    async def test_int_field_requires_int(self, env) -> None:
        _, _, service = env
        with pytest.raises(OverrideError, match="integer"):
            await service.set(
                STRATEGY, "consecutive_loss_limit", "2.5", reason="x"
            )

    async def test_audit_failure_rolls_back_redis(self, env, monkeypatch) -> None:
        store, redis, service = env

        def boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(store, "insert_risk_override", boom)

        with pytest.raises(RuntimeError):
            await service.set(
                STRATEGY, "max_order_qty", "5000", reason="x", by="owner"
            )

        # 审计没落库，覆盖值就不能生效——否则「谁改的」无从查起
        assert await redis.hget(service._key, f"{STRATEGY}:max_order_qty") is None


class TestServiceClear:
    async def test_clear_removes_redis_and_revokes(self, env) -> None:
        store, redis, service = env
        await service.set(
            STRATEGY, "max_order_qty", "5000", reason="x", by="owner"
        )

        result = await service.clear(STRATEGY, "max_order_qty", by="owner")

        assert result["cleared"] is True
        assert await service.effective(STRATEGY) == {}
        assert store.get_active_risk_override(STRATEGY, "max_order_qty") is None

    async def test_clear_without_override_is_noop(self, env) -> None:
        _, _, service = env

        result = await service.clear(STRATEGY, "max_order_qty")

        assert result["cleared"] is False
        assert result["reason"] == "no active override"

    async def test_clear_unknown_param_rejected(self, env) -> None:
        _, _, service = env
        with pytest.raises(OverrideError, match="unknown risk param"):
            await service.clear(STRATEGY, "nope")

    async def test_list_returns_effective_and_history(self, env) -> None:
        _, _, service = env
        await service.set(
            STRATEGY, "max_order_qty", "5000", reason="x", by="owner"
        )

        data = await service.list(strategy_id=STRATEGY)

        assert data["effective"] == {"max_order_qty": "5000"}
        assert data["total"] == 1


def _base_limits() -> RiskLimits:
    return resolve_limits(STRATEGY)


def _worker(store, redis, *, overrides_key="quant:risk:overrides") -> Worker:
    base = _base_limits()
    return Worker(
        queue=SignalQueue(redis),
        risk=RiskChecker(
            RiskConfig(
                strategy_id=STRATEGY,
                max_position_pct=base.max_position_pct,
                max_total_pct=base.max_total_pct,
                max_order_qty=base.max_order_qty,
                daily_loss_limit=base.daily_loss_limit,
                consecutive_loss_limit=base.consecutive_loss_limit,
            ),
            store,
        ),
        executor=Executor(),
        store=store,
        redis=redis,
        overrides_key=overrides_key,
        base_limits=base,
    )


class TestWorkerRefresh:
    async def test_override_takes_effect_without_restart(self, env) -> None:
        store, redis, service = env
        worker = _worker(store, redis)

        await service.set(
            STRATEGY, "max_order_qty", "5000", reason="降杠杆", by="owner"
        )
        await worker._refresh_limits()

        assert worker.risk.config.max_order_qty == Decimal("5000")
        assert worker.risk_limits["rules"]["max_order_qty"] == "5000"
        assert worker.risk_limits["sources"]["max_order_qty"] == "override"

    async def test_clear_restores_config_value(self, env) -> None:
        store, redis, service = env
        worker = _worker(store, redis)
        await service.set(
            STRATEGY, "max_order_qty", "5000", reason="降杠杆", by="owner"
        )
        await worker._refresh_limits()

        await service.clear(STRATEGY, "max_order_qty", by="owner")
        await worker._refresh_limits()

        assert worker.risk.config.max_order_qty == _base_limits().max_order_qty
        assert worker.risk_limits["sources"]["max_order_qty"] != "override"

    async def test_no_override_keeps_base(self, env) -> None:
        store, redis, _ = env
        worker = _worker(store, redis)

        await worker._refresh_limits()

        assert worker.risk.config.max_order_qty == _base_limits().max_order_qty

    async def test_redis_failure_keeps_last_value(self, env, monkeypatch) -> None:
        store, redis, service = env
        worker = _worker(store, redis)
        await service.set(
            STRATEGY, "max_order_qty", "5000", reason="降杠杆", by="owner"
        )
        await worker._refresh_limits()

        async def broken(*args, **kwargs):
            raise RuntimeError("redis down")

        monkeypatch.setattr(redis, "hgetall", broken)
        await worker._refresh_limits()

        # 抖动时保留上一轮的人工决定，不静默退回配置值
        assert worker.risk.config.max_order_qty == Decimal("5000")


class TestApi:
    LOCAL = {"host": "127.0.0.1:8080", "X-Dashboard-Request": "1"}

    async def _client(self, tmp_path):
        store = SQLiteStore(tmp_path / "quant.db")
        redis = _redis()
        service = DashboardService(store, redis, _settings(tmp_path))
        app = create_app(service, tmp_path / "missing-dist")
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        return store, client

    async def test_get_and_post(self, tmp_path) -> None:
        store, client = await self._client(tmp_path)
        try:
            async with client:
                created = await client.post(
                    "/api/risk/overrides",
                    headers=self.LOCAL,
                    json={
                        "strategy_id": STRATEGY,
                        "param": "max_order_qty",
                        "value": "5000",
                        "reason": "测试",
                    },
                )
                assert created.status_code == 200
                assert created.json()["new_value"] == "5000"

                listed = await client.get(
                    "/api/risk/overrides",
                    params={"strategy_id": STRATEGY},
                )
                body = listed.json()
                assert body["effective"] == {"max_order_qty": "5000"}
                assert body["total"] == 1
        finally:
            store.close()

    async def test_write_requires_guard(self, tmp_path) -> None:
        store, client = await self._client(tmp_path)
        try:
            async with client:
                denied = await client.post(
                    "/api/risk/overrides",
                    headers={"host": "127.0.0.1:8080"},
                    json={
                        "strategy_id": STRATEGY,
                        "param": "max_order_qty",
                        "value": "5000",
                        "reason": "测试",
                    },
                )
                assert denied.status_code == 400
        finally:
            store.close()

    async def test_invalid_value_returns_400(self, tmp_path) -> None:
        store, client = await self._client(tmp_path)
        try:
            async with client:
                bad = await client.post(
                    "/api/risk/overrides",
                    headers=self.LOCAL,
                    json={
                        "strategy_id": STRATEGY,
                        "param": "max_total_pct",
                        "value": "1.5",
                        "reason": "测试",
                    },
                )
                assert bad.status_code == 400
                assert "within (0, 1]" in bad.json()["detail"]
        finally:
            store.close()

    async def test_delete_clears(self, tmp_path) -> None:
        store, client = await self._client(tmp_path)
        try:
            async with client:
                await client.post(
                    "/api/risk/overrides",
                    headers=self.LOCAL,
                    json={
                        "strategy_id": STRATEGY,
                        "param": "max_order_qty",
                        "value": "5000",
                        "reason": "测试",
                    },
                )
                cleared = await client.delete(
                    f"/api/risk/overrides/{STRATEGY}/max_order_qty",
                    headers=self.LOCAL,
                )
                assert cleared.status_code == 200
                assert cleared.json()["cleared"] is True
                assert store.get_active_risk_override(
                    STRATEGY, "max_order_qty"
                ) is None
        finally:
            store.close()
