"""批量回测测试（M4-7）：多品种 × 多周期 × 多参数。

覆盖：
- `expand_cells` 的顺序、去重与空入参
- `sort_cells` 的升降序与「算不出」的兜底位置
- `BatchSweep` 的 K 线复用、单格失败隔离、全失败报错、取消
- 服务层批量入口（品种/周期/参数组合、上限、未知指标）
- 任务队列登记 + 产物落盘 + 逐格读取 + 重跑
- HTTP 端点
"""
from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest

from quant.backtest.batch import BatchCell, BatchSweep, expand_cells, sort_cells
from quant.backtest.matcher import Matcher
from quant.backtest.signal import Signal
from quant.backtest.strategy import Strategy
from quant.dashboard.app import create_app
from quant.dashboard.backtest import (
    BacktestRequestError,
    BacktestService,
    BatchRequest,
)
from quant.dashboard.service import DashboardService
from quant.dashboard.tasks import DONE, TaskRunner
from quant.foundation.config import OKXSettings, Settings
from quant.models import Bar
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage

HOUR = 3_600_000
START = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
STRATEGY_TOML = (
    "[strategy]\n"
    'strategy_id = "sma_cross"\n'
    'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n\n'
    "[params]\nfast_len = 5\nslow_len = 20\n"
)

# ----------------------------------------------------------------------
# 纯逻辑：格子展开 / 排序 / 逐格回测
# ----------------------------------------------------------------------


class _QtyStrategy(Strategy):
    """首根买入、第三根卖出，用 qty 参数把各格的收益拉开。"""

    def __init__(self) -> None:
        self.qty = Decimal("1")

    @property
    def strategy_id(self) -> str:
        return "qty"

    def on_init(self, ctx) -> None:
        self.qty = Decimal(str(ctx.params.get("qty", "1")))
        if self.qty <= 0:
            raise ValueError("qty must be positive")

    def on_bar(self, bar: Bar, ctx) -> Signal | None:
        if len(ctx.bars_window) == 1 and ctx.position.is_flat:
            return Signal(
                strategy_id=self.strategy_id,
                side="buy",
                order_type="market",
                inst_id=bar.inst_id,
                qty=self.qty,
                ts=bar.ts,
            )
        if len(ctx.bars_window) == 3 and ctx.position.is_long:
            return Signal(
                strategy_id=self.strategy_id,
                side="sell",
                order_type="market",
                inst_id=bar.inst_id,
                qty=ctx.position.qty,
                ts=bar.ts,
            )
        return None


def _bars(inst: str = "BTC-USDT") -> list[Bar]:
    """100 → 110 → 120 → 120：买入后上涨，qty 越大收益越高。"""
    ohlc = [
        ("100", "100", "100", "100"),
        ("100", "110", "100", "110"),
        ("110", "120", "110", "120"),
        ("120", "120", "120", "120"),
    ]
    return [
        Bar(
            ts=START + i * HOUR,
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(low),
            close=Decimal(c),
            vol=Decimal("1"),
            vol_quote=Decimal("1"),
            confirm=1,
            inst_id=inst,
            bar_size="1H",
        )
        for i, (o, h, low, c) in enumerate(ohlc)
    ]


def _sweep() -> BatchSweep:
    return BatchSweep(lambda _params: _QtyStrategy(), matcher=Matcher())


class TestExpandCells:
    def test_order_is_inst_then_bar_then_params(self) -> None:
        cells = expand_cells(
            ["BTC-USDT", "ETH-USDT"], ["1H", "4H"], {"qty": ["1", "2"]}
        )

        assert len(cells) == 8
        assert cells[0] == ("BTC-USDT", "1H", {"qty": "1"})
        assert cells[1] == ("BTC-USDT", "1H", {"qty": "2"})
        assert cells[2] == ("BTC-USDT", "4H", {"qty": "1"})
        assert cells[-1] == ("ETH-USDT", "4H", {"qty": "2"})

    def test_strips_and_dedupes(self) -> None:
        cells = expand_cells([" BTC-USDT ", "BTC-USDT"], ["1H"], None)

        assert cells == [("BTC-USDT", "1H", {})]

    def test_empty_insts_rejected(self) -> None:
        with pytest.raises(ValueError, match="insts is empty"):
            expand_cells([], ["1H"])

    def test_blank_bars_rejected(self) -> None:
        with pytest.raises(ValueError, match="bars is empty"):
            expand_cells(["BTC-USDT"], ["  "])


