"""看板回测 / 寻优接口测试。

覆盖：
- BacktestService：策略清单扫描、单次回测、网格寻优、入参校验
- HTTP 端点：GET /api/strategies、POST /api/backtest、POST /api/optimize
"""
from __future__ import annotations

import math
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
)
from quant.dashboard.service import DashboardService
from quant.foundation.config import OKXSettings, Settings
from quant.models import Bar
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage

HOUR = 3_600_000
START = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
INST = "BTC-USDT"
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

        assert items == [
            {"id": "sma_cross", "params": {"fast_len": 5, "slow_len": 20}}
        ]

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

    async def test_rejects_too_many_combos(self, service, monkeypatch) -> None:
        monkeypatch.setattr("quant.dashboard.backtest.MAX_COMBOS", 2)
        with pytest.raises(BacktestRequestError, match="组合过多"):
            await service.optimize(
                OptimizeRequest(
                    **_request().model_dump(),
                    grid="fast_len=3,5;slow_len=20,30",
                )
            )

    async def test_all_combos_invalid(self, service) -> None:
        with pytest.raises(BacktestRequestError, match="all 1 parameter"):
            await service.optimize(
                OptimizeRequest(
                    **_request().model_dump(),
                    grid="fast_len=30;slow_len=10",
                )
            )


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

    async def test_backtest_endpoint(self, app) -> None:
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
        body = response.json()
        assert body["kind"] == "backtest"
        assert body["inst"] == "BTC-USDT"
        assert body["metrics"]["trade_count"] >= 0

    async def test_backtest_error_is_400(self, app) -> None:
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
