"""回测任务队列测试（MS4-C §5.2 / §10.4）。"""
from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from quant.dashboard.backtest import (
    BacktestRequest,
    BacktestRequestError,
    BacktestService,
    WalkforwardRequest,
)
from quant.dashboard.tasks import (
    CANCELED,
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    RunNotFound,
    TaskRunner,
)
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


def _settings(tmp_path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "quant.db"},
        engine={"config_dir": tmp_path / "strategies"},
        backtest={"artifacts_dir": tmp_path / "backtests"},
    )


@pytest.fixture
def env(tmp_path):
    settings = _settings(tmp_path)
    cfg_dir = tmp_path / "strategies"
    cfg_dir.mkdir()
    (cfg_dir / "sma_cross.toml").write_text(STRATEGY_TOML)
    BarStorage(settings.storage.data_dir).save_bars(
        [_bar(START + i * HOUR) for i in range(400)]
    )
    store = SQLiteStore(settings.state.db_path)
    service = BacktestService(settings, store)
    yield store, settings, service
    store.close()


def _request(**overrides) -> BacktestRequest:
    data = {
        "strategy": "sma_cross",
        "inst": INST,
        "bar": "1H",
        "start": "2026-09-05",
        "end": "2026-09-10",
        "warmup_bars": 50,
    }
    data.update(overrides)
    return BacktestRequest(**data)


def _wf_request(**overrides) -> WalkforwardRequest:
    data = {
        "strategy": "sma_cross",
        "inst": INST,
        "bar": "1H",
        "start": "2026-09-05",
        "end": "2026-09-10",
        "warmup_bars": 20,
        "grid": "fast_len=3,5;slow_len=15,20",
        "metric": "sharpe",
        "is_bars": 60,
        "oos_bars": 20,
    }
    data.update(overrides)
    return WalkforwardRequest(**data)


