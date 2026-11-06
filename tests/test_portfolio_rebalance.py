"""M6-5/6/7/8/9（8-3）：组合风控、再平衡、调仓单与失败回滚。"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from quant.backtest.order import Order
from quant.backtest.signal import Signal
from quant.portfolio import (
    PortfolioConfig,
    PortfolioRebalanceService,
    PortfolioRisk,
    PortfolioSnapshot,
    PortfolioUnit,
    RebalanceConfig,
    RebalanceError,
    Rebalancer,
    RiskAction,
)
from quant.state.sqlite_store import SCHEMA_VERSION, SQLiteStore

_A = "a:BTC-USDT"
_B = "b:ETH-USDT"
_T0 = 1_700_000_000_000
_HOUR = 3_600_000
_DAY = 86_400_000


def _config(**kwargs: object) -> PortfolioConfig:
    base: dict[str, object] = {
        "portfolio_id": "core",
        "units": (
            PortfolioUnit("a", "BTC-USDT"),
            PortfolioUnit("b", "ETH-USDT"),
        ),
    }
    base.update(kwargs)
    return PortfolioConfig(**base)  # type: ignore[arg-type]


def _snapshot(
    ts: int = _T0,
    a: Decimal = Decimal("5000"),
    b: Decimal = Decimal("5000"),
    drawdown: Decimal = Decimal("0"),
) -> PortfolioSnapshot:
    equity = a + b
    return PortfolioSnapshot(
        portfolio_id="core",
        ts=ts,
        equity=equity,
        unit_equity={_A: a, _B: b},
        weights={_A: a / equity, _B: b / equity},
        drawdown=drawdown,
    )


def _signal(signal_id: str = "pf-1", source: str = "portfolio") -> Signal:
    return Signal(
        strategy_id="a",
        side="buy",
        order_type="market",
        inst_id="BTC-USDT",
        qty=None,
        ts=_T0,
        signal_id=signal_id,
        size_pct=Decimal("0.1"),
        source=source,
    )


class _Queue:
    def __init__(self, fail_insts: tuple[str, ...] = ()) -> None:
        self.pushed: list[Signal] = []
        self.fail_insts = set(fail_insts)

    async def push(self, signal: Signal) -> None:
        if signal.inst_id in self.fail_insts:
            raise RuntimeError("queue down")
        self.pushed.append(signal)


class TestSourceColumn:
    def test_fresh_db_v16_and_columns(self, tmp_path: Path) -> None:
        path = tmp_path / "quant.db"
        SQLiteStore(path).close()
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 16
            for table in ("signals", "orders"):
                columns = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                assert "source" in columns
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "portfolio_rebalance_attempts" in tables
        finally:
            conn.close()

    def test_v13_db_upgrades_and_keeps_old_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "quant.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE signals (
                signal_id TEXT PRIMARY KEY, strategy_id TEXT, side TEXT,
                order_type TEXT, inst_id TEXT, qty TEXT, price TEXT,
                ts INTEGER, status TEXT, created_at INTEGER);
            INSERT INTO signals VALUES ('s-old','a','buy','market','BTC-USDT',
                '1',NULL,1,'executed',1);
            """
        )
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
        conn.close()

        store = SQLiteStore(path)
        store.close()
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
            row = conn.execute(
                "SELECT status, source FROM signals WHERE signal_id='s-old'"
            ).fetchone()
            assert row == ("executed", "webhook")
        finally:
            conn.close()

    def test_signal_source_roundtrip(self) -> None:
        signal = _signal()
        assert Signal.from_json(signal.to_json()) == signal
        assert Signal.from_json(
            {k: v for k, v in signal.to_json().items() if k != "source"}
        ).source == "webhook"
        with pytest.raises(ValueError, match="source"):
            _signal(source="robot")

    def test_save_signal_and_order_persist_source(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            signal = _signal()
            store.save_signal(signal, "pending")
            assert store.get_signal("pf-1") is not None
            assert store.get_signal("pf-1")["source"] == "portfolio"
            store.save_order(
                Order(
                    order_id="o-1",
                    signal=signal,
                    status="pending",
                    created_ts=_T0,
                )
            )
            row = store._query_one(
                "SELECT source FROM orders WHERE order_id='o-1'", ()
            )
            assert row is not None and row["source"] == "portfolio"
        finally:
            store.close()


class TestPortfolioRisk:
    def test_drawdown_reduces_all_units(self) -> None:
        actions = PortfolioRisk(_config()).evaluate(
            _snapshot(drawdown=Decimal("0.20"))
        )
        assert {a.unit_key for a in actions} == {_A, _B}
        assert all(a.reason == "portfolio_drawdown" for a in actions)

    def test_unit_contribution_and_correlation(self) -> None:
        actions = PortfolioRisk(_config()).evaluate(
            _snapshot(a=Decimal("9000"), b=Decimal("1000"))
        )
        assert any(a.unit_key == _A and a.reason == "unit_contribution" for a in actions)

        actions = PortfolioRisk(
            _config(), correlations={(_A, _B): Decimal("0.95")}
        ).evaluate(_snapshot())
        assert {a.unit_key for a in actions} == {_A, _B}
        assert all(a.reason == "unit_correlation" for a in actions)

    def test_no_risk_returns_empty(self) -> None:
        assert PortfolioRisk(_config()).evaluate(_snapshot()) == []

    def test_apply_pause_and_renormalize(self) -> None:
        adjusted = PortfolioRisk.apply(
            {_A: Decimal("0.5"), _B: Decimal("0.5")},
            [RiskAction(_B, "pause", "test")],
        )
        assert adjusted[_A] == Decimal("1")
        assert adjusted[_B] == Decimal("0")

        reduced = PortfolioRisk.apply(
            {_A: Decimal("0.5"), _B: Decimal("0.5")},
            [RiskAction(_A, "reduce", "test", Decimal("0.5"))],
        )
        assert sum(reduced.values(), Decimal("0")) == Decimal("1")
        assert reduced[_A] < reduced[_B]


class TestRebalancer:
    def test_calendar_first_run_triggers(self) -> None:
        # 显式关闭风控建议，单独验证再平衡口径（80/20 -> 50/50）
        plan = Rebalancer(_config()).evaluate(
            _snapshot(a=Decimal("8000"), b=Decimal("2000")),
            risk_actions=[],
        )
        assert plan is not None
        assert plan.reason == "calendar"
        deltas = plan.delta_by_key()
        assert deltas[_A].side == "sell"
        assert deltas[_B].side == "buy"
        assert deltas[_A].amount == Decimal("-3000")
        assert deltas[_B].amount == Decimal("3000")

    def test_default_risk_actions_reduce_concentrated_unit(self) -> None:
        """默认会把组合风控建议应用到目标权重（M6-5 接线证据）。"""
        plan = Rebalancer(_config()).evaluate(
            _snapshot(a=Decimal("8000"), b=Decimal("2000"))
        )
        assert plan is not None
        # a 占 80% 超过单单元贡献度上限 50%，目标权重被降权后低于 0.5
        assert plan.delta_by_key()[_A].target_weight < Decimal("0.5")

    def test_cooldown_blocks_trigger(self) -> None:
        cfg = _config(
            rebalance=RebalanceConfig(
                mode="calendar", interval_hours=1, cooldown_hours=6
            )
        )
        plan = Rebalancer(cfg).evaluate(
            _snapshot(ts=_T0 + _DAY),
            last_rebalance_ts=_T0 + _DAY - _HOUR,
        )
        assert plan is None

    def test_threshold_mode_needs_drift(self) -> None:
        cfg = _config(rebalance=RebalanceConfig(mode="threshold", cooldown_hours=0))
        assert Rebalancer(cfg).evaluate(_snapshot()) is None
        plan = Rebalancer(cfg).evaluate(
            _snapshot(a=Decimal("8000"), b=Decimal("2000"))
        )
        assert plan is not None and plan.reason == "threshold"

    def test_both_mode_triggers_on_either(self) -> None:
        cfg = _config(rebalance=RebalanceConfig(mode="both", cooldown_hours=0))
        plan = Rebalancer(cfg).evaluate(_snapshot(), last_rebalance_ts=_T0 - _HOUR)
        # 权重已等于目标、日历也未到期 -> 无动作
        assert plan is None

    def test_dust_delta_returns_none(self) -> None:
        plan = Rebalancer(_config(), min_delta_pct=Decimal("1")).evaluate(
            _snapshot(a=Decimal("8000"), b=Decimal("2000"))
        )
        assert plan is None

    def test_risk_pause_changes_target(self) -> None:
        plan = Rebalancer(_config()).evaluate(
            _snapshot(), risk_actions=[RiskAction(_B, "pause", "test")]
        )
        assert plan is not None
        deltas = plan.delta_by_key()
        assert deltas[_B].target_weight == Decimal("0")
        assert deltas[_B].side == "sell"

    def test_to_signals_are_standard_portfolio_signals(self) -> None:
        plan = Rebalancer(_config()).evaluate(
            _snapshot(a=Decimal("8000"), b=Decimal("2000"))
        )
        assert plan is not None
        signals = Rebalancer.to_signals(plan)
        assert len(signals) == 2
        assert all(s.source == "portfolio" for s in signals)
        assert all(s.qty is None and s.size_pct is not None for s in signals)
        assert all(s.size_pct > 0 for s in signals)
        assert all(s.order_type == "market" for s in signals)
        # 同一计划重复生成 signal_id 不变（幂等）
        assert [s.signal_id for s in signals] == [
            s.signal_id for s in Rebalancer.to_signals(plan)
        ]


class TestRebalanceService:
    @pytest.mark.asyncio
    async def test_disabled_rejects_submission(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            plan = Rebalancer(_config()).evaluate(
                _snapshot(a=Decimal("8000"), b=Decimal("2000"))
            )
            assert plan is not None
            service = PortfolioRebalanceService(
                _config(), store, _Queue(), enabled=False
            )
            with pytest.raises(RebalanceError, match="enabled=false"):
                await service.submit(plan)
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_submit_uses_m8_queue_and_leaves_audit(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            plan = Rebalancer(_config()).evaluate(
                _snapshot(a=Decimal("8000"), b=Decimal("2000"))
            )
            assert plan is not None
            queue = _Queue()
            service = PortfolioRebalanceService(
                _config(), store, queue, enabled=True
            )
            results = await service.submit(plan)
            assert [r.status for r in results] == ["submitted", "submitted"]
            assert len(queue.pushed) == 2
            assert all(s.source == "portfolio" for s in queue.pushed)
            rows = store.get_rebalance_attempts("core", plan.ts)
            assert len(rows) == 2
            assert all(row["status"] == "submitted" for row in rows)
            assert store.get_signal(plan.deltas[0].signal_id) is not None
            assert store.get_signal(plan.deltas[0].signal_id)["source"] == "portfolio"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_partial_failure_never_retries_same_plan(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            plan = Rebalancer(_config()).evaluate(
                _snapshot(a=Decimal("8000"), b=Decimal("2000"))
            )
            assert plan is not None
            queue = _Queue(fail_insts=("ETH-USDT",))
            service = PortfolioRebalanceService(
                _config(), store, queue, enabled=True
            )
            first = await service.submit(plan)
            statuses = {r.unit_key: r.status for r in first}
            assert statuses[_A] == "submitted"
            assert statuses[_B] == "failed"
            assert next(r for r in first if r.unit_key == _B).reason is not None

            # 同计划再提交：成功与失败都不追单
            second = await service.submit(plan)
            assert all(r.status == "skipped" for r in second)
            assert len(queue.pushed) == 1

            # 下一周期新 plan_ts 重新评估，signal_id 随之更新
            later = Rebalancer(_config()).evaluate(
                _snapshot(ts=_T0 + _DAY, a=Decimal("8000"), b=Decimal("2000"))
            )
            assert later is not None
            assert later.deltas[0].signal_id != plan.deltas[0].signal_id
            third = await service.submit(later)
            assert all(r.status in ("submitted", "failed") for r in third)
        finally:
            store.close()