def _cell(inst: str, sharpe: float | None, drawdown: float | None = None) -> BatchCell:
    metrics: dict[str, float | int | str | None] = {}
    if sharpe is not None:
        metrics["sharpe"] = sharpe
    if drawdown is not None:
        metrics["max_drawdown"] = drawdown
    return BatchCell(inst=inst, bar="1H", params={}, metrics=metrics)


class TestSortCells:
    def test_descending_puts_missing_last(self) -> None:
        cells = [_cell("A", 1.0), _cell("B", None), _cell("C", 2.0)]

        assert [cell.inst for cell in sort_cells(cells, "sharpe")] == ["C", "A", "B"]

    def test_ascending_puts_missing_last(self) -> None:
        cells = [_cell("A", 1.0, 0.2), _cell("B", 1.0, None), _cell("C", 1.0, 0.1)]

        ordered = sort_cells(cells, "max_drawdown", ascending=True)

        assert [cell.inst for cell in ordered] == ["C", "A", "B"]

    def test_non_numeric_metric_counts_as_missing(self) -> None:
        cell = BatchCell("A", "1H", {}, {"monthly_returns": "[]"})

        assert sort_cells([cell], "monthly_returns")


class TestBatchSweep:
    def test_bars_loaded_once_per_inst_and_period(self) -> None:
        calls: list[tuple[str, str]] = []

        def load(inst: str, bar: str):
            calls.append((inst, bar))
            return [], _bars(inst)

        cells = expand_cells(
            ["BTC-USDT", "ETH-USDT"], ["1H"], {"qty": ["1", "2", "3"]}
        )
        ok, failed = _sweep().run(cells, load)

        assert len(ok) == 6
        assert failed == []
        # 3 组参数共用一份 K 线，不必读三遍
        assert calls == [("BTC-USDT", "1H"), ("ETH-USDT", "1H")]

    def test_missing_symbol_only_fails_its_own_cells(self) -> None:
        def load(inst: str, bar: str):
            if inst == "ETH-USDT":
                raise RuntimeError("no local bars")
            return [], _bars(inst)

        cells = expand_cells(["BTC-USDT", "ETH-USDT"], ["1H"])
        ok, failed = _sweep().run(cells, load)

        assert [cell.inst for cell in ok] == ["BTC-USDT"]
        assert [cell.inst for cell in failed] == ["ETH-USDT"]
        assert "no local bars" in (failed[0].error or "")

    def test_invalid_params_fail_only_that_cell(self) -> None:
        cells = expand_cells(["BTC-USDT"], ["1H"], {"qty": ["1", "0"]})

        ok, failed = _sweep().run(cells, lambda inst, bar: ([], _bars(inst)))

        assert len(ok) == 1
        assert [cell.params["qty"] for cell in failed] == ["0"]
        assert "qty must be positive" in (failed[0].error or "")

    def test_all_cells_failed_raises(self) -> None:
        def load(inst: str, bar: str):
            raise RuntimeError("boom")

        with pytest.raises(ValueError, match="all 1 batch cells failed"):
            _sweep().run(expand_cells(["BTC-USDT"], ["1H"]), load)

    def test_should_stop_aborts_within_cell_and_discards_partial(self) -> None:
        """取消粒度与单次回测一致（每根 bar）：格内取消要丢弃部分结果（P1）。"""
        seen = {"n": 0}

        def stop() -> bool:
            seen["n"] += 1
            return seen["n"] > 1

        cells = expand_cells(["A", "B", "C"], ["1H"])
        ok, failed = _sweep().run(
            cells, lambda inst, bar: ([], _bars(inst)), should_stop=stop
        )

        # 第一格内即取消：部分结果不可用，后续格不再执行，且不报「全部失败」
        assert ok == []
        assert failed == []

    def test_progress_reports_done_and_total(self) -> None:
        steps: list[tuple[int, int]] = []
        cells = expand_cells(["A", "B"], ["1H"])

        _sweep().run(
            cells, lambda inst, bar: ([], _bars(inst)), progress=lambda d, t: steps.append((d, t))
        )

        assert steps == [(1, 2), (2, 2)]


