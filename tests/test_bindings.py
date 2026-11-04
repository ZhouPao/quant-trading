"""启停矩阵测试（MS4-C §4 / §10.3 / §11.1）。

覆盖：
- store 绑定 CRUD：主键 (strategy_id, inst_id)，bar 不参与唯一性
- 首次启动 seed 初始绑定
- 读取优先级：Redis 缓存 → SQLite 回落 → 重建缓存；halt 模式与回滚开关
- 引擎多绑定：每组合独立水位/状态键，停用项不评估
- API：GET/PUT 绑定，周期冲突拒绝，写入后重建缓存
"""
from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest

from quant.dashboard.app import create_app
from quant.dashboard.backtest import BacktestService, BindingUpdateRequest, StrategyParamError
from quant.dashboard.service import DashboardService
from quant.engine.live import LiveSignalEngine
from quant.foundation.config import OKXSettings, Settings
from quant.models import Bar
from quant.queue.signal_queue import SignalQueue
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage
from quant.strategies.bindings import (
    Binding,
    BindingsUnavailable,
    ensure_seed,
    read_bindings,
    write_cache,
)

HOUR = 3_600_000
LAST_TS = (1_700_000_000_000 // HOUR) * HOUR
NOW = LAST_TS + HOUR
INST = "BTC-USDT"
LOCAL_WRITE = {"host": "127.0.0.1:8080", "X-Dashboard-Request": "1"}
STRATEGY_TOML = (
    "[strategy]\n"
    'strategy_id = "sma_cross"\n'
    'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n\n'
    "[params]\nfast_len = 10\nslow_len = 50\n"
)


def _bars(count: int = 60, last_close: str = "120", inst: str = INST) -> list[Bar]:
    """前 count-1 根横盘 100，最后一根跳升（制造金叉）。"""
    out = []
    for i in range(count):
        price = Decimal(last_close) if i == count - 1 else Decimal("100")
        out.append(
            Bar(
                ts=LAST_TS - (count - 1 - i) * HOUR,
                open=price,
                high=price,
                low=price,
                close=price,
                vol=Decimal("10"),
                vol_quote=price * 10,
                confirm=1,
                inst_id=inst,
                bar_size="1H",
            )
        )
    return out


def _settings(tmp_path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p"),
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "quant.db"},
        backtest={"window_size": 200},
        engine={
            "strategy": "sma_cross",
            "inst": INST,
            "bar": "1H",
            "warmup_bars": 200,
            "config_dir": tmp_path / "strategies",
            "cash_fallback": "5000",
        },
        monitor={"dashboard_snapshot_key": "quant:dashboard:snapshot"},
    )


def _write_strategy(tmp_path) -> None:
    cfg = tmp_path / "strategies"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "sma_cross.toml").write_text(STRATEGY_TOML)


@pytest.fixture
def env(tmp_path):
    settings = _settings(tmp_path)
    _write_strategy(tmp_path)
    BarStorage(settings.storage.data_dir).save_bars(_bars())
    store = SQLiteStore(settings.state.db_path)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield settings, store, redis
    store.close()


def _engine(settings, store, redis) -> LiveSignalEngine:
    return LiveSignalEngine(
        settings,
        store,
        redis,
        queue=SignalQueue(
            redis,
            pending_key=settings.redis.queue_key,
            inflight_key=settings.redis.inflight_key,
            dead_key=settings.redis.dead_key,
        ),
    )


