"""事件驱动回测引擎（spec §4.3）。

按时间顺序回放 Bar，每根调 strategy.on_bar；产生 Signal 时按下一根 bar 模拟成交。
设计约束：
- bars 必须按 ts 严格升序且同品种，引擎不排序
- 市价单按下一根开盘价成交，避免使用未来数据
- 保护性出场（`strategy.protective_exit`）在 bar 内触及即按该价位成交，
  本根不再接受新信号
- 最后一根产生的信号无法成交，记入 unfilled_signals
- equity = cash + position.market_value(bar.close)，每根 bar 记录一次
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from quant.backtest.context import Context
from quant.backtest.matcher import Matcher
from quant.backtest.metrics import compute_drawdown_curve, compute_metrics
from quant.backtest.order import Fill
from quant.backtest.position import Position
from quant.backtest.signal import Signal
from quant.backtest.strategy import Strategy
from quant.foundation.errors import NoFillError
from quant.models.bar import Bar


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """回测结果（spec §4.5）。

    drawdown_curve / metrics 由 metrics 模块在 run() 结束时填充。
    """

    trades: list[Fill]
    equity_curve: list[tuple[int, Decimal]]
    unfilled_signals: list[Signal]
    final_position: Position
    initial_cash: Decimal
    final_cash: Decimal
    drawdown_curve: list[tuple[int, Decimal]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


class Backtest:
    """事件驱动回测引擎。

    用法：
        bt = Backtest(strategy, Matcher(), initial_cash=Decimal("10000"))
        result = bt.run(bars)

    warmup_bars 用于「只统计指定区间」的回测：预热段照常喂给策略
    （保证 RSI 等有内部状态的策略初始化正确），但产生的信号被丢弃、
    不成交也不计入权益曲线。
    """

    def __init__(
        self,
        strategy: Strategy,
        matcher: Matcher | None = None,
        initial_cash: Decimal = Decimal("10000"),
        window_size: int = 200,
        params: dict[str, Any] | None = None,
        warmup_bars: list[Bar] | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError(f"initial_cash must be positive, got {initial_cash}")
        if window_size <= 0:
            raise ValueError(f"window_size must be positive, got {window_size}")
        self.strategy = strategy
        self.matcher = matcher if matcher is not None else Matcher()
        self.initial_cash = initial_cash
        self.window_size = window_size
        self.params = dict(params) if params else {}
        self.warmup_bars = list(warmup_bars) if warmup_bars else []

    def run(self, bars: list[Bar]) -> BacktestResult:
        """运行回测。

        Args:
            bars: 历史 K线，必须按 ts 严格升序且同品种

        Returns:
            BacktestResult

        Raises:
            ValueError: bars 为空 / 时间乱序 / 多品种混合
        """
        self._validate_bars(bars)
        if self.warmup_bars:
            self._validate_warmup(self.warmup_bars, bars)

        position = Position(inst_id=bars[0].inst_id)
        ctx = Context(
            position=position,
            params=dict(self.params),
            cash=self.initial_cash,
        )
        self.strategy.on_init(ctx)

        # 预热：只驱动策略状态与窗口，信号丢弃、不成交
        for bar in self.warmup_bars:
            self._advance(ctx, bar)
            self.strategy.on_bar(bar, ctx)

        trades: list[Fill] = []
        equity_curve: list[tuple[int, Decimal]] = []
        unfilled: list[Signal] = []
        cash = self.initial_cash
        pending: Signal | None = None

        for bar in bars:
            # 上一根产生的信号在本根开盘时执行（避免未来数据）
            if pending is not None:
                try:
                    fill = self.matcher.match(pending, bar)
                except NoFillError:
                    unfilled.append(pending)
                else:
                    cash -= fill.net_value
                    position.apply_fill(fill)
                    trades.append(fill)
                    ctx.cash = cash
                    self.strategy.on_trade(fill, ctx)
                pending = None

            ctx.timestamp = bar.ts
            self._advance(ctx, bar)

            # 保护性出场：止损/止盈在 bar 内触及即按该价位成交，本根不再接受新信号
            exited = False
            if position.is_long:
                exit_price = self.strategy.protective_exit(bar, ctx)
                if exit_price is not None:
                    fill = self.matcher.fill_at(
                        inst_id=bar.inst_id,
                        side="sell",
                        qty=position.qty,
                        price=exit_price,
                        ts=bar.ts,
                    )
                    cash -= fill.net_value
                    position.apply_fill(fill)
                    trades.append(fill)
                    ctx.cash = cash
                    self.strategy.on_trade(fill, ctx)
                    exited = True

            if not exited:
                signal = self.strategy.on_bar(bar, ctx)
                if signal is not None:
                    pending = signal

            ctx.cash = cash
            equity_curve.append((bar.ts, cash + position.market_value(bar.close)))

        # 最后一根 bar 的信号无下一根可成交
        if pending is not None:
            unfilled.append(pending)

        self.strategy.on_finish(ctx)

        drawdown_curve = compute_drawdown_curve(equity_curve)
        metrics = compute_metrics(trades, equity_curve, bars)

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            unfilled_signals=unfilled,
            final_position=position,
            initial_cash=self.initial_cash,
            final_cash=cash,
            drawdown_curve=drawdown_curve,
            metrics=metrics,
        )

    def _advance(self, ctx: Context, bar: Bar) -> None:
        """把 bar 推进窗口（更新 timestamp、裁剪到 window_size）。"""
        ctx.timestamp = bar.ts
        ctx.bars_window.append(bar)
        if len(ctx.bars_window) > self.window_size:
            del ctx.bars_window[: len(ctx.bars_window) - self.window_size]

    @staticmethod
    def _validate_bars(bars: list[Bar]) -> None:
        """校验 bars 非空、同品种、ts 严格升序。"""
        if not bars:
            raise ValueError("bars must be non-empty")

        first_inst = bars[0].inst_id
        prev_ts = bars[0].ts
        for bar in bars[1:]:
            if bar.inst_id != first_inst:
                raise ValueError(
                    f"mixed instruments in bars: {first_inst} vs {bar.inst_id}"
                )
            if bar.ts <= prev_ts:
                raise ValueError(
                    f"bars must be strictly ascending by ts, "
                    f"got {prev_ts} then {bar.ts}"
                )
            prev_ts = bar.ts

    @staticmethod
    def _validate_warmup(warmup: list[Bar], bars: list[Bar]) -> None:
        """校验预热段：同品种、严格升序、整体早于回测区间。"""
        Backtest._validate_bars(warmup)
        if warmup[0].inst_id != bars[0].inst_id:
            raise ValueError(
                f"warmup inst {warmup[0].inst_id} != bars inst {bars[0].inst_id}"
            )
        if warmup[-1].ts >= bars[0].ts:
            raise ValueError(
                f"warmup bars must precede backtest range: "
                f"warmup ends at {warmup[-1].ts}, bars start at {bars[0].ts}"
            )


__all__ = ["Backtest", "BacktestResult"]
