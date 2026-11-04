"""M3-5：策略无信号时给出可读原因（Context.last_block_reason）。"""
from __future__ import annotations

from decimal import Decimal

from quant.backtest.context import Context
from quant.backtest.position import Position
from quant.models import Bar
from quant.strategies.python.rsi import RsiStrategy
from quant.strategies.python.sma_cross import SmaCrossStrategy

HOUR = 3_600_000
INST = "BTC-USDT"


def _bar(ts: int, price: str = "100") -> Bar:
    value = Decimal(price)
    return Bar(
        ts=ts,
        open=value,
        high=value,
        low=value,
        close=value,
        vol=Decimal("1"),
        vol_quote=value,
        confirm=1,
        inst_id=INST,
        bar_size="1H",
    )


def _ctx(bars: list[Bar], params: dict | None = None) -> Context:
    return Context(
        position=Position(inst_id=INST),
        bars_window=list(bars),
        params=params or {},
        cash=Decimal("10000"),
    )


class TestSmaReasons:
    def test_warmup_reports_needed_bars(self) -> None:
        strategy = SmaCrossStrategy()
        ctx = _ctx([])
        strategy.on_init(ctx)

        assert strategy.on_bar(_bar(0), ctx) is None
        assert "warmup" in ctx.last_block_reason
        assert "need" in ctx.last_block_reason

    def test_no_cross_leaves_reason_empty(self) -> None:
        """价格恒定：既没有金叉也没有死叉，不该编造原因。"""
        strategy = SmaCrossStrategy()
        bars = [_bar(i * HOUR) for i in range(5)]
        ctx = _ctx(bars, {"fast_len": 2, "slow_len": 4})
        strategy.on_init(ctx)

        assert strategy.on_bar(bars[-1], ctx) is None
        assert ctx.last_block_reason == ""

    def test_trend_filter_blocked(self, monkeypatch) -> None:
        strategy = SmaCrossStrategy()
        prices = ["100", "100", "100", "90", "120"]  # 最后两根形成金叉
        bars = [_bar(i * HOUR, price) for i, price in enumerate(prices)]
        ctx = _ctx(
            bars,
            {"fast_len": 2, "slow_len": 4, "ema_filter": True, "ema_len": 3},
        )
        strategy.on_init(ctx)
        monkeypatch.setattr(strategy, "_trend_ok", lambda _bars: False)

        assert strategy.on_bar(bars[-1], ctx) is None
        assert "trend filter" in ctx.last_block_reason

    def test_golden_cross_ignored_when_already_long(self) -> None:
        strategy = SmaCrossStrategy()
        prices = ["100", "100", "100", "90", "120"]
        bars = [_bar(i * HOUR, price) for i, price in enumerate(prices)]
        ctx = _ctx(bars, {"fast_len": 2, "slow_len": 4})
        strategy.on_init(ctx)
        ctx.position.qty = Decimal("1")  # 已持多

        assert strategy.on_bar(bars[-1], ctx) is None
        assert "already long" in ctx.last_block_reason


class TestRsiReasons:
    def test_neutral_rsi_reports_value(self) -> None:
        strategy = RsiStrategy()
        prices = ["100", "101", "100", "101", "100", "101", "100", "101"]
        bars = [_bar(i * HOUR, price) for i, price in enumerate(prices)]
        ctx = _ctx([], {"rsi_period": 3, "oversold": 30, "overbought": 70})
        strategy.on_init(ctx)

        # RSI 有内部状态：必须逐根喂入（引擎也是每根 bar 调一次）
        for bar in bars:
            ctx.bars_window.append(bar)
            result = strategy.on_bar(bar, ctx)

        assert result is None
        assert "rsi=" in ctx.last_block_reason
        assert "entry zone" in ctx.last_block_reason