class TestStore:
    def test_upsert_and_list(self, env) -> None:
        _settings_, store, _redis = env

        store.upsert_binding("sma_cross", INST, "1H", True, NOW)
        store.upsert_binding("sma_cross", "ETH-USDT", "4H", False, NOW)

        rows = store.list_bindings("sma_cross")
        assert [(r["inst_id"], r["bar"], r["enabled"]) for r in rows] == [
            ("BTC-USDT", "1H", 1),
            ("ETH-USDT", "4H", 0),
        ]
        assert store.get_binding("sma_cross", "ETH-USDT")["enabled"] == 0

    def test_same_key_updates_in_place(self, env) -> None:
        """主键不含 bar：同策略同品种再写一次是更新，不是新增行。"""
        _settings_, store, _redis = env

        store.upsert_binding("sma_cross", INST, "1H", True, NOW)
        store.upsert_binding("sma_cross", INST, "1H", False, NOW + 1)

        rows = store.list_bindings()
        assert len(rows) == 1
        assert rows[0]["enabled"] == 0

    def test_delete_binding(self, env) -> None:
        _settings_, store, _redis = env
        store.upsert_binding("sma_cross", INST, "1H", True, NOW)

        assert store.delete_binding("sma_cross", INST) is True
        assert store.delete_binding("sma_cross", INST) is False
        assert store.list_bindings() == []


class TestSeed:
    def test_seeds_initial_binding_once(self, env) -> None:
        settings, store, _redis = env

        assert ensure_seed(store, settings, NOW) is True
        assert ensure_seed(store, settings, NOW) is False

        rows = store.list_bindings()
        assert [(r["strategy_id"], r["inst_id"], r["bar"]) for r in rows] == [
            ("sma_cross", INST, "1H")
        ]

    def test_seed_skipped_when_bindings_exist(self, env) -> None:
        settings, store, _redis = env
        store.upsert_binding("rsi", "ETH-USDT", "1H", True, NOW)

        assert ensure_seed(store, settings, NOW) is False
        assert [r["strategy_id"] for r in store.list_bindings()] == ["rsi"]


class TestReadBindings:
    async def test_cache_hit(self, env) -> None:
        settings, store, redis = env
        await write_cache(
            redis,
            settings.engine.bindings_key,
            [Binding("rsi", "ETH-USDT", "4H", True)],
        )

        bindings = await read_bindings(redis, store, settings)

        assert bindings == [Binding("rsi", "ETH-USDT", "4H", True)]
        # 缓存命中时不写库
        assert store.list_bindings() == []

    async def test_falls_back_to_db_and_rebuilds_cache(self, env) -> None:
        settings, store, redis = env
        store.upsert_binding("sma_cross", INST, "1H", True, NOW)
        store.upsert_binding("sma_cross", "ETH-USDT", "4H", False, NOW)

        bindings = await read_bindings(redis, store, settings)

        assert len(bindings) == 2
        cached = await redis.hgetall(settings.engine.bindings_key)
        # 禁用项也要写进缓存，否则「全部禁用」会被误判成缓存丢失
        assert set(cached) == {"sma_cross:BTC-USDT", "sma_cross:ETH-USDT"}

    async def test_empty_db_seeds_initial_binding(self, env) -> None:
        settings, store, redis = env

        bindings = await read_bindings(redis, store, settings)

        assert bindings == [Binding("sma_cross", INST, "1H", True)]
        assert store.list_bindings()

    async def test_halt_mode_raises_on_cache_miss(self, env) -> None:
        settings, store, redis = env
        settings.engine.bindings_fail_mode = "halt"

        with pytest.raises(BindingsUnavailable):
            await read_bindings(redis, store, settings)

    async def test_rollback_switch_ignores_cache_and_db(self, env) -> None:
        settings, store, redis = env
        store.upsert_binding("rsi", "ETH-USDT", "4H", True, NOW)
        settings.engine.multi_binding = False

        bindings = await read_bindings(redis, store, settings)

        assert bindings == [Binding("sma_cross", INST, "1H", True)]

    async def test_strategy_filter(self, env) -> None:
        settings, store, redis = env
        store.upsert_binding("sma_cross", INST, "1H", True, NOW)
        store.upsert_binding("rsi", "ETH-USDT", "4H", True, NOW)

        bindings = await read_bindings(redis, store, settings, strategy_id="rsi")

        assert [b.strategy_id for b in bindings] == ["rsi"]