# ----------------------------------------------------------------------
# 看板服务 / 任务队列 / HTTP
# ----------------------------------------------------------------------


def _bar(ts: int, inst: str, bar: str) -> Bar:
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
        inst_id=inst,
        bar_size=bar,
    )


def _settings(tmp_path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "quant.db"},
        engine={"config_dir": tmp_path / "strategies"},
        backtest={"artifacts_dir": tmp_path / "backtests"},
    )


def _write_world(tmp_path, settings: Settings) -> None:
    cfg_dir = tmp_path / "strategies"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "sma_cross.toml").write_text(STRATEGY_TOML)

    storage = BarStorage(settings.storage.data_dir)
    for inst in ("BTC-USDT", "ETH-USDT"):
        for bar in ("1H", "4H"):
            storage.save_bars(
                [_bar(START + i * HOUR, inst, bar) for i in range(400)]
            )


def _batch(**overrides) -> BatchRequest:
    data = {
        "strategy": "sma_cross",
        "insts": ["BTC-USDT", "ETH-USDT"],
        "bars": ["1H", "4H"],
        "start": "2026-09-05",
        "end": "2026-09-10",
        "warmup_bars": 20,
        "metric": "sharpe",
    }
    data.update(overrides)
    return BatchRequest(**data)


@pytest.fixture
def env(tmp_path):
    settings = _settings(tmp_path)
    _write_world(tmp_path, settings)
    store = SQLiteStore(settings.state.db_path)
    service = BacktestService(settings, store)
    yield store, settings, service
    store.close()


class TestServiceBatch:
    async def test_runs_every_cell_and_sorts_by_metric(self, env) -> None:
        _store, _settings_, service = env

        payload, result = await service.build_batch_async(
            _batch(grid="fast_len=3,5;slow_len=20,30")
        )

        # 2 品种 × 2 周期 × 4 组参数
        assert payload["combo_count"] == 16
        assert payload["cell_count"] == len(payload["rows"]) == 16
        assert payload["failed_count"] == 0
        assert payload["kind"] == "batch"
        assert payload["insts"] == ["BTC-USDT", "ETH-USDT"]
        assert payload["bars"] == ["1H", "4H"]
        assert len(result.cells) == 16

        sharpes = [row["metrics"]["sharpe"] for row in payload["rows"]]
        assert sharpes == sorted(sharpes, reverse=True)

    async def test_ascending_metric_reverses_order(self, env) -> None:
        _store, _settings_, service = env

        payload, _result = await service.build_batch_async(
            _batch(metric="max_drawdown")
        )

        drawdowns = [row["metrics"]["max_drawdown"] for row in payload["rows"]]
        assert drawdowns == sorted(drawdowns)

    async def test_combo_limit_is_shared_with_optimize(self, env) -> None:
        _store, settings, service = env
        settings.backtest.max_combos = 3

        with pytest.raises(BacktestRequestError, match="批量组合过多"):
            await service.build_batch_async(_batch())

    async def test_unknown_metric_rejected(self, env) -> None:
        _store, _settings_, service = env

        with pytest.raises(BacktestRequestError, match="unknown metric"):
            await service.build_batch_async(_batch(metric="monthly_returns"))

    async def test_empty_insts_rejected(self, env) -> None:
        _store, _settings_, service = env

        # pydantic 拦下空列表；直接传空串也应在展开阶段报错
        with pytest.raises(BacktestRequestError, match="insts is empty"):
            await service.build_batch_async(_batch(insts=["  "]))

    async def test_bad_date_rejected_before_loading(self, env) -> None:
        _store, _settings_, service = env

        with pytest.raises(BacktestRequestError, match="invalid date"):
            await service.build_batch_async(_batch(start="2026-13-01"))


