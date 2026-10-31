"""看板回测 / 寻优接口测试。

覆盖：
- BacktestService：策略清单扫描、单次回测、网格寻优、入参校验
- HTTP 端点：GET /api/strategies、POST /api/backtest、POST /api/optimize
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest

from quant.dashboard.app import create_app
from quant.dashboard.backtest import (
    BacktestRequest,
    BacktestRequestError,
    BacktestService,
    OptimizeRequest,
    ParamUpdateRequest,
    StrategyParamError,
)
from quant.dashboard.service import DashboardService
from quant.foundation.config import OKXSettings, Settings
from quant.models import Bar
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage

HOUR = 3_600_000
START = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
INST = "BTC-USDT"
LOCAL_WRITE = {"host": "127.0.0.1:8080", "X-Dashboard-Request": "1"}
STRATEGY_TOML = (
    "[strategy]\n"
    'strategy_id = "sma_cross"\n'
    'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n\n'
    "[params]\nfast_len = 5\nslow_len = 20\n"
)


def _bar(ts: int) -> Bar:
    price = Decimal(f"{100 + 12 * math.sin((ts - START) / HOUR / 9):.2f}")
    return Bar(
        ts=ts,
        open=price,
        high=price,
        low=price,
        close=price,
        vol=Decimal("1"),
        vol_quote=price,
        confirm=1,
        inst_id=INST,
        bar_size="1H",
    )


def _write_bars(settings: Settings, count: int = 400) -> None:
    bars = [_bar(START + i * HOUR) for i in range(count)]
    BarStorage(settings.storage.data_dir).save_bars(bars)


def _settings(tmp_path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "quant.db"},
        engine={"config_dir": tmp_path / "strategies"},
        backtest={"artifacts_dir": tmp_path / "backtests"},
    )


def _write_strategy(tmp_path, name: str = "sma_cross", text: str = STRATEGY_TOML):
    cfg_dir = tmp_path / "strategies"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / f"{name}.toml").write_text(text)
    return cfg_dir


@pytest.fixture
def service(tmp_path):
    settings = _settings(tmp_path)
    _write_strategy(tmp_path)
    _write_bars(settings)
    return BacktestService(settings)


@pytest.fixture
def param_service(tmp_path):
    """带 store 的服务：参数覆盖必须落库才算数。"""
    settings = _settings(tmp_path)
    _write_strategy(tmp_path)
    store = SQLiteStore(settings.state.db_path)
    yield BacktestService(settings, store), store
    store.close()


async def _wait_done(client, run_id: str, timeout: float = 5.0) -> dict:
    """轮询任务状态直到终态；超时直接失败，避免挂住测试。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get(f"/api/backtest/{run_id}")
        body = response.json()
        if body["status"] in {"done", "failed", "canceled"}:
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish in {timeout}s")


def _request(**overrides) -> BacktestRequest:
    data = {
        "strategy": "sma_cross",
        "inst": INST,
        "bar": "1H",
        "start": "2026-09-05",
        "end": "2026-09-10",
        "params": "",
        "warmup_bars": 50,
    }
    data.update(overrides)
    return BacktestRequest(**data)