class TestEngineMultiBinding:
    async def test_runs_each_binding_with_own_keys(self, env) -> None:
        settings, store, redis = env
        # ETH-USDT 没有本地 K 线 → waiting_data；BTC-USDT 应产出信号
        store.upsert_binding("sma_cross", INST, "1H", True, NOW)
        store.upsert_binding("sma_cross", "ETH-USDT", "1H", True, NOW)
        engine = _engine(settings, store, redis)

        results = await engine.run_all(now_ms=NOW)

        assert [b.inst_id for b, _tick in results] == [INST, "ETH-USDT"]
        ticks = {b.inst_id: tick for b, tick in results}
        assert ticks[INST].status == "signal"
        assert ticks["ETH-USDT"].status == "waiting_data"

        # 水位按绑定分键，互不覆盖
        assert await redis.get(f"quant:engine:last-bar:sma_cross:{INST}") == str(
            LAST_TS
        )
        assert await redis.get("quant:engine:last-bar:sma_cross:ETH-USDT") is None

    async def test_disabled_binding_not_evaluated(self, env) -> None:
        settings, store, redis = env
        store.upsert_binding("sma_cross", INST, "1H", True, NOW)
        store.upsert_binding("sma_cross", "ETH-USDT", "1H", False, NOW)
        engine = _engine(settings, store, redis)

        results = await engine.run_all(now_ms=NOW)

        assert [b.inst_id for b, _tick in results] == [INST]

    async def test_publishes_status_per_binding(self, env) -> None:
        settings, store, redis = env
        store.upsert_binding("sma_cross", INST, "1H", True, NOW)
        store.upsert_binding("sma_cross", "ETH-USDT", "1H", True, NOW)
        engine = _engine(settings, store, redis)

        for binding, tick in await engine.run_all(now_ms=NOW):
            await engine.publish_status(tick, binding)

        btc = await redis.get(f"quant:engine:status:sma_cross:{INST}")
        eth = await redis.get("quant:engine:status:sma_cross:ETH-USDT")
        assert btc and eth
        assert engine.status_key == f"quant:engine:status:sma_cross:{INST}"

    async def test_no_enabled_binding_skips(self, env) -> None:
        settings, store, redis = env
        store.upsert_binding("sma_cross", INST, "1H", False, NOW)
        engine = _engine(settings, store, redis)

        tick = await engine.run_once(now_ms=NOW)

        assert tick.status == "skipped"
        assert tick.reason == "no enabled bindings"

    async def test_halt_mode_activates_kill_switch(self, env) -> None:
        settings, store, redis = env
        settings.engine.bindings_fail_mode = "halt"
        engine = _engine(settings, store, redis)

        results = await engine.run_all(now_ms=NOW)

        assert results == []
        assert await redis.exists("quant:kill-switch") == 1


@pytest.fixture
def api(tmp_path):
    settings = _settings(tmp_path)
    _write_strategy(tmp_path)
    BarStorage(settings.storage.data_dir).save_bars(_bars())
    store = SQLiteStore(settings.state.db_path)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = create_app(DashboardService(store, redis, settings), tmp_path / "dist")
    yield app, store, redis, settings
    store.close()


