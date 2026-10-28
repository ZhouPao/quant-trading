"""RSI 策略单元测试（spec §5.4）。

覆盖：
- 参数校验
- Wilder RSI 数值（与 Pine ta.rsi 对齐）
- 超卖买入 / 超买平仓 / 中位无信号
- 引擎集成回测
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.context import Context
from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.position import Position
from quant.models.bar import Bar
from quant.strategies.python.rsi import RsiStrategy

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
        params=params or {"rsi_period": 2, "oversold": 30, "overbought": 70},
        cash=Decimal(cash),
    )


def _feed(strategy: RsiStrategy, ctx: Context, bars: list[Bar]):
    signals = []
    for bar in bars:
        ctx.bars_window.append(bar)
        signal = strategy.on_bar(bar, ctx)
        if signal is not None:
            signals.append(signal)
    return signals


class TestRsiInit:
    def test_strategy_id(self) -> None:
        strategy = RsiStrategy()
        strategy.on_init(_ctx())
        assert strategy.strategy_id == "rsi"

    def test_non_positive_period_raises(self) -> None:
        strategy = RsiStrategy()
        with pytest.raises(ValueError, match="rsi_period"):
            strategy.on_init(_ctx({"rsi_period": 0}))

    def test_threshold_order_raises(self) -> None:
        strategy = RsiStrategy()
        with pytest.raises(ValueError, match="oversold"):
            strategy.on_init(_ctx({"oversold": 80, "overbought": 20}))


class TestRsiValues:
    def test_insufficient_data_returns_none(self) -> None:
        """period=2：前两根 bar 无法计算 RSI。"""
        strategy = RsiStrategy()
        ctx = _ctx()
        strategy.on_init(ctx)
        bars = _bars(["100", "102", "101"])
        _feed(strategy, ctx, bars[:2])
        assert strategy.last_rsi is None

    def test_wilder_smoothing_values(self) -> None:
        """100,102,101,103（period=2）→ 第 3 根 RSI=66.67，第 4 根 RSI=85.71。"""
        strategy = RsiStrategy()
        ctx = _ctx()
        strategy.on_init(ctx)
        _feed(strategy, ctx, _bars(["100", "102", "101"]))

        assert strategy.last_rsi is not None
        assert float(strategy.last_rsi) == pytest.approx(66.6667, abs=1e-3)

        strategy.on_bar(_bars(["100", "102", "101", "103"])[-1], ctx)
        assert strategy.last_rsi is not None
        assert float(strategy.last_rsi) == pytest.approx(85.7143, abs=1e-3)

    def test_all_gains_rsi_100(self) -> None:
        strategy = RsiStrategy()
        ctx = _ctx()
        strategy.on_init(ctx)
        _feed(strategy, ctx, _bars(["100", "101", "102"]))
        assert strategy.last_rsi == Decimal("100")

    def test_all_losses_rsi_zero(self) -> None:
        strategy = RsiStrategy()
        ctx = _ctx()
        strategy.on_init(ctx)
        _feed(strategy, ctx, _bars(["100", "99", "97"]))
        assert strategy.last_rsi == Decimal("0")


class TestRsiSignals:
    def test_oversold_buy(self) -> None:
        """RSI=0 < 30 → 空仓买入。"""
        strategy = RsiStrategy()
        ctx = _ctx()
        strategy.on_init(ctx)
        bars = _bars(["100", "99", "97"])
        signals = _feed(strategy, ctx, bars)

        assert len(signals) == 1
        assert signals[0].side == "buy"
        assert signals[0].ts == bars[-1].ts
        assert signals[0].qty > 0

    def test_overbought_sell_when_long(self) -> None:
        """RSI=100 > 70 且持多 → 全部平仓。"""
        strategy = RsiStrategy()
        position = Position(
            inst_id="BTC-USDT", qty=Decimal("1"), avg_price=Decimal("100")
        )
        ctx = _ctx(position=position)
        strategy.on_init(ctx)
        signals = _feed(strategy, ctx, _bars(["100", "102", "105"]))

        assert len(signals) == 1
        assert signals[0].side == "sell"
        assert signals[0].qty == Decimal("1")

    def test_overbought_when_flat_no_signal(self) -> None:
        """超买但空仓时不做空（策略只做多）。"""
        strategy = RsiStrategy()
        ctx = _ctx()
        strategy.on_init(ctx)
        signals = _feed(strategy, ctx, _bars(["100", "102", "105"]))

        assert signals == []

    def test_mid_range_no_signal(self) -> None:
        """RSI=66.67 处于 [30,70] → 无信号。"""
        strategy = RsiStrategy()
        ctx = _ctx()
        strategy.on_init(ctx)
        signals = _feed(strategy, ctx, _bars(["100", "102", "101"]))

        assert signals == []

    def test_oversold_when_long_no_signal(self) -> None:
        """已持多且超卖时不加仓。"""
        strategy = RsiStrategy()
        position = Position(
            inst_id="BTC-USDT", qty=Decimal("1"), avg_price=Decimal("100")
        )
        ctx = _ctx(position=position)
        strategy.on_init(ctx)
        signals = _feed(strategy, ctx, _bars(["100", "99", "97"]))

        assert signals == []

    def test_qty_reserves_commission(self) -> None:
        strategy = RsiStrategy()
        ctx = _ctx(
            {"rsi_period": 2, "oversold": 30, "overbought": 70,
             "commission_rate": "0.001"}
        )
        strategy.on_init(ctx)
        bars = _bars(["100", "99", "97"])
        signals = _feed(strategy, ctx, bars)

        assert len(signals) == 1
        cost = signals[0].qty * bars[-1].close * Decimal("1.001")
        assert cost <= Decimal("10000")


class TestRsiBacktest:
    def test_oversold_buy_then_overbought_sell(self) -> None:
        """V 形行情：超卖买入 → 反弹超买卖出。"""
        closes = ["100", "99", "97", "99", "103", "108", "108"]
        bars = _bars(closes)
        result = Backtest(
            RsiStrategy(),
            Matcher(commission_rate=Decimal("0")),
            initial_cash=Decimal("1000"),
            params={
                "rsi_period": 2,
                "oversold": 30,
                "overbought": 70,
                "commission_rate": "0",
            },
        ).run(bars)

        assert len(result.trades) == 2
        assert result.trades[0].side == "buy"
        assert result.trades[1].side == "sell"
        assert result.final_position.is_flat
        assert result.final_position.realized_pnl > 0
