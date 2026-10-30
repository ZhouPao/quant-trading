"""Backtest 事件驱动引擎测试（spec §4.3）。

覆盖：
- 输入校验：空 bars / 乱序 / 重复 ts / 多品种 / 非法 cash 或 window
- 生命周期：on_init 一次、on_bar 每根、on_trade 成交时、on_finish 一次
- 成交时序：信号在下一根 bar 开盘成交
- 持仓与现金：买入/卖出/开空、手续费、已实现盈亏
- unfilled：最后一根信号、限价未穿透
- 权益曲线：长度、空仓等于现金、持仓按收盘价估值
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.context import Context
from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.order import Fill
from quant.backtest.signal import Signal
from quant.backtest.strategy import Strategy
from quant.foundation.errors import NoFillError
from quant.models.bar import Bar

INST = "BTC-USDT"


def _bar(
    ts: int,
    open_: str = "100",
    high: str = "100",
    low: str = "100",
    close: str = "100",
    inst_id: str = INST,
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


def _bars(n: int = 3, **kwargs) -> list[Bar]:
    """n 根等距 1H bar。"""
    return [_bar(i * 3_600_000, **kwargs) for i in range(n)]


def _signal(side: str = "buy", qty: str = "1", price: str | None = None,
            order_type: str = "market", ts: int = 0) -> Signal:
    return Signal(
        strategy_id="test",
        side=side,
        order_type=order_type,
        inst_id=INST,
        qty=Decimal(qty),
        ts=ts,
        price=Decimal(price) if price is not None else None,
    )


class ScriptedStrategy(Strategy):
    """按 bar 序号返回预设信号，并记录生命周期回调。"""

    def __init__(self, signals: dict[int, Signal] | None = None) -> None:
        self._signals = signals or {}
        self.init_count = 0
        self.finish_count = 0
        self.bar_count = 0
        self.window_sizes: list[int] = []
        self.trades: list[Fill] = []
        self.params_seen: dict = {}
        self.cash_seen: list[Decimal] = []

    @property
    def strategy_id(self) -> str:
        return "test"

    def on_init(self, ctx: Context) -> None:
        self.init_count += 1
        self.params_seen = dict(ctx.params)

    def on_bar(self, bar: Bar, ctx: Context) -> Signal | None:
        self.window_sizes.append(len(ctx.bars_window))
        self.cash_seen.append(ctx.cash)
        signal = self._signals.get(self.bar_count)
        self.bar_count += 1
        return signal

    def on_trade(self, trade: Fill, ctx: Context) -> None:
        self.trades.append(trade)

    def on_finish(self, ctx: Context) -> None:
        self.finish_count += 1


class TestInputValidation:
    def test_empty_bars(self) -> None:
        with pytest.raises(ValueError, match="must be non-empty"):
            Backtest(ScriptedStrategy()).run([])

    def test_unordered_bars(self) -> None:
        bars = [_bar(3_600_000), _bar(0)]
        with pytest.raises(ValueError, match="strictly ascending"):
            Backtest(ScriptedStrategy()).run(bars)

    def test_duplicate_ts(self) -> None:
        bars = [_bar(0), _bar(0)]
        with pytest.raises(ValueError, match="strictly ascending"):
            Backtest(ScriptedStrategy()).run(bars)

    def test_mixed_instruments(self) -> None:
        bars = [_bar(0), _bar(3_600_000, inst_id="ETH-USDT")]
        with pytest.raises(ValueError, match="mixed instruments"):
            Backtest(ScriptedStrategy()).run(bars)

    def test_invalid_initial_cash(self) -> None:
        with pytest.raises(ValueError, match="initial_cash must be positive"):
            Backtest(ScriptedStrategy(), initial_cash=Decimal("0"))

    def test_invalid_window_size(self) -> None:
        with pytest.raises(ValueError, match="window_size must be positive"):
            Backtest(ScriptedStrategy(), window_size=0)


class TestLifecycle:
    def test_callbacks(self) -> None:
        strategy = ScriptedStrategy()
        Backtest(strategy).run(_bars(3))

        assert strategy.init_count == 1
        assert strategy.finish_count == 1
        assert strategy.bar_count == 3

    def test_params_passed(self) -> None:
        strategy = ScriptedStrategy()
        Backtest(strategy, params={"fast_len": 3}).run(_bars(1))

        assert strategy.params_seen == {"fast_len": 3}

    def test_window_trimmed(self) -> None:
        strategy = ScriptedStrategy()
        Backtest(strategy, window_size=3).run(_bars(5))

        assert max(strategy.window_sizes) == 3
        assert strategy.window_sizes[-1] == 3

    def test_on_trade_called_once_per_fill(self) -> None:
        strategy = ScriptedStrategy({0: _signal()})
        Backtest(strategy, Matcher(commission_rate=Decimal("0"))).run(_bars(2))

        assert len(strategy.trades) == 1
        assert strategy.trades[0].ts == 3_600_000


class TestFills:
    def test_market_fill_at_next_open(self) -> None:
        bars = [
            _bar(0, close="100"),
            _bar(3_600_000, open_="101", high="105", low="99", close="104"),
        ]
        result = Backtest(
            ScriptedStrategy({0: _signal()}),
            Matcher(commission_rate=Decimal("0")),
        ).run(bars)

        assert len(result.trades) == 1
        assert result.trades[0].price == Decimal("101")
        assert result.trades[0].ts == 3_600_000

    def test_buy_updates_position_and_cash(self) -> None:
        bars = [
            _bar(0),
            _bar(3_600_000, open_="100", high="110", low="99", close="105"),
        ]
        result = Backtest(
            ScriptedStrategy({0: _signal(qty="2")}),
            Matcher(commission_rate=Decimal("0")),
            initial_cash=Decimal("1000"),
        ).run(bars)

        assert result.final_position.qty == Decimal("2")
        assert result.final_position.avg_price == Decimal("100")
        assert result.final_cash == Decimal("800")

    def test_sell_closes_long_and_realizes_pnl(self) -> None:
        bars = [
            _bar(0),
            _bar(3_600_000, open_="100", close="100"),
            _bar(7_200_000, open_="120", high="120", low="120", close="120"),
        ]
        # bar0 买入，bar1 卖出（按 bar1 开盘 100 成交）
        strategy = ScriptedStrategy({0: _signal("buy"), 1: _signal("sell")})
        result = Backtest(
            strategy, Matcher(commission_rate=Decimal("0")),
            initial_cash=Decimal("1000"),
        ).run(bars)

        assert len(result.trades) == 2
        assert result.final_position.qty == Decimal("0")
        # bar0 买入按 bar1 开盘 100 成交；bar1 卖出按 bar2 开盘 120 成交
        assert result.final_position.realized_pnl == Decimal("20")
        assert result.final_cash == Decimal("1020")

    def test_open_short(self) -> None:
        bars = [
            _bar(0),
            _bar(3_600_000, open_="100", high="100", low="90", close="90"),
        ]
        result = Backtest(
            ScriptedStrategy({0: _signal("sell", qty="2")}),
            Matcher(commission_rate=Decimal("0")),
            initial_cash=Decimal("1000"),
        ).run(bars)

        assert result.final_position.qty == Decimal("-2")
        assert result.final_position.avg_price == Decimal("100")
        assert result.final_cash == Decimal("1200")

    def test_commission_charged(self) -> None:
        bars = [_bar(0), _bar(3_600_000, open_="100")]
        result = Backtest(
            ScriptedStrategy({0: _signal(qty="1")}),
            Matcher(commission_rate=Decimal("0.01")),
            initial_cash=Decimal("1000"),
        ).run(bars)

        assert result.trades[0].fee == Decimal("1.00")
        assert result.final_cash == Decimal("899.00")


class TestUnfilled:
    def test_signal_on_last_bar_unfilled(self) -> None:
        result = Backtest(
            ScriptedStrategy({2: _signal()}), Matcher()
        ).run(_bars(3))

        assert result.trades == []
        assert len(result.unfilled_signals) == 1

    def test_limit_not_touched_unfilled(self) -> None:
        bars = [
            _bar(0),
            _bar(3_600_000, open_="100", high="105", low="99"),
        ]
        signal = _signal(qty="1", price="90", order_type="limit")
        result = Backtest(
            ScriptedStrategy({0: signal}), Matcher()
        ).run(bars)

        # Matcher 抛 NoFillError，引擎把它记入 unfilled
        assert result.trades == []
        assert len(result.unfilled_signals) == 1
        with pytest.raises(NoFillError):
            Matcher().match(signal, bars[1])


class TestShouldStop:
    def test_stops_before_first_bar(self) -> None:
        result = Backtest(ScriptedStrategy()).run(
            _bars(4), should_stop=lambda: True
        )

        assert result.equity_curve == []
        assert result.trades == []

    def test_stops_midway(self) -> None:
        """每根 bar 前检查一次；已回放的部分保留（调用方自行决定是否采用）。"""
        result = Backtest(ScriptedStrategy()).run(_bars(4), should_stop=_StopAfter(2))

        assert len(result.equity_curve) == 2

    def test_none_means_full_run(self) -> None:
        result = Backtest(ScriptedStrategy()).run(_bars(3), should_stop=None)

        assert len(result.equity_curve) == 3


class _StopAfter:
    """第 n 次调用返回 True（前 n-1 次放行）。"""

    def __init__(self, n: int) -> None:
        self._left = n
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self._left


class TestEquityCurve:
    def test_length_matches_bars(self) -> None:
        result = Backtest(ScriptedStrategy()).run(_bars(4))
        assert len(result.equity_curve) == 4
        assert [ts for ts, _ in result.equity_curve] == [
            b.ts for b in _bars(4)
        ]

    def test_flat_equals_cash(self) -> None:
        result = Backtest(
            ScriptedStrategy(), initial_cash=Decimal("500")
        ).run(_bars(2))

        assert all(value == Decimal("500") for _, value in result.equity_curve)

    def test_position_marked_at_close(self) -> None:
        bars = [
            _bar(0),
            _bar(3_600_000, open_="100", high="120", low="99", close="110"),
        ]
        result = Backtest(
            ScriptedStrategy({0: _signal(qty="1")}),
            Matcher(commission_rate=Decimal("0")),
            initial_cash=Decimal("1000"),
        ).run(bars)

        # bar1 成交后 cash=900，持仓 1 按收盘 110 估值 → 1010
        assert result.equity_curve[0][1] == Decimal("1000")
        assert result.equity_curve[1][1] == Decimal("1010")
