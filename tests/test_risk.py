"""风控规则单元测试（spec §10）。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quant.backtest.order import Fill
from quant.backtest.position import Position
from quant.backtest.signal import Signal
from quant.risk.checker import RiskChecker, RiskDecision
from quant.risk.config import RiskConfig, load_risk_config
from quant.risk.pnl import round_trip_pnls
from quant.state.sqlite_store import SQLiteStore

_TS = 1_700_000_000_000


def _signal(
    side: str = "buy",
    qty: str = "1",
    inst_id: str = "BTC-USDT",
) -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side=side,  # type: ignore[arg-type]
        order_type="market",
        inst_id=inst_id,
        qty=Decimal(qty),
        ts=_TS,
        signal_id="sig-1",
    )


def _fill(side: str, price: str, ts: int = _TS, fee: str = "0") -> Fill:
    return Fill(
        order_id=f"o-{ts}-{side}",
        inst_id="BTC-USDT",
        side=side,
        qty=Decimal("1"),
        price=Decimal(price),
        fee=Decimal(fee),
        ts=ts,
    )


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "quant.db")
    yield s
    s.close()


def _config(**overrides) -> RiskConfig:
    defaults = {
        "strategy_id": "sma_cross",
        "max_position_pct": Decimal("0.3"),
        "max_total_pct": Decimal("0.8"),
        "max_order_qty": Decimal("10000"),
        "daily_loss_limit": Decimal("0.05"),
        "consecutive_loss_limit": 3,
    }
    defaults.update(overrides)
    return RiskConfig(**defaults)


# =====================================================================
# Config loading
# =====================================================================


class TestRiskConfig:
    def test_load_from_repo_config(self) -> None:
        config = load_risk_config("sma_cross")
        assert config.strategy_id == "sma_cross"
        assert config.max_position_pct == Decimal("0.3")
        assert config.consecutive_loss_limit == 3

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_risk_config("nope", tmp_path)

    def test_missing_risk_section_raises(self, tmp_path: Path) -> None:
        (tmp_path / "x.toml").write_text('[strategy]\nstrategy_id = "x"\n')
        with pytest.raises(ValueError, match="missing \\[risk\\]"):
            load_risk_config("x", tmp_path)

    def test_strategy_id_mismatch_raises(self, tmp_path: Path) -> None:
        (tmp_path / "x.toml").write_text(
            '[strategy]\nstrategy_id = "y"\n[risk]\n'
        )
        with pytest.raises(ValueError, match="mismatch"):
            load_risk_config("x", tmp_path)


# =====================================================================
# Rules
# =====================================================================


class TestApproval:
    def test_normal_buy_approved(self, store) -> None:
        checker = RiskChecker(_config(), store)
        decision = checker.check(_signal(qty="1"), market_price=Decimal("100"))

        assert decision.approved is True
        assert decision.adjusted_qty == Decimal("1")
        assert decision.reason == ""

    def test_unknown_price_skips_notional_rules(self, store) -> None:
        """无市价且无持仓 → 无法计算名义价值，放行。"""
        checker = RiskChecker(_config(), store)
        decision = checker.check(_signal(qty="999"))

        assert decision.approved is True
        assert decision.adjusted_qty == Decimal("999")


class TestMaxOrderQty:
    def test_adjusts_qty_down(self, store) -> None:
        checker = RiskChecker(_config(max_order_qty=Decimal("1000")), store)
        decision = checker.check(_signal(qty="20"), market_price=Decimal("100"))

        assert decision.approved is True
        assert decision.adjusted_qty == Decimal("10")

    def test_under_limit_unchanged(self, store) -> None:
        checker = RiskChecker(_config(max_order_qty=Decimal("1000")), store)
        decision = checker.check(_signal(qty="5"), market_price=Decimal("100"))

        assert decision.adjusted_qty == Decimal("5")


class TestMaxPositionPct:
    def test_adjusts_qty_down(self, store) -> None:
        """equity=10000, 上限 30% = 3000；price=100 → 最多 30 个。"""
        checker = RiskChecker(_config(), store)
        decision = checker.check(_signal(qty="50"), market_price=Decimal("100"))

        assert decision.approved is True
        assert decision.adjusted_qty == Decimal("30")

    def test_rejects_when_already_at_limit(self, store) -> None:
        store.update_position(
            Position(
                inst_id="BTC-USDT", qty=Decimal("30"), avg_price=Decimal("100")
            )
        )
        checker = RiskChecker(_config(), store)
        decision = checker.check(_signal(qty="1"), market_price=Decimal("100"))

        assert decision.approved is False
        assert "position limit" in decision.reason

    def test_sell_reducing_position_allowed(self, store) -> None:
        store.update_position(
            Position(
                inst_id="BTC-USDT", qty=Decimal("30"), avg_price=Decimal("100")
            )
        )
        checker = RiskChecker(_config(), store)
        decision = checker.check(
            _signal(side="sell", qty="10"), market_price=Decimal("100")
        )

        assert decision.approved is True
        assert decision.adjusted_qty == Decimal("10")

    def test_equity_override_raises_limit(self, store) -> None:
        """传入真实账户权益后按权益算上限，不再用 initial_equity 兜底。"""
        checker = RiskChecker(
            _config(max_order_qty=Decimal("100000")), store
        )
        decision = checker.check(
            _signal(qty="150"),
            market_price=Decimal("100"),
            equity=Decimal("50000"),
        )

        assert decision.approved is True
        assert decision.adjusted_qty == Decimal("150")

    def test_invalid_equity_falls_back(self, store) -> None:
        """快照缺失时 provider 传 None/0，仍退回构造时的权益。"""
        checker = RiskChecker(_config(), store)
        decision = checker.check(
            _signal(qty="50"),
            market_price=Decimal("100"),
            equity=Decimal("0"),
        )

        assert decision.adjusted_qty == Decimal("30")


class TestMaxTotalPct:
    def test_rejects_when_total_exceeded(self, store) -> None:
        """其他品种已占 75%，总上限 80%，本品种再加 10% → 拒绝或下调。"""
        store.update_position(
            Position(
                inst_id="ETH-USDT", qty=Decimal("75"), avg_price=Decimal("100")
            )
        )
        checker = RiskChecker(_config(), store)
        decision = checker.check(_signal(qty="10"), market_price=Decimal("100"))

        assert decision.approved is True
        assert decision.adjusted_qty == Decimal("5")

    def test_rejects_when_no_room(self, store) -> None:
        store.update_position(
            Position(
                inst_id="ETH-USDT", qty=Decimal("80"), avg_price=Decimal("100")
            )
        )
        checker = RiskChecker(_config(), store)
        decision = checker.check(_signal(qty="1"), market_price=Decimal("100"))

        assert decision.approved is False
        assert "total position limit" in decision.reason


class TestDailyLossLimit:
    def test_approves_when_profitable(self, store) -> None:
        store.save_fill(_fill("buy", "100", ts=_TS))
        store.save_fill(_fill("sell", "400", ts=_TS + 1))

        checker = RiskChecker(_config(daily_loss_limit=Decimal("0.01")), store)
        assert checker.check(_signal(), market_price=Decimal("100")).approved

    def test_reject_with_realized_loss(self, store) -> None:
        store.save_fill(_fill("buy", "600", ts=_TS))
        store.save_fill(_fill("sell", "100", ts=_TS + 1))
        # 已实现亏损 500；equity 10000 * 5% = 500 → 触发
        checker = RiskChecker(_config(), store)
        decision = checker.check(_signal(), market_price=Decimal("100"))

        assert decision.approved is False
        assert "daily loss" in decision.reason
        assert decision.halt is True

    def test_ignore_loss_before_today(self, store) -> None:
        store.save_fill(_fill("buy", "600", ts=1000))
        store.save_fill(_fill("sell", "100", ts=2000))

        checker = RiskChecker(_config(), store)
        decision = checker.check(
            _signal(), market_price=Decimal("100"), today_start_ms=3000
        )
        assert decision.approved is True


class TestConsecutiveLosses:
    def test_rejects_after_three_losses(self, store) -> None:
        ts = _TS
        for i in range(3):
            store.save_fill(_fill("buy", "100", ts=ts + i * 10))
            store.save_fill(_fill("sell", "90", ts=ts + i * 10 + 1))

        checker = RiskChecker(_config(consecutive_loss_limit=3), store)
        decision = checker.check(_signal(), market_price=Decimal("100"))

        assert decision.approved is False
        assert "consecutive loss" in decision.reason
        assert decision.halt is True

    def test_win_breaks_streak(self, store) -> None:
        ts = _TS
        # 亏、亏、赢、亏、亏 → 最近连续亏损 2 < 3
        for i, sell_price in enumerate(["90", "90", "110", "90", "90"]):
            store.save_fill(_fill("buy", "100", ts=ts + i * 10))
            store.save_fill(_fill("sell", sell_price, ts=ts + i * 10 + 1))

        checker = RiskChecker(_config(consecutive_loss_limit=3), store)
        decision = checker.check(_signal(), market_price=Decimal("100"))

        assert decision.approved is True


class TestDecisionType:
    def test_decision_is_frozen(self, store) -> None:
        decision = RiskDecision(
            approved=True, adjusted_qty=Decimal("1")
        )
        with pytest.raises(AttributeError):
            decision.approved = False  # type: ignore[misc]


class TestRoundTripPnls:
    def test_instruments_do_not_cross_match(self) -> None:
        def fill(inst_id: str, side: str, price: str, ts: int) -> Fill:
            return Fill(
                order_id=f"o-{ts}",
                inst_id=inst_id,
                side=side,
                qty=Decimal("1"),
                price=Decimal(price),
                fee=Decimal("0"),
                ts=ts,
            )

        fills = [
            fill("BTC-USDT", "buy", "100", 1),
            fill("BTC-USDT-SWAP", "buy", "200", 2),
            fill("BTC-USDT-SWAP", "sell", "210", 3),
            fill("BTC-USDT", "sell", "90", 4),
        ]

        # 现货与合约各自配对；结果按平仓时间排序，而不是按品种分组顺序。
        assert round_trip_pnls(fills) == [Decimal("10"), Decimal("-10")]