class TestTaskRunnerBatch:
    async def test_submit_runs_to_done_and_writes_rows(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            run_id = await tasks.submit_batch(_batch())
            detail = await _drain(tasks, run_id)
            rows = tasks.batches(run_id)
        finally:
            await tasks.close()

        assert detail["status"] == DONE
        assert detail["kind"] == "batch"
        assert detail["inst"] == "BTC-USDT,ETH-USDT"
        assert detail["bar"] == "1H,4H"
        assert {item["kind"] for item in detail["artifacts"]} == {"batch"}
        assert len(rows) == 4
        assert {row["inst"] for row in rows} == {"BTC-USDT", "ETH-USDT"}
        assert rows[0]["params"]["fast_len"] == 5
        assert rows[0]["metrics"]["sharpe"] is not None

    async def test_rerun_creates_new_batch_run(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            first = await tasks.submit_batch(_batch())
            await _drain(tasks, first)
            second = await tasks.rerun(first)
            detail = await _drain(tasks, second)
        finally:
            await tasks.close()

        assert second != first
        assert detail["kind"] == "batch"
        assert detail["params"]["insts"] == ["BTC-USDT", "ETH-USDT"]
        assert detail["params"]["metric"] == "sharpe"

    async def test_empty_rows_for_non_batch_run(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)

        assert tasks.batches("nope") == []


async def _drain(tasks: TaskRunner, run_id: str, timeout: float = 20.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        detail = tasks.get(run_id)
        if detail["status"] in (DONE, "failed", "canceled"):
            return detail
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")


@pytest.fixture
def app(tmp_path):
    settings = _settings(tmp_path)
    _write_world(tmp_path, settings)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SQLiteStore(settings.state.db_path)
    dashboard = create_app(DashboardService(store, redis, settings), tmp_path / "dist")
    yield dashboard, store
    store.close()


class TestBatchEndpoint:
    async def test_sync_batch(self, app) -> None:
        dashboard, _store = app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/backtest/batch?sync=1",
                json={
                    "strategy": "sma_cross",
                    "insts": ["BTC-USDT", "ETH-USDT"],
                    "bars": ["1H"],
                    "start": "2026-09-05",
                    "end": "2026-09-10",
                    "warmup_bars": 20,
                    "grid": "fast_len=3,5;slow_len=20,30",
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["combo_count"] == 8
        assert body["cell_count"] == 8
        assert len(body["rows"]) == 8

    async def test_bad_metric_is_400(self, app) -> None:
        dashboard, _store = app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/backtest/batch?sync=1",
                json={
                    "strategy": "sma_cross",
                    "insts": ["BTC-USDT"],
                    "bars": ["1H"],
                    "start": "2026-09-05",
                    "end": "2026-09-10",
                    "metric": "monthly_returns",
                },
            )

        assert response.status_code == 400

    async def test_empty_insts_is_422(self, app) -> None:
        dashboard, _store = app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/backtest/batch",
                json={
                    "strategy": "sma_cross",
                    "insts": [],
                    "bars": ["1H"],
                    "start": "2026-09-05",
                    "end": "2026-09-10",
                },
            )

        assert response.status_code == 422

    async def test_rows_endpoint_is_empty_without_artifact(self, app) -> None:
        dashboard, store = app
        store.create_backtest_run(
            "20261101-sma_cross-batch-001",
            strategy_id="sma_cross",
            inst_id="BTC-USDT,ETH-USDT",
            bar="1H,4H",
            start_ts=START,
            end_ts=START + HOUR,
            params="{}",
            kind="batch",
            status="done",
            created_at=START,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/backtest/20261101-sma_cross-batch-001/batch"
            )
            missing = await client.get("/api/backtest/nope/batch")

        assert response.status_code == 200
        assert response.json()["rows"] == []
        assert missing.status_code == 404