async def _drain(tasks: TaskRunner, run_id: str, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        detail = tasks.get(run_id)
        if detail["status"] in (DONE, FAILED, CANCELED):
            return detail
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")


class TestSubmit:
    async def test_runs_to_done_and_persists(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            run_id = await tasks.submit(_request())
            detail = await _drain(tasks, run_id)
        finally:
            await tasks.close()

        assert detail["status"] == DONE
        assert detail["inst"] == INST
        assert detail["params"]["fast_len"] == 5
        assert detail["metrics"]["sharpe"] is not None
        # M4-15 收尾：资金费率成本随指标落库；默认开关关闭时恒为 0
        assert detail["metrics"]["funding_cost"] == 0.0
        assert {item["kind"] for item in detail["artifacts"]} == {
            "equity",
            "trades",
            "report",
        }
        assert detail["started_at"] <= detail["finished_at"]

    async def test_run_id_is_sequential_per_day(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            first = await tasks.submit(_request())
            second = await tasks.submit(_request())
            await _drain(tasks, first)
            await _drain(tasks, second)
        finally:
            await tasks.close()

        assert first.endswith("-001")
        assert second.endswith("-002")
        assert first != second

    async def test_unknown_strategy_is_rejected_before_queueing(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            with pytest.raises(BacktestRequestError, match="not found"):
                await tasks.submit(_request(strategy="missing"))
        finally:
            await tasks.close()

        assert store.list_backtest_runs()[1] == 0

    async def test_equity_and_trades_read_from_artifacts(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            run_id = await tasks.submit(_request())
            await _drain(tasks, run_id)
            points = tasks.equity(run_id)
            trades = tasks.trades(run_id, limit=2)
        finally:
            await tasks.close()

        assert points
        assert {"ts", "equity", "drawdown"} <= set(points[0])
        assert trades["total"] >= trades["trades"].__len__()

    def test_equity_downsample_keeps_last_point(self, env, monkeypatch) -> None:
        """P1：等距采样不能丢尾点，否则曲线终点与 final_equity 不符。"""
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        frame = pd.DataFrame(
            {
                "ts": list(range(100)),
                "equity": [str(i) for i in range(100)],
                "drawdown": ["0"] * 100,
            }
        )
        monkeypatch.setattr(tasks, "_artifact_path", lambda *a: "fake")
        monkeypatch.setattr(
            "quant.dashboard.tasks._read_parquet", lambda _p: frame
        )

        points = tasks.equity("x", limit=60)

        assert points[0]["ts"] == 0
        assert points[-1]["ts"] == 99
        assert points[-1]["equity"] == "99"

    async def test_equity_of_unknown_run_is_empty(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)

        assert tasks.equity("nope") == []
        assert tasks.trades("nope") == {"total": 0, "trades": []}


class TestCancel:
    async def test_cancel_pending(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            run_id = await tasks.submit(_request())
            status = await tasks.cancel(run_id)
            detail = await _drain(tasks, run_id)
        finally:
            await tasks.close()

        assert status in (CANCELED, "canceling")
        assert detail["status"] == CANCELED
        assert detail["metrics"] == {}

    async def test_cancel_running_stops_at_next_bar(self, env) -> None:
        store, settings, service = env
        stub = _SlowService()
        tasks = TaskRunner(store, settings, stub)
        try:
            run_id = await tasks.submit(_request())
            await asyncio.wait_for(stub.started.wait(), timeout=2)
            assert await tasks.cancel(run_id) == "canceling"
            detail = await _drain(tasks, run_id)
        finally:
            await tasks.close()

        assert detail["status"] == CANCELED
        assert stub.saw_stop is True

    async def test_cancel_terminal_run_is_noop(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            run_id = await tasks.submit(_request())
            await _drain(tasks, run_id)
            assert await tasks.cancel(run_id) == DONE
        finally:
            await tasks.close()

    async def test_cancel_unknown_run(self, env) -> None:
        store, settings, service = env
        tasks = TaskRunner(store, settings, service)

        with pytest.raises(RunNotFound):
            await tasks.cancel("nope")


class TestRerun:
    async def test_rerun_creates_new_run_with_same_params(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            run_id = await tasks.submit(_request())
            await _drain(tasks, run_id)
            new_id = await tasks.rerun(run_id)
            detail = await _drain(tasks, new_id)
            original = tasks.get(run_id)
        finally:
            await tasks.close()

        assert new_id != run_id
        assert detail["status"] == DONE
        assert detail["params"] == original["params"]
        assert detail["start"] == original["start"]
        assert detail["end"] == original["end"]
        # 旧结果保留，不被覆盖
        assert original["status"] == DONE

    async def test_rerun_unknown_run(self, env) -> None:
        store, settings, service = env
        tasks = TaskRunner(store, settings, service)

        with pytest.raises(RunNotFound):
            await tasks.rerun("nope")


class TestWalkforward:
    async def test_persists_segments_and_summary(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            run_id = await tasks.submit_walkforward(_wf_request())
            detail = await _drain(tasks, run_id)
            segments = tasks.segments(run_id)
            rerun_id = await tasks.rerun(run_id)
            rerun_detail = await _drain(tasks, rerun_id)
        finally:
            await tasks.close()

        assert detail["status"] == DONE
        assert detail["kind"] == "walkforward"
        # 汇总指标按 §8 落库，模态参数是独立列
        assert detail["metrics"]["compounded_return"] is not None
        assert detail["metrics"]["profitable_segments"] is not None
        assert isinstance(detail["metrics"]["modal_params"], dict)
        assert {item["kind"] for item in detail["artifacts"]} == {"segments"}

        assert segments
        first = segments[0]
        assert set(first) >= {
            "index",
            "is_start",
            "is_end",
            "oos_start",
            "oos_end",
            "params",
            "is_metrics",
            "oos_metrics",
        }
        assert isinstance(first["params"], dict)
        assert "sharpe" in first["oos_metrics"]

        # 重跑要能从落库的参数结构还原网格与切分
        assert rerun_detail["status"] == DONE
        assert rerun_detail["kind"] == "walkforward"
        assert rerun_detail["metrics"]["compounded_return"] == pytest.approx(
            detail["metrics"]["compounded_return"]
        )


class TestHistory:
    async def test_filters_and_pagination(self, env) -> None:
        store, _settings_, service = env
        tasks = TaskRunner(store, service._settings, service)
        try:
            ids = []
            for _ in range(3):
                ids.append(await tasks.submit(_request()))
            for run_id in ids:
                await _drain(tasks, run_id)

            page = tasks.list_runs(2, offset=1, strategy_id="sma_cross")
            other = tasks.list_runs(10, inst_id="ETH-USDT")
        finally:
            await tasks.close()

        assert page["total"] == 3
        assert len(page["runs"]) == 2
        assert other["total"] == 0
        assert all(item["metrics"] for item in page["runs"])

    async def test_compare_requires_same_inst_and_bar(self, env) -> None:
        store, settings, service = env
        tasks = TaskRunner(store, settings, service)
        now = 1_700_000_000_000
        for run_id, bar in (("a", "1H"), ("b", "4H")):
            store.create_backtest_run(
                run_id,
                strategy_id="sma_cross",
                inst_id=INST,
                bar=bar,
                start_ts=now,
                end_ts=now + HOUR,
                params="{}",
                kind="backtest",
                status=DONE,
                created_at=now,
            )

        with pytest.raises(ValueError, match="品种或周期不一致"):
            tasks.compare(["a", "b"])

    async def test_compare_ok(self, env) -> None:
        store, settings, service = env
        tasks = TaskRunner(store, settings, service)
        now = 1_700_000_000_000
        for run_id in ("a", "b"):
            store.create_backtest_run(
                run_id,
                strategy_id="sma_cross",
                inst_id=INST,
                bar="1H",
                start_ts=now,
                end_ts=now + HOUR,
                params="{}",
                kind="backtest",
                status=DONE,
                created_at=now,
            )

        result = tasks.compare(["a", "b"])

        assert result["inst_id"] == INST
        assert [item["run_id"] for item in result["runs"]] == ["a", "b"]

    async def test_compare_needs_two(self, env) -> None:
        store, settings, service = env
        tasks = TaskRunner(store, settings, service)

        with pytest.raises(ValueError, match="at least 2"):
            tasks.compare(["a"])


class TestCompareValidation:
    """M3-18 同品种+同周期+同区间+已完成（P1）。"""

    def _run(self, store, run_id: str, *, start: int, status: str = DONE) -> None:
        store.create_backtest_run(
            run_id,
            strategy_id="sma_cross",
            inst_id=INST,
            bar="1H",
            start_ts=start,
            end_ts=start + HOUR,
            params="{}",
            kind="backtest",
            status=status,
            created_at=start,
        )

    async def test_compare_rejects_different_range(self, env) -> None:
        store, settings, service = env
        tasks = TaskRunner(store, settings, service)
        now = 1_700_000_000_000
        self._run(store, "a", start=now)
        self._run(store, "b", start=now + 10 * HOUR)

        with pytest.raises(ValueError, match="区间不一致"):
            tasks.compare(["a", "b"])

    async def test_compare_rejects_unfinished_run(self, env) -> None:
        store, settings, service = env
        tasks = TaskRunner(store, settings, service)
        now = 1_700_000_000_000
        self._run(store, "a", start=now)
        self._run(store, "b", start=now, status="failed")

        with pytest.raises(ValueError, match="已完成的回测"):
            tasks.compare(["a", "b"])

    async def test_compare_rejects_too_many(self, env) -> None:
        store, settings, service = env
        tasks = TaskRunner(store, settings, service)

        with pytest.raises(ValueError, match="at most"):
            tasks.compare([f"r{i}" for i in range(9)])


class TestRecover:
    async def test_interrupted_runs_marked_failed(self, env) -> None:
        store, settings, service = env
        tasks = TaskRunner(store, settings, service)
        now = 1_700_000_000_000
        for run_id, status in (("r1", RUNNING), ("r2", PENDING), ("r3", DONE)):
            store.create_backtest_run(
                run_id,
                strategy_id="sma_cross",
                inst_id=INST,
                bar="1H",
                start_ts=now,
                end_ts=now + HOUR,
                params="{}",
                kind="backtest",
                status=status,
                created_at=now,
            )

        count = await tasks.recover()

        assert count == 2
        assert store.get_backtest_run("r1")["error"] == "interrupted"
        assert store.get_backtest_run("r2")["status"] == FAILED
        assert store.get_backtest_run("r3")["status"] == DONE


class TestFailureAlert:
    async def test_artifact_error_publishes_alert(self, env, monkeypatch) -> None:
        """M9-11：回测失败进告警，带 run_id 对应的策略与品种。"""
        store, settings, service = env
        alerts = _RecordingAlerts()
        tasks = TaskRunner(store, settings, service, alerts=alerts)

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(tasks, "_write_artifacts", boom)
        run_id = await tasks.submit(_request())

        detail = await _drain(tasks, run_id)
        await tasks.close()

        assert detail["status"] == FAILED
        assert [event.rule for event in alerts.events] == ["backtest_failed"]
        assert alerts.events[0].level == "ERROR"
        assert alerts.events[0].inst_id == INST
        assert alerts.events[0].strategy_id == "sma_cross"


class _RecordingAlerts:
    """记录发布出去的告警，不落库也不发信。"""

    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> str:
        self.events.append(event)
        return f"id-{len(self.events)}"


class _SlowService:
    """跑到取消为止的假服务，用来验证 running 状态下的取消。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.saw_stop = False

    def check(self, req) -> dict:
        return {}

    async def build_async(self, req, should_stop=None):
        self.started.set()
        while not (should_stop and should_stop()):
            await asyncio.sleep(0.005)
        self.saw_stop = True
        return {}, None
