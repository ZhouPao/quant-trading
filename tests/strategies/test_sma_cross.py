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
from quant.strategies.python.sma_cross import SmaCrossStrategy, _atr_last, _ema_last

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


class TestEmaTrendFilter:
    def test_ema_seeded_by_sma_then_recursive(self) -> None:
        """1..5 取 4 周期：种子 (1+2+3+4)/4=2.5，k=0.4 → 3.5。"""
        closes = [Decimal(str(v)) for v in range(1, 6)]
        assert _ema_last(closes, 4) == Decimal("3.5")

    def test_ema_returns_none_when_not_enough_bars(self) -> None:
        assert _ema_last([Decimal("1"), Decimal("2")], 4) is None

    def test_filter_blocks_buy_below_ema(self) -> None:
        """金叉出现但收盘价仍在 EMA 下方 → 不开仓。"""
        strategy = SmaCrossStrategy()
        ctx = _ctx(
            {"fast_len": 2, "slow_len": 3, "ema_len": 4, "ema_filter": True}
        )
        strategy.on_init(ctx)
        signals = _feed(
            strategy, ctx, _bars(["100", "100", "100", "100", "50", "52", "54"])
        )

        assert signals == []

    def test_filter_allows_buy_above_ema(self) -> None:
        """同一组参数下收盘价站上 EMA → 正常买入。"""
        strategy = SmaCrossStrategy()
        ctx = _ctx(
            {"fast_len": 2, "slow_len": 3, "ema_len": 4, "ema_filter": True}
        )
        strategy.on_init(ctx)
        signals = _feed(strategy, ctx, _bars(["10", "10", "10", "10", "11"]))

        assert len(signals) == 1
        assert signals[0].side == "buy"

    def test_filter_does_not_block_exit(self) -> None:
        """死叉平仓不受趋势过滤影响（收盘价低于 EMA 也要平）。"""
        strategy = SmaCrossStrategy()
        position = Position(
            inst_id="BTC-USDT", qty=Decimal("1"), avg_price=Decimal("10")
        )
        ctx = _ctx(
            params={"fast_len": 2, "slow_len": 3, "ema_len": 4, "ema_filter": True},
            position=position,
        )
        strategy.on_init(ctx)
        signals = _feed(
            strategy, ctx, _bars(["10", "10", "10", "10", "12", "13", "10"])
        )

        assert [s.side for s in signals] == ["sell"]

    def test_filter_waits_for_ema_window(self) -> None:
        """ema_len 大于 slow_len 时，窗口不足不产生信号。"""
        strategy = SmaCrossStrategy()
        ctx = _ctx(
            {"fast_len": 2, "slow_len": 3, "ema_len": 10, "ema_filter": True}
        )
        strategy.on_init(ctx)
        signals = _feed(strategy, ctx, _bars(["10", "10", "10", "9", "12"]))

        assert signals == []


def _ohlc_bars(rows: list[tuple[str, str, str, str]]) -> list[Bar]:
    """按 (open, high, low, close) 构造 bar，用于 ATR 的波幅场景。"""
    bars = []
    for i, (o, h, low, c) in enumerate(rows):
        bars.append(
            Bar(
                ts=_TS0 + i * _HOUR,
                open=Decimal(o),
                high=Decimal(h),
                low=Decimal(low),
                close=Decimal(c),
                vol=Decimal("1"),
                vol_quote=Decimal("1"),
                confirm=1,
                inst_id="BTC-USDT",
                bar_size="1H",
            )
        )
    return bars


class TestAtr:
    def test_equals_last_period_true_ranges(self) -> None:
        """period=2 只看末 3 根：TR=[2.5, 1.5] → 均值 2.0。"""
        bars = _ohlc_bars(
            [
                ("10", "10", "10", "10"),
                ("10", "11", "10", "10.5"),
                ("12.5", "13", "12", "12.5"),
                ("13", "14", "13", "13.5"),
            ]
        )

        assert _atr_last(bars, 2) == Decimal("2.0")

    def test_gap_uses_previous_close(self) -> None:
        """跳空时 TR 取 |high-prev_close| 而非 high-low。"""
        bars = _ohlc_bars(
            [
                ("10", "10", "10", "10"),
                ("20", "20", "20", "20"),
            ]
        )

        # TR = max(0, |20-10|, |20-10|) = 10
        assert _atr_last(bars, 1) == Decimal("10")

    def test_returns_none_when_not_enough_bars(self) -> None:
        bars = _ohlc_bars([("10", "10", "10", "10"), ("10", "11", "10", "10.5")])

        assert _atr_last(bars, 5) is None

    def test_only_last_window_is_used(self) -> None:
        """更早的极端波幅不应影响结果（窗口外数据不参与）。"""
        noisy = _ohlc_bars([("1", "1000", "1", "500")])
        target = _ohlc_bars([("10", "10", "10", "10"), ("10", "11", "10", "10.5")])

        assert _atr_last(noisy + target, 1) == _atr_last(target, 1)


class TestAtrBand:
    def _ctx(self, **params) -> Context:
        base = {"fast_len": 2, "slow_len": 3}
        base.update(params)
        return _ctx(base)

    def test_band_blocks_close_just_above_ema(self) -> None:
        """收盘刚站上 EMA，但远未越过 EMA + k×ATR → 不放行。"""
        bars = _ohlc_bars(
            [
                ("100", "100", "100", "100"),
                ("100", "100", "100", "100"),
                ("100", "200", "1", "101"),
            ]
        )
        plain = SmaCrossStrategy()
        ctx = self._ctx(ema_len=2, ema_filter=True, atr_len=2)
        plain.on_init(ctx)
        banded = SmaCrossStrategy()
        bctx = self._ctx(
            ema_len=2, ema_filter=True, atr_len=2, ema_band_atr="1"
        )
        banded.on_init(bctx)

        assert plain._trend_ok(bars) is True
        assert banded._trend_ok(bars) is False

    def test_zero_band_matches_plain_filter(self) -> None:
        """ema_band_atr=0 时门槛就是 EMA，与旧行为完全一致。"""
        bars = _bars(["10", "10", "10", "10", "11"])
        plain = SmaCrossStrategy()
        plain.on_init(self._ctx(ema_len=4, ema_filter=True))
        zero = SmaCrossStrategy()
        zero.on_init(self._ctx(ema_len=4, ema_filter=True, ema_band_atr="0"))

        assert plain._trend_ok(bars) == zero._trend_ok(bars) is True

    def test_band_waits_for_atr_window(self) -> None:
        """带宽非 0 时需 atr_len+1 根，窗口不足不出信号。"""
        strategy = SmaCrossStrategy()
        ctx = self._ctx(
            ema_len=2, ema_filter=True, atr_len=10, ema_band_atr="1"
        )
        strategy.on_init(ctx)

        signals = _feed(strategy, ctx, _bars(["10", "10", "10", "9", "12"]))

        assert signals == []

    def test_negative_band_raises(self) -> None:
        strategy = SmaCrossStrategy()
        with pytest.raises(ValueError, match="ema_band_atr"):
            strategy.on_init(self._ctx(ema_band_atr="-1"))

    def test_non_positive_atr_len_raises(self) -> None:
        strategy = SmaCrossStrategy()
        with pytest.raises(ValueError, match="atr_len"):
            strategy.on_init(self._ctx(atr_len=0))


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
