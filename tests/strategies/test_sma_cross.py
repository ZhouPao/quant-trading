"""SMA Cross 策略单元测试（spec §5.3）。

覆盖：
- 参数校验、窗口不足不产生信号
- 金叉买入 / 死叉平多、持仓状态下不重复开仓
- 下单量预留手续费、引擎集成回测
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.context import Context
from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.position import Position
from quant.models.bar import Bar
from quant.strategies.python.sma_cross import SmaCrossStrategy

_TS0 = 1_700_000_000_000
_HOUR = 3_600_000


def _bars(closes: list[str]) -> list[Bar]:
    bars = []
    for i, close in enumerate(closes):
        price = Decimal(close)
        bars.append(
            Bar(
                ts=_TS0 + i * _HOUR,
                open=price,
                high=price,
                low=price,
                close=price,
                vol=Decimal("1"),
                vol_quote=Decimal("1"),
                confirm=1,
                inst_id="BTC-USDT",
                bar_size="1H",
            )
        )
    return bars


def _ctx(
    params: dict | None = None,
    position: Position | None = None,
    cash: str = "10000",
) -> Context:
    return Context(
        position=position if position is not None else Position(inst_id="BTC-USDT"),
        params=params or {"fast_len": 2, "slow_len": 3},
        cash=Decimal(cash),
    )


def _feed(strategy: SmaCrossStrategy, ctx: Context, bars: list[Bar]):
    signals = []
    for bar in bars:
        ctx.bars_window.append(bar)
        signal = strategy.on_bar(bar, ctx)
        if signal is not None:
            signals.append(signal)
    return signals


class TestSmaCrossInit:
    def test_strategy_id(self) -> None:
        strategy = SmaCrossStrategy()
        strategy.on_init(_ctx())
        assert strategy.strategy_id == "sma_cross"

    def test_fast_must_be_less_than_slow(self) -> None:
        strategy = SmaCrossStrategy()
        with pytest.raises(ValueError, match="fast_len"):
            strategy.on_init(_ctx({"fast_len": 5, "slow_len": 5}))

    def test_non_positive_period_raises(self) -> None:
        strategy = SmaCrossStrategy()
        with pytest.raises(ValueError, match="positive"):
            strategy.on_init(_ctx({"fast_len": 0, "slow_len": 3}))


class TestSmaCrossSignals:
    def test_insufficient_window_no_signal(self) -> None:
        """slow_len=3 需要 4 根窗口，前 3 根不产生信号。"""
        strategy = SmaCrossStrategy()
        ctx = _ctx()
        strategy.on_init(ctx)
        signals = _feed(strategy, ctx, _bars(["10", "10", "10"]))

        assert signals == []

    def test_golden_cross_buy(self) -> None:
        """10,10,10,9,12：第 5 根快线上穿慢线 → 买入信号。"""
        strategy = SmaCrossStrategy()
        ctx = _ctx()
        strategy.on_init(ctx)
        bars = _bars(["10", "10", "10", "9", "12"])
        signals = _feed(strategy, ctx, bars)

        assert len(signals) == 1
        signal = signals[0]
        assert signal.side == "buy"
        assert signal.order_type == "market"
        assert signal.inst_id == "BTC-USDT"
        assert signal.ts == bars[-1].ts
        assert signal.qty > 0

    def test_no_buy_when_already_long(self) -> None:
        """已持多时金叉不再加仓（对应 Pine pyramiding=0）。"""
        strategy = SmaCrossStrategy()
        position = Position(
            inst_id="BTC-USDT", qty=Decimal("1"), avg_price=Decimal("10")
        )
        ctx = _ctx(position=position)
        strategy.on_init(ctx)
        signals = _feed(strategy, ctx, _bars(["10", "10", "10", "9", "12"]))

        # 金叉出现在末根，但已持多 → 不产生买入信号
        assert all(s.side != "buy" for s in signals)

    def test_death_cross_sell_all(self) -> None:
        """10,10,10,10,12,9,8：末根快线下穿慢线 → 卖出全部持仓。"""
        strategy = SmaCrossStrategy()
        position = Position(
            inst_id="BTC-USDT", qty=Decimal("2"), avg_price=Decimal("10")
        )
        ctx = _ctx(position=position)
        strategy.on_init(ctx)
        bars = _bars(["10", "10", "10", "10", "12", "9", "8"])
        signals = _feed(strategy, ctx, bars)

        assert len(signals) == 1
        assert signals[0].side == "sell"
        assert signals[0].qty == Decimal("2")

    def test_flat_market_no_signal(self) -> None:
        strategy = SmaCrossStrategy()
        ctx = _ctx()
        strategy.on_init(ctx)
        signals = _feed(strategy, ctx, _bars(["10", "10", "10", "10", "10"]))

        assert signals == []

    def test_qty_reserves_commission(self) -> None:
        """下单量应使 qty*price*(1+rate) <= cash。"""
        strategy = SmaCrossStrategy()
        ctx = _ctx(params={"fast_len": 2, "slow_len": 3, "commission_rate": "0.001"})
        strategy.on_init(ctx)
        bars = _bars(["10", "10", "10", "9", "12"])
        signals = _feed(strategy, ctx, bars)

        assert len(signals) == 1
        cost = signals[0].qty * bars[-1].close * Decimal("1.001")
        assert cost <= Decimal("10000")


class TestSmaCrossBacktest:
    def test_full_round_trip(self) -> None:
        """金叉买入 → 死叉卖出，验证引擎集成与已实现盈亏。"""
        closes = ["10", "10", "10", "10", "12", "9", "8", "8"]
        bars = _bars(closes)
        strategy = SmaCrossStrategy()
        result = Backtest(
            strategy,
            Matcher(commission_rate=Decimal("0")),
            initial_cash=Decimal("1000"),
            params={"fast_len": 2, "slow_len": 3, "commission_rate": "0"},
        ).run(bars)

        assert len(result.trades) == 2
        assert result.trades[0].side == "buy"
        assert result.trades[1].side == "sell"
        assert result.final_position.is_flat
        # 买入价 9（第 6 根开盘），卖出价 8（第 8 根开盘）→ 亏损
        assert result.final_position.realized_pnl < 0