class TestApi:
    async def test_get_seeds_initial_binding(self, api) -> None:
        app, _store, _redis, _settings_ = api
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/strategies/sma_cross/bindings")

        assert response.status_code == 200
        body = response.json()
        assert body["default_bar"] == "1H"
        assert [(b["inst_id"], b["bar"], b["enabled"]) for b in body["bindings"]] == [
            (INST, "1H", True)
        ]

    async def test_put_disables_and_rebuilds_cache(self, api) -> None:
        app, store, redis, settings = api
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                f"/api/strategies/sma_cross/bindings/{INST}",
                headers=LOCAL_WRITE,
                json={"enabled": False},
            )

        assert response.status_code == 200
        assert response.json()["binding"]["enabled"] is False
        assert store.get_binding("sma_cross", INST)["enabled"] == 0
        # 写入后缓存同步，引擎下一轮即可读到
        cached = await redis.hgetall(settings.engine.bindings_key)
        assert "sma_cross:BTC-USDT" in cached

    async def test_delete_binding_then_rebind_with_new_bar(self, api) -> None:
        """解绑入口：原先只提示「请先解绑」却无 DELETE 路由，bar 永远改不了。"""
        app, store, redis, settings = api
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/api/strategies/sma_cross/bindings")  # 触发 seed
            deleted = await client.request(
                "DELETE",
                f"/api/strategies/sma_cross/bindings/{INST}",
                headers=LOCAL_WRITE,
            )
            assert deleted.status_code == 200
            assert deleted.json()["ok"] is True
            assert store.get_binding("sma_cross", INST) is None
            # 缓存也同步清掉，引擎下一轮不再评估该组合
            cached = await redis.hgetall(settings.engine.bindings_key)
            assert "sma_cross:BTC-USDT" not in cached

            # 解绑后可用新周期重新绑定
            rebound = await client.put(
                f"/api/strategies/sma_cross/bindings/{INST}",
                headers=LOCAL_WRITE,
                json={"bar": "4H", "enabled": True},
            )
            assert rebound.status_code == 200
            assert rebound.json()["binding"]["bar"] == "4H"

    async def test_delete_binding_requires_write_guard(self, api) -> None:
        app, _store, _redis, _settings_ = api
        # 与 PUT 的写守卫测试同口径：本机 Host 但缺 X-Dashboard-Request
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.request(
                "DELETE", f"/api/strategies/sma_cross/bindings/{INST}"
            )

        assert response.status_code == 400
        assert "X-Dashboard-Request" in response.json()["detail"]

    async def test_put_rejects_bar_change(self, api) -> None:
        app, _store, _redis, _settings_ = api
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/api/strategies/sma_cross/bindings")  # 触发 seed
            response = await client.put(
                f"/api/strategies/sma_cross/bindings/{INST}",
                headers=LOCAL_WRITE,
                json={"bar": "4H"},
            )

        assert response.status_code == 400
        assert "请先解绑" in response.json()["detail"]

    async def test_put_creates_new_binding_with_default_bar(self, api) -> None:
        app, store, _redis, settings = api
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                "/api/strategies/sma_cross/bindings/ETH-USDT",
                headers=LOCAL_WRITE,
                json={"bar": "4H"},
            )

        assert response.status_code == 200
        row = store.get_binding("sma_cross", "ETH-USDT")
        assert row["bar"] == "4H"
        assert row["enabled"] == 1
        assert settings.engine.bar == "1H"

    async def test_put_rejects_invalid_bar(self, api) -> None:
        app, _store, _redis, _settings_ = api
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                "/api/strategies/sma_cross/bindings/ETH-USDT",
                headers=LOCAL_WRITE,
                json={"bar": "7X"},
            )

        assert response.status_code == 400

    async def test_put_unknown_strategy(self, api) -> None:
        app, _store, _redis, _settings_ = api
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                "/api/strategies/nope/bindings/ETH-USDT",
                headers=LOCAL_WRITE,
                json={"enabled": True},
            )

        assert response.status_code == 400

    async def test_put_requires_write_guard(self, api) -> None:
        app, _store, _redis, _settings_ = api
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.put(
                f"/api/strategies/sma_cross/bindings/{INST}",
                json={"enabled": False},
            )

        assert response.status_code == 400
        assert "X-Dashboard-Request" in response.json()["detail"]


class TestServiceDirect:
    async def test_set_binding_without_store_raises(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        _write_strategy(tmp_path)
        service = BacktestService(settings)

        with pytest.raises(StrategyParamError):
            await service.bindings("sma_cross")

    async def test_binding_update_request_defaults(self) -> None:
        req = BindingUpdateRequest()

        assert req.enabled is None
        assert req.bar is None
