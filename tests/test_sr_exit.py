"""支撑/压力止盈止损测试。

覆盖：开仓冻结、bar 内触发、止损优先、跳空成交、无压力只止损、
无支撑不开仓，以及回测端到端价位。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.context import Context
from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.position import Position
from quant.backtest.signal import Signal
from quant.backtest.strategy import Strategy
from quant.models.bar import Bar
from quant.strategies.python.sma_cross import SmaCrossStrategy
from quant.strategies.python.sr_exit import SrExitMixin


def bar(
    ts: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    inst_id: str = "BTC-USDT",
) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        vol=Decimal("1"),
        vol_quote=Decimal("100"),
        confirm=1,
        inst_id=inst_id,
        bar_size="1H",
    )


class ScriptedStrategy(SrExitMixin, Strategy):
    """在指定 bar 买入，并冻结构造时给定的止损/止盈。"""

    def __init__(self, buy_ts: int, stop: Decimal | None, target: Decimal | None) -> None:
        self.buy_ts = buy_ts
        self.stop = stop
        self.target = target

    @property
    def strategy_id(self) -> str:
        return "scripted"

    def on_init(self, ctx: Context) -> None:
        self._sr_init(ctx)

    def on_bar(self, bar: Bar, ctx: Context) -> Signal | None:
        if bar.ts == self.buy_ts and ctx.position.is_flat:
            self._sr_stop = self.stop
            self._sr_target = self.target
            return Signal(
                strategy_id=self.strategy_id,
                side="buy",
                order_type="market",
                inst_id=bar.inst_id,
                qty=Decimal("1"),
                ts=bar.ts,
            )
        return None


def run(strategy: Strategy, bars: list[Bar], **kwargs) -> object:
    params = {"sr_enabled": True, **kwargs}
    return Backtest(
        strategy,
        Matcher(slippage_rate=Decimal("0"), commission_rate=Decimal("0")),
        initial_cash=Decimal("10000"),
        params=params,
    ).run(bars)


SIGNAL_BAR = bar(1000, "100", "101", "99", "100")
ENTRY_BAR = bar(2000, "100", "101", "99.5", "100.5")


class TestProtectiveExit:
    def test_stop_has_priority_when_both_touched(self) -> None:
        # 同一根 bar 的 low 触及止损、high 触及止盈 → 按止损成交
        exit_bar = bar(3000, "100.5", "103", "99", "101")
        result = run(
            ScriptedStrategy(SIGNAL_BAR.ts, Decimal("99"), Decimal("102")),
            [SIGNAL_BAR, ENTRY_BAR, exit_bar],
        )
        sells = [trade for trade in result.trades if trade.side == "sell"]
        assert len(sells) == 1
        assert sells[0].price == Decimal("99")
        assert sells[0].ts == exit_bar.ts

    def test_target_fills_when_only_target_touched(self) -> None:
        exit_bar = bar(3000, "100.5", "103", "100.2", "102")
        result = run(
            ScriptedStrategy(SIGNAL_BAR.ts, Decimal("99"), Decimal("102")),
            [SIGNAL_BAR, ENTRY_BAR, exit_bar],
        )
        sells = [trade for trade in result.trades if trade.side == "sell"]
        assert len(sells) == 1
        assert sells[0].price == Decimal("102")

    def test_no_exit_when_levels_untouched(self) -> None:
        quiet = bar(3000, "100.5", "101.5", "100.2", "101")
        result = run(
            ScriptedStrategy(SIGNAL_BAR.ts, Decimal("99"), Decimal("102")),
            [SIGNAL_BAR, ENTRY_BAR, quiet],
        )
        assert [trade.side for trade in result.trades] == ["buy"]

    def test_gap_down_fills_at_open(self) -> None:
        # 开盘直接跳到止损下方，按开盘价成交（比止损更差）
        gap = bar(3000, "98", "100", "97.5", "99")
        result = run(
            ScriptedStrategy(SIGNAL_BAR.ts, Decimal("99"), Decimal("102")),
            [SIGNAL_BAR, ENTRY_BAR, gap],
        )
        sells = [trade for trade in result.trades if trade.side == "sell"]
        assert sells[0].price == Decimal("98")

    def test_gap_up_fills_at_open_for_target(self) -> None:
        gap = bar(3000, "105", "106", "104.5", "105")
        result = run(
            ScriptedStrategy(SIGNAL_BAR.ts, Decimal("99"), Decimal("102")),
            [SIGNAL_BAR, ENTRY_BAR, gap],
        )
        sells = [trade for trade in result.trades if trade.side == "sell"]
        assert sells[0].price == Decimal("105")

    def test_stop_only_without_target(self) -> None:
        exit_bar = bar(3000, "100.5", "103", "98", "99")
        result = run(
            ScriptedStrategy(SIGNAL_BAR.ts, Decimal("99"), None),
            [SIGNAL_BAR, ENTRY_BAR, exit_bar],
        )
        sells = [trade for trade in result.trades if trade.side == "sell"]
        assert sells[0].price == Decimal("99")

    def test_bar_after_exit_accepts_new_signal(self) -> None:
        exit_bar = bar(3000, "100.5", "101", "98", "99")
        result = run(
            ScriptedStrategy(SIGNAL_BAR.ts, Decimal("99"), Decimal("102")),
            [SIGNAL_BAR, ENTRY_BAR, exit_bar],
        )
        assert result.final_position.is_flat


class TestFreezeAndClear:
    def test_levels_cleared_after_exit(self) -> None:
        strategy = ScriptedStrategy(SIGNAL_BAR.ts, Decimal("99"), Decimal("102"))
        result = run(strategy, [SIGNAL_BAR, ENTRY_BAR, bar(3000, "100", "101", "98", "99")])
        assert result.final_position.is_flat
        assert strategy._sr_stop is None
        assert strategy._sr_target is None

    def test_disabled_by_default(self) -> None:
        strategy = ScriptedStrategy(SIGNAL_BAR.ts, Decimal("99"), Decimal("102"))
        crash = bar(3000, "100.5", "103", "90", "91")
        result = Backtest(
            strategy,
            Matcher(slippage_rate=Decimal("0"), commission_rate=Decimal("0")),
            initial_cash=Decimal("10000"),
        ).run([SIGNAL_BAR, ENTRY_BAR, crash])
        # 未启用时不检查档位、也不出场
        assert [trade.side for trade in result.trades] == ["buy"]
        ctx = Context(position=Position(inst_id="BTC-USDT"))
        assert strategy.protective_exit(crash, ctx) is None

    def test_invalid_params_rejected(self) -> None:
        strategy = ScriptedStrategy(SIGNAL_BAR.ts, None, None)
        ctx = Context(position=Position(inst_id="BTC-USDT"), params={"sr_left_right": 0})
        with pytest.raises(ValueError):
            strategy.on_init(ctx)


class TestEntryGate:
    def test_no_entry_without_support(self) -> None:
        strategy = SmaCrossStrategy()
        ctx = Context(
            position=Position(inst_id="BTC-USDT"),
            params={"sr_enabled": True, "sr_left_right": 2},
        )
        strategy.on_init(ctx)
        # 单调下跌没有摆动点 → 无任何档位，自然也没有支撑
        ctx.bars_window = [
            bar(
                1000 + index * 1000,
                str(200 - index),
                str(201 - index),
                str(199 - index),
                str(200 - index),
            )
            for index in range(10)
        ]
        stop, _ = strategy._sr_freeze(Decimal("191"), ctx)
        assert stop is None
        assert strategy._sr_allows_entry(stop) is False

    def test_entry_allowed_when_disabled(self) -> None:
        strategy = SmaCrossStrategy()
        ctx = Context(position=Position(inst_id="BTC-USDT"), params={})
        strategy.on_init(ctx)
        stop, target = strategy._sr_freeze(Decimal("100"), ctx)
        assert (stop, target) == (None, None)
        assert strategy._sr_allows_entry(stop) is True

    def test_plan_uses_confirmed_pivots_only(self) -> None:
        strategy = SmaCrossStrategy()
        ctx = Context(
            position=Position(inst_id="BTC-USDT"),
            params={"sr_enabled": True, "sr_left_right": 2},
        )
        strategy.on_init(ctx)
        path = [100, 105, 110, 105, 100, 105, 110, 105, 100, 105, 95, 90]
        ctx.bars_window = [
            bar(1000 + index * 1000, str(c), str(c + 1), str(c - 1), str(c))
            for index, c in enumerate(path)
        ]
        stop, target = strategy._sr_freeze(Decimal("100"), ctx)
        assert stop == Decimal("99")
        assert target == Decimal("111")
