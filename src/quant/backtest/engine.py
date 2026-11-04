"""事件驱动回测引擎（spec §4.3）。

按时间顺序回放 Bar，每根调 strategy.on_bar；产生 Signal 时按下一根 bar 模拟成交。
设计约束：
- bars 必须按 ts 严格升序且同品种，引擎不排序
- 市价单按下一根开盘价成交，避免使用未来数据
- 保护性出场（`strategy.protective_exit`）在 bar 内触及即按该价位成交，
  本根不再接受新信号
- 最后一根产生的信号无法成交，记入 unfilled_signals
- equity = cash + position.market_value(bar.close)，每根 bar 记录一次
- 可选计入资金费率（M4-15）：传入 funding 时，每个结算点按当时的持仓与
  本根开盘价结算，`cash -= rate * qty * price`
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from quant.backtest.context import Context
from quant.backtest.funding import FundingSchedule
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
    #: 资金费率净支出（正=付出，负=收取）；未启用资金费率时恒为 0
    funding_cost: Decimal = Decimal("0")


class Backtest:
    """事件驱动回测引擎。

    用法：
        bt = Backtest(strategy, Matcher(), initial_cash=Decimal("10000"))
        result = bt.run(bars)

    warmup_bars 用于「只统计指定区间」的回测：预热段照常喂给策略
    （保证 RSI 等有内部状态的策略初始化正确），但产生的信号被丢弃、
    不成交也不计入权益曲线。

    funding 为空表示不计资金费率；传入时预热段不结算（预热期不持仓）。
    """

    def __init__(
        self,
        strategy: Strategy,
        matcher: Matcher | None = None,
        initial_cash: Decimal = Decimal("10000"),
        window_size: int = 200,
        params: dict[str, Any] | None = None,
        warmup_bars: list[Bar] | None = None,
        funding: FundingSchedule | None = None,
        factor_series: Mapping[str, Mapping[int, Decimal | None]] | None = None,
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
        self.funding = funding
        #: 因子 ID -> {ts: 因子值}（M5-13）；空表示不向策略暴露因子
        self.factor_series = {
            factor_id: dict(values)
            for factor_id, values in (factor_series or {}).items()
        }

    def run(
        self,
        bars: list[Bar],
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> BacktestResult:
        """运行回测。

        Args:
            bars: 历史 K线，必须按 ts 严格升序且同品种
            should_stop: 每根 bar 前调用一次，返回 True 即提前结束（取消任务）。
                提前结束时结果只覆盖已回放的部分，调用方应丢弃。

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
        funding_cost = Decimal("0")
        funding_cursor = 0

        for bar in bars:
            if should_stop is not None and should_stop():
                break
            # 资金费率结算：覆盖上一根 bar 之后到本根 bar 时间戳的结算点。
            # 用带符号 qty 统一方向：rate>0 时长仓付、短仓收。
            if self.funding is not None:
                until = self.funding.index_until(bar.ts)
                while funding_cursor < until:
                    rate = self.funding.rates[funding_cursor]
                    funding_cursor += 1
                    cost = rate * position.qty * bar.open
                    if cost != 0:
                        cash -= cost
                        funding_cost += cost
            # 上一根产生的信号在本根开盘时执行（避免未来数据）
            if pending is not None:
                try:
                    fill = self.matcher.match(pending, bar)
                except NoFillError:
                    # 限价单未穿透：保留挂单，下一根继续尝试（GTC）。
                    # 原先直接丢弃 → 限价策略在回测里被系统性低估（P1）
                    pass
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
                    # 策略返回的出场价必须落在本根 bar 内：否则等于允许任意价
                    # 成交，策略 bug 会静默放大/缩小收益（P1）
                    if not (bar.low <= exit_price <= bar.high):
                        raise ValueError(
                            f"protective exit price {exit_price} outside bar "
                            f"range [{bar.low}, {bar.high}]"
                        )
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
                    if pending is not None:
                        # 新信号取消旧挂单：记入未成交，避免静默丢失
                        unfilled.append(pending)
                    pending = signal

            ctx.cash = cash
            equity_curve.append((bar.ts, cash + position.market_value(bar.close)))

        # 最后一根 bar 的信号无下一根可成交
        if pending is not None:
            unfilled.append(pending)

        self.strategy.on_finish(ctx)

        drawdown_curve = compute_drawdown_curve(equity_curve)
        metrics = compute_metrics(trades, equity_curve, bars)
        # 金额必须保持 Decimal 文本：转 float 入库会变成
        # 0.10000000000000001 这类脏值（AGENTS.md 硬规则 #1）
        metrics["funding_cost"] = str(funding_cost)

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            unfilled_signals=unfilled,
            final_position=position,
            initial_cash=self.initial_cash,
            final_cash=cash,
            drawdown_curve=drawdown_curve,
            metrics=metrics,
            funding_cost=funding_cost,
        )

    def _advance(self, ctx: Context, bar: Bar) -> None:
        """把 bar 推进窗口（更新 timestamp、裁剪到 window_size）。"""
        ctx.timestamp = bar.ts
        ctx.bars_window.append(bar)
        if len(ctx.bars_window) > self.window_size:
            del ctx.bars_window[: len(ctx.bars_window) - self.window_size]
        # 因子序列与窗口同步（M5-13）：缺值保持 None，长度始终等于 bars_window
        if self.factor_series:
            for factor_id, values in self.factor_series.items():
                series = ctx.factors.setdefault(factor_id, [])
                series.append(values.get(bar.ts))
                if len(series) > len(ctx.bars_window):
                    del series[: len(series) - len(ctx.bars_window)]

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
