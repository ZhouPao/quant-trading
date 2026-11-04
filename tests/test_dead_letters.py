"""死信查看与重放测试（MS4-B §10）。

覆盖：
- `dead_letters` 迁移与 store 读写、原子占用/归还
- SignalQueue 双写（Redis 计数 + SQLite 权威记录）
- DeadLetterService 重放幂等：requeued / already_processed / already_replayed
- 看板 API：列表筛选、单条重放、批量重放、丢弃与写操作守卫
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import fakeredis.aioredis
import httpx
import pytest

from quant.backtest.signal import Signal
from quant.dashboard.app import create_app
from quant.dashboard.service import DashboardService
from quant.execution.dead_letter import (
    ALREADY_PROCESSED,
    ALREADY_REPLAYED,
    DISCARDED,
    FAILED,
    NOT_FOUND,
    NOT_REPLAYABLE,
    REQUEUED,
    DeadLetterService,
)
from quant.foundation.config import OKXSettings, Settings
from quant.queue.signal_queue import SignalQueue
from quant.state.sqlite_store import SCHEMA_VERSION, SQLiteStore

NOW = 1_700_000_000_000


def _settings(tmp_path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage={"data_dir": tmp_path / "data"},
    )


def _signal(signal_id: str = "sig-1") -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side="buy",
        order_type="market",
        inst_id="BTC-USDT",
        qty=Decimal("0.01"),
        ts=NOW,
        signal_id=signal_id,
    )


def _insert(
    store: SQLiteStore,
    dead_id: str,
    *,
    stage: str = "submit",
    signal: Signal | None = None,
    created_at: int = NOW,
) -> None:
    payload = (
        {"signal": signal.to_json(), "reason": "boom"}
        if signal is not None
        else {"raw": "not json", "reason": "parse boom"}
    )
    store.insert_dead_letter(
        dead_id,
        stage,
        payload["reason"],
        json.dumps(payload),
        created_at=created_at,
        signal_id=signal.signal_id if signal else None,
    )


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def store(tmp_path):
    db = SQLiteStore(tmp_path / "quant.db")
    yield db
    db.close()


@pytest.fixture
def service(store, redis, tmp_path):
    return DashboardService(store, redis, _settings(tmp_path))


# =====================================================================
# 存储
# =====================================================================


class TestDeadLetterStore:
    def test_schema_version_bumped(self) -> None:
        # v1 基线 + 10 段迁移 = 11；对账差异表是 v8，资金费率成本列是 v9，
        # optimize_jobs 表（M4-13 寻优进度落库）是 v10，
        # app_meta 表（绑定初始化标记，P0 全量解绑复活修复）是 v11
        assert SCHEMA_VERSION == 11

    def test_insert_and_get(self, store) -> None:
        _insert(store, "d-1", signal=_signal())

        row = store.get_dead_letter("d-1")

        assert row is not None
        assert row["stage"] == "submit"
        assert row["signal_id"] == "sig-1"
        assert row["replayed_at"] is None
        assert store.get_dead_letter("missing") is None

    def test_duplicate_insert_is_idempotent(self, store) -> None:
        _insert(store, "d-1")
        again = store.insert_dead_letter(
            "d-1", "parse", "other", "{}", created_at=NOW
        )

        assert again is False
        assert store.get_dead_letter("d-1")["stage"] == "submit"

    def test_list_filters(self, store) -> None:
        _insert(store, "d-1", stage="parse", created_at=NOW)
        _insert(store, "d-2", stage="submit", signal=_signal(), created_at=NOW + 1)
        store.claim_dead_letter("d-2", NOW + 2)
        store.finish_dead_letter("d-2", REQUEUED)

        everything, total = store.list_dead_letters(10)
        assert total == 2
        assert [r["dead_id"] for r in everything] == ["d-2", "d-1"]

        submits, sub_total = store.list_dead_letters(10, stage="submit")
        assert sub_total == 1
        assert submits[0]["replay_result"] == REQUEUED

        pending, pending_total = store.list_dead_letters(10, only_pending=True)
        assert pending_total == 1
        assert pending[0]["dead_id"] == "d-1"

        recent, recent_total = store.list_dead_letters(10, since=NOW + 1)
        assert recent_total == 1

    def test_claim_is_atomic(self, store) -> None:
        _insert(store, "d-1")

        assert store.claim_dead_letter("d-1", NOW) is True
        assert store.claim_dead_letter("d-1", NOW) is False

        store.release_dead_letter("d-1")

        assert store.get_dead_letter("d-1")["replayed_at"] is None
        assert store.claim_dead_letter("d-1", NOW) is True

    def test_release_only_affects_claimed(self, store) -> None:
        _insert(store, "d-1")
        store.claim_dead_letter("d-1", NOW)
        store.finish_dead_letter("d-1", REQUEUED)

        store.release_dead_letter("d-1")

        row = store.get_dead_letter("d-1")
        assert row["replay_result"] == REQUEUED
        assert row["replayed_at"] is not None


# =====================================================================
# SignalQueue 双写
# =====================================================================


class TestSignalQueueDualWrite:
    async def test_push_dead_writes_both_sides(self, redis, store) -> None:
        queue = SignalQueue(redis, store=store)

        await queue.push_dead(_signal(), "place failed")

        assert await queue.dead_length() == 1
        items, total = store.list_dead_letters(10)
        assert total == 1
        assert items[0]["stage"] == "submit"
        assert items[0]["signal_id"] == "sig-1"
        assert items[0]["reason"] == "place failed"

    async def test_push_dead_raw_defaults_to_parse(self, redis, store) -> None:
        queue = SignalQueue(redis, store=store)

        await queue.push_dead_raw("{bad body", "parse error")

        items, _ = store.list_dead_letters(10)
        assert items[0]["stage"] == "parse"
        assert items[0]["signal_id"] is None

    async def test_no_store_keeps_legacy_behaviour(self, redis) -> None:
        queue = SignalQueue(redis)

        await queue.push_dead(_signal(), "x")

        assert await queue.dead_length() == 1

    async def test_store_failure_does_not_lose_dead_letter(
        self, redis
    ) -> None:
        broken = MagicMock()
        broken.insert_dead_letter.side_effect = RuntimeError("db down")
        queue = SignalQueue(redis, store=broken)

        await queue.push_dead(_signal(), "x")

        assert await queue.dead_length() == 1


# =====================================================================
# 重放
# =====================================================================


class _FailingQueue:
    async def push(self, _signal) -> None:
        raise RuntimeError("redis down")


class TestReplay:
    async def test_requeues_and_marks(self, redis, store) -> None:
        _insert(store, "d-1", signal=_signal())
        queue = SignalQueue(redis, store=store)
        svc = DeadLetterService(store, queue)

        result = await svc.replay("d-1")

        assert result["result"] == REQUEUED
        assert await queue.pending_length() == 1
        row = store.get_dead_letter("d-1")
        assert row["replay_result"] == REQUEUED
        assert row["replayed_at"] is not None

    async def test_second_replay_does_not_duplicate(self, redis, store) -> None:
        _insert(store, "d-1", signal=_signal())
        queue = SignalQueue(redis, store=store)
        svc = DeadLetterService(store, queue)

        await svc.replay("d-1")
        again = await svc.replay("d-1")

        assert again["result"] == REQUEUED
        assert await queue.pending_length() == 1

    async def test_inflight_claim_returns_already_replayed(
        self, redis, store
    ) -> None:
        _insert(store, "d-1", signal=_signal())
        store.claim_dead_letter("d-1", NOW)  # 模拟另一次重放正在入队
        svc = DeadLetterService(store, SignalQueue(redis, store=store))

        result = await svc.replay("d-1")

        assert result["result"] == ALREADY_REPLAYED

    @pytest.mark.parametrize("status", ["executed", "rejected"])
    async def test_settled_signal_is_not_requeued(
        self, redis, store, status
    ) -> None:
        """worker 只写 executed / rejected；取值写错会让幂等永远失效。"""
        signal = _signal("sig-done")
        store.save_signal(signal, status, now_ms=NOW)
        _insert(store, "d-1", signal=signal)
        queue = SignalQueue(redis, store=store)
        svc = DeadLetterService(store, queue)

        result = await svc.replay("d-1")

        assert result["result"] == ALREADY_PROCESSED
        assert await queue.pending_length() == 0
        assert store.get_dead_letter("d-1")["replay_result"] == ALREADY_PROCESSED

    async def test_parse_dead_letter_is_not_replayable(
        self, redis, store
    ) -> None:
        _insert(store, "d-1", stage="parse")
        queue = SignalQueue(redis, store=store)
        svc = DeadLetterService(store, queue)

        result = await svc.replay("d-1")

        assert result["result"] == NOT_REPLAYABLE
        assert await queue.pending_length() == 0
        assert store.get_dead_letter("d-1")["replayed_at"] is None

    async def test_missing_id(self, redis, store) -> None:
        svc = DeadLetterService(store, SignalQueue(redis, store=store))

        assert (await svc.replay("nope"))["result"] == NOT_FOUND

    async def test_enqueue_failure_releases_claim(self, store) -> None:
        _insert(store, "d-1", signal=_signal())
        svc = DeadLetterService(store, _FailingQueue())

        result = await svc.replay("d-1")

        assert result["result"] == FAILED
        assert store.get_dead_letter("d-1")["replayed_at"] is None

    async def test_discard_marks_and_stays(self, redis, store) -> None:
        _insert(store, "d-1", signal=_signal())
        svc = DeadLetterService(store, SignalQueue(redis, store=store))

        result = await svc.discard("d-1")

        assert result["result"] == DISCARDED
        row = store.get_dead_letter("d-1")
        assert row["replay_result"] == DISCARDED
        assert row["replayed_at"] is not None

    async def test_replay_many_is_independent(self, redis, store) -> None:
        _insert(store, "d-1", signal=_signal("s-1"))
        _insert(store, "d-2", stage="parse")
        queue = SignalQueue(redis, store=store)
        svc = DeadLetterService(store, queue)

        results = await svc.replay_many(["d-1", "d-2", "missing"])

        assert [r["result"] for r in results] == [
            REQUEUED,
            NOT_REPLAYABLE,
            NOT_FOUND,
        ]
        assert await queue.pending_length() == 1


# =====================================================================
# 看板 API
# =====================================================================


class TestDeadLetterApi:
    LOCAL = {"host": "127.0.0.1:8080", "X-Dashboard-Request": "1"}

    async def test_list_and_filter(self, service, store) -> None:
        store.insert_dead_letter(
            "d-1", "submit", "boom", json.dumps({"signal": {}}), created_at=NOW
        )
        store.insert_dead_letter(
            "d-2", "parse", "bad", json.dumps({"raw": "x"}), created_at=NOW + 1
        )
        app = create_app(service)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/dead-letters?stage=parse")
            pending = await client.get("/api/dead-letters?pending_only=true")

        body = resp.json()
        assert resp.status_code == 200
        assert body["total"] == 1
        assert body["items"][0]["dead_id"] == "d-2"
        assert body["items"][0]["payload"] == json.dumps({"raw": "x"})
        assert body["items"][0]["payload_truncated"] is False
        assert pending.json()["total"] == 2

    async def test_replay_requires_guard(self, service, store) -> None:
        _insert(store, "d-1", signal=_signal())
        app = create_app(service)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.post(
                "/api/dead-letters/d-1/replay",
                headers={"host": "127.0.0.1:8080"},
            )
            ok = await client.post(
                "/api/dead-letters/d-1/replay", headers=self.LOCAL
            )

        assert denied.status_code == 400
        assert ok.json()["result"] == REQUEUED
        assert store.get_dead_letter("d-1")["replay_result"] == REQUEUED

    async def test_batch_replay_and_discard(self, service, store, redis) -> None:
        _insert(store, "d-1", signal=_signal("s-1"))
        _insert(store, "d-2", stage="parse")
        _insert(store, "d-3", signal=_signal("s-3"))
        app = create_app(service)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            batch = await client.post(
                "/api/dead-letters/replay",
                json={"ids": ["d-1", "d-2"]},
                headers=self.LOCAL,
            )
            discarded = await client.post(
                "/api/dead-letters/d-3/discard", headers=self.LOCAL
            )

        assert [r["result"] for r in batch.json()["results"]] == [
            REQUEUED,
            NOT_REPLAYABLE,
        ]
        assert discarded.json()["result"] == DISCARDED
        assert await redis.llen("quant:signals") == 1

    async def test_payload_is_truncated(self, service, store) -> None:
        store.insert_dead_letter(
            "d-1", "submit", "boom", "x" * 5000, created_at=NOW
        )
        app = create_app(service)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/dead-letters")

        item = resp.json()["items"][0]
        assert item["payload_truncated"] is True
        assert len(item["payload"]) == 4000