class TestStrategies:
    async def test_lists_config_with_params(self, service) -> None:
        items = await service.strategies()

        assert [item["id"] for item in items] == ["sma_cross"]
        assert items[0]["params"] == {"fast_len": 5, "slow_len": 20}
        assert items[0]["sources"] == {"fast_len": "toml", "slow_len": "toml"}
        # PARAM_SPEC 里声明的才可编辑；toml 里的其它键不出现
        spec_names = [item["name"] for item in items[0]["param_spec"]]
        assert items[0]["editable"] == spec_names
        assert "commission_rate" not in spec_names

    async def test_skips_broken_config(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        _write_strategy(tmp_path)
        cfg_dir = _write_strategy(tmp_path, "broken", "[strategy]\nid = 1\n")

        assert (cfg_dir / "broken.toml").is_file()
        items = await BacktestService(settings).strategies()

        assert [item["id"] for item in items] == ["sma_cross"]


class TestBacktest:
    async def test_returns_metrics_and_equity(self, service) -> None:
        payload = await service.run(_request())

        assert payload["kind"] == "backtest"
        assert payload["inst"] == INST
        assert payload["bar_count"] == 6 * 24
        assert payload["warmup_count"] == 50
        assert payload["params"] == {"fast_len": 5, "slow_len": 20}
        assert set(payload["metrics"]) >= {"sharpe", "max_drawdown", "trade_count"}
        assert len(payload["equity"]) == payload["bar_count"]
        assert payload["trade_count"] >= 1
        assert len(payload["trades"]) == payload["trade_count"]
        assert all(item["side"] in ("buy", "sell") for item in payload["trades"])
        assert payload["elapsed_ms"] >= 0

    async def test_param_override(self, service) -> None:
        payload = await service.run(_request(params="fast_len=8;slow_len=30"))

        assert payload["params"] == {"fast_len": 8, "slow_len": 30}

    async def test_unknown_strategy(self, service) -> None:
        with pytest.raises(BacktestRequestError, match="not found"):
            await service.run(_request(strategy="nope"))

    async def test_insufficient_warmup(self, service) -> None:
        with pytest.raises(BacktestRequestError, match="not enough warmup"):
            await service.run(_request(warmup_bars=5000, start="2026-09-02"))

    async def test_bad_range(self, service) -> None:
        with pytest.raises(BacktestRequestError, match="must not be after"):
            await service.run(_request(start="2026-09-10", end="2026-09-05"))

    async def test_invalid_params(self, service) -> None:
        with pytest.raises(BacktestRequestError, match="must be < slow_len"):
            await service.run(_request(params="fast_len=30;slow_len=10"))

    async def test_no_data(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        _write_strategy(tmp_path)
        with pytest.raises(BacktestRequestError, match="no local bars"):
            await BacktestService(settings).run(_request())


class TestOptimize:
    async def test_returns_sorted_ranking(self, service) -> None:
        payload = await service.optimize(
            OptimizeRequest(
                **_request().model_dump(),
                grid="fast_len=3,5;slow_len=20,30",
                metric="sharpe",
                top=3,
            )
        )

        assert payload["kind"] == "optimize"
        assert payload["combo_count"] == 4
        assert payload["skipped_count"] == 0
        assert payload["grid"] == {
            "fast_len": [3, 5],
            "slow_len": [20, 30],
        }
        assert [row["rank"] for row in payload["results"]] == [1, 2, 3]
        sharpes = [row["metrics"]["sharpe"] for row in payload["results"]]
        assert sharpes == sorted(sharpes, reverse=True)
        assert set(payload["results"][0]["params"]) == {"fast_len", "slow_len"}

    async def test_drawdown_sorted_ascending(self, service) -> None:
        payload = await service.optimize(
            OptimizeRequest(
                **_request().model_dump(),
                grid="fast_len=3,5;slow_len=20,30",
                metric="max_drawdown",
            )
        )

        values = [row["metrics"]["max_drawdown"] for row in payload["results"]]
        assert values == sorted(values)

    async def test_unknown_metric(self, service) -> None:
        with pytest.raises(BacktestRequestError, match="unknown metric"):
            await service.optimize(
                OptimizeRequest(
                    **_request().model_dump(),
                    grid="fast_len=3,5",
                    metric="nope",
                )
            )

    async def test_rejects_empty_grid(self, service) -> None:
        with pytest.raises(BacktestRequestError, match="empty param grid"):
            await service.optimize(
                OptimizeRequest(**_request().model_dump(), grid="")
            )

    async def test_rejects_too_many_combos(self, service) -> None:
        # 上限来自 config，CLI 与看板共用同一个值
        service._settings.backtest.max_combos = 2
        with pytest.raises(BacktestRequestError, match="组合过多"):
            await service.optimize(
                OptimizeRequest(
                    **_request().model_dump(),
                    grid="fast_len=3,5;slow_len=20,30",
                )
            )

    async def test_parallel_matches_serial(self, service) -> None:
        req = OptimizeRequest(
            **_request().model_dump(),
            grid="fast_len=3,5;slow_len=20,30",
            metric="total_return",
        )

        serial = await service.optimize(req)
        parallel = await service.optimize(req.model_copy(update={"n_jobs": 2}))

        assert parallel["n_jobs"] == 2
        assert [r["params"] for r in parallel["results"]] == [
            r["params"] for r in serial["results"]
        ]
        assert [r["metrics"] for r in parallel["results"]] == [
            r["metrics"] for r in serial["results"]
        ]

    async def test_all_combos_invalid(self, service) -> None:
        with pytest.raises(BacktestRequestError, match="all 1 parameter"):
            await service.optimize(
                OptimizeRequest(
                    **_request().model_dump(),
                    grid="fast_len=30;slow_len=10",
                )
            )


class TestStrategyParamWrites:
    async def test_get_reports_toml_sources(self, param_service) -> None:
        service, _store = param_service

        detail = await service.params("sma_cross")

        assert detail["params"]["fast_len"] == 5
        assert detail["sources"]["fast_len"] == "toml"
        assert "fast_len" in detail["editable"]

    async def test_unknown_strategy(self, param_service) -> None:
        service, _store = param_service

        with pytest.raises(StrategyParamError, match="not found"):
            await service.params("nope")

    async def test_write_persists_and_audits(self, param_service) -> None:
        service, store = param_service

        detail = await service.set_params(
            "sma_cross",
            ParamUpdateRequest(
                values={"slow_len": 30, "ema_filter": True},
                reason="过滤震荡",
                by="alice",
            ),
        )

        assert detail["changed"] == ["slow_len", "ema_filter"]
        assert detail["sources"]["slow_len"] == "db"
        assert detail["params"]["ema_filter"] is True

        changes, total = store.list_param_changes("sma_cross")
        assert total == 2
        by_param = {row["param"]: row for row in changes}
        assert by_param["slow_len"]["new_value"] == "30"
        assert by_param["ema_filter"]["new_value"] == "true"
        assert by_param["slow_len"]["reason"] == "过滤震荡"
        assert by_param["slow_len"]["changed_by"] == "alice"

    async def test_same_value_writes_nothing(self, param_service) -> None:
        service, store = param_service

        detail = await service.set_params(
            "sma_cross", ParamUpdateRequest(values={"fast_len": 5})
        )

        assert detail["changed"] == []
        assert store.list_param_changes("sma_cross")[1] == 0
        assert store.get_strategy_params("sma_cross") == []

    async def test_rejects_uneditable_param(self, param_service) -> None:
        service, _store = param_service

        with pytest.raises(StrategyParamError, match="PARAM_SPEC"):
            await service.set_params(
                "sma_cross", ParamUpdateRequest(values={"commission_rate": 0})
            )

    async def test_rejects_out_of_range(self, param_service) -> None:
        service, _store = param_service

        with pytest.raises(StrategyParamError, match="slow_len"):
            await service.set_params(
                "sma_cross", ParamUpdateRequest(values={"slow_len": 9999})
            )

    async def test_empty_values(self, param_service) -> None:
        service, _store = param_service

        with pytest.raises(StrategyParamError, match="empty"):
            await service.set_params("sma_cross", ParamUpdateRequest())

    async def test_requires_store(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        _write_strategy(tmp_path)

        with pytest.raises(StrategyParamError, match="store"):
            await BacktestService(settings).set_params(
                "sma_cross", ParamUpdateRequest(values={"fast_len": 8})
            )

    async def test_history_pagination(self, param_service) -> None:
        service, _store = param_service
        for value in (8, 9, 10):
            await service.set_params(
                "sma_cross", ParamUpdateRequest(values={"fast_len": value})
            )

        page = await service.param_changes("sma_cross", limit=2, offset=1)

        assert page["total"] == 3
        assert [row["new_value"] for row in page["changes"]] == ["9", "8"]


@pytest.fixture
def app(tmp_path):
    settings = _settings(tmp_path)
    _write_strategy(tmp_path)
    _write_bars(settings)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SQLiteStore(settings.state.db_path)
    dashboard = create_app(DashboardService(store, redis, settings), tmp_path / "dist")
    yield dashboard
    store.close()


class TestApp:
    async def test_strategies_endpoint(self, app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/strategies")

        assert response.status_code == 200
        assert response.json()["strategies"][0]["id"] == "sma_cross"

    async def test_backtest_endpoint_sync_compat(self, app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/backtest?sync=1",
                json={
                    "strategy": "sma_cross",
                    "start": "2026-09-05",
                    "end": "2026-09-10",
                    "warmup_bars": 50,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == "backtest"
        assert body["inst"] == "BTC-USDT"
        assert body["metrics"]["trade_count"] >= 0

    async def test_backtest_endpoint_returns_run_id(self, app) -> None:
        """默认异步：立即返回 run_id，结果与产物随后落库。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/backtest",
                    json={
                        "strategy": "sma_cross",
                        "start": "2026-09-05",
                        "end": "2026-09-10",
                        "warmup_bars": 50,
                    },
                )
                assert response.status_code == 200
                run_id = response.json()["run_id"]
                assert response.json()["status"] == "pending"

                detail = await _wait_done(client, run_id)
                assert detail["status"] == "done"
                assert detail["inst"] == INST
                assert detail["metrics"]["trade_count"] >= 0
                assert {item["kind"] for item in detail["artifacts"]} == {
                    "equity",
                    "trades",
                    "report",
                }

                equity = await client.get(f"/api/backtest/{run_id}/equity")
                assert len(equity.json()["points"]) > 0

                trades = (await client.get(f"/api/backtest/{run_id}/trades")).json()
                # metrics.trade_count 是「回合数」，产物里是逐笔成交，两者口径不同
                assert trades["total"] > 0
                assert trades["total"] == len(trades["trades"])

                history = await client.get("/api/backtest/runs")
                assert history.json()["total"] == 1
                assert history.json()["runs"][0]["run_id"] == run_id

    async def test_backtest_endpoint_error_is_400(self, app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/backtest",
                json={
                    "strategy": "missing",
                    "start": "2026-09-05",
                    "end": "2026-09-10",
                },
            )

        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

    # ------------------------------------------------------------------
    # 参数覆盖（MS4-C §11.1）
    # ------------------------------------------------------------------

    async def test_params_endpoint(self, app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/strategies/sma_cross/params")
            missing = await client.get("/api/strategies/nope/params")

        assert response.status_code == 200
        assert response.json()["sources"]["fast_len"] == "toml"
        assert missing.status_code == 404

    async def test_put_params_requires_guard(self, app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                "/api/strategies/sma_cross/params",
                json={"values": {"fast_len": 8}},
            )

        # base_url 是 test 主机，先被 Host 白名单拦下
        assert response.status_code == 403

    async def test_put_params_persists_and_records_history(self, app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.put(
                "/api/strategies/sma_cross/params",
                headers=LOCAL_WRITE,
                json={
                    "values": {"fast_len": 8},
                    "reason": "调快均线",
                    "by": "alice",
                },
            )
            body = response.json()
            history = await client.get(
                "/api/strategies/sma_cross/param-changes"
            )
            detail = await client.get("/api/strategies/sma_cross/params")

        assert response.status_code == 200
        assert body["changed"] == ["fast_len"]
        assert body["params"]["fast_len"] == 8
        assert body["sources"]["fast_len"] == "db"
        assert detail.json()["params"]["fast_len"] == 8

        changes = history.json()
        assert changes["total"] == 1
        row = changes["changes"][0]
        assert row["param"] == "fast_len"
        assert row["old_value"] is None
        assert row["new_value"] == "8"
        assert row["reason"] == "调快均线"
        assert row["changed_by"] == "alice"

    async def test_put_params_rejects_uneditable(self, app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.put(
                "/api/strategies/sma_cross/params",
                headers=LOCAL_WRITE,
                json={"values": {"commission_rate": 0}},
            )

        assert response.status_code == 400
        assert "PARAM_SPEC" in response.json()["detail"]

    async def test_optimize_endpoint(self, app) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/optimize",
                json={
                    "strategy": "sma_cross",
                    "start": "2026-09-05",
                    "end": "2026-09-10",
                    "warmup_bars": 50,
                    "grid": "fast_len=3,5;slow_len=20,30",
                    "metric": "sharpe",
                    "top": 2,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == "optimize"
        assert len(body["results"]) == 2
        assert body["combo_count"] == 4
