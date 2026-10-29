"""SMA Cross 策略 Python 副本（spec §5.3）。

与 `strategies/pine/sma_cross.pine` 严格对齐：
- fast/slow 均为简单移动平均（不用 EMA）
- 金叉（fast 上穿 slow）买入；死叉（fast 下穿 slow）平多
- 使用上一根窗口计算 prev_fast/prev_slow，避免未来数据
"""
from __future__ import annotations

from decimal import Decimal

from quant.backtest.context import Context
from quant.backtest.signal import Signal
from quant.backtest.strategy import Strategy
from quant.models.bar import Bar
from quant.strategies.python.sr_exit import SrExitMixin


class SmaCrossStrategy(SrExitMixin, Strategy):
    """SMA 金叉/死叉策略。

    参数（ctx.params）：
        fast_len: 快线周期，默认 10
        slow_len: 慢线周期，默认 50
        commission_rate: 手续费率，用于下单量预留，默认 0.001
        sr_*: 支撑压力止盈止损参数，见 SrExitMixin，默认关闭
    """

    @property
    def strategy_id(self) -> str:
        return "sma_cross"

    def on_init(self, ctx: Context) -> None:
        self.fast_len = int(ctx.params.get("fast_len", 10))
        self.slow_len = int(ctx.params.get("slow_len", 50))
        self.commission_rate = Decimal(
            str(ctx.params.get("commission_rate", "0.001"))
        )
        if self.fast_len <= 0 or self.slow_len <= 0:
            raise ValueError("fast_len and slow_len must be positive")
        if self.fast_len >= self.slow_len:
            raise ValueError(
                f"fast_len ({self.fast_len}) must be < slow_len ({self.slow_len})"
            )
        self._sr_init(ctx)

    def on_bar(self, bar: Bar, ctx: Context) -> Signal | None:
        # 需要 slow_len 根当前窗口 + 1 根用于计算前值
        if len(ctx.bars_window) < self.slow_len + 1:
            return None

        closes = [b.close for b in ctx.bars_window]
        fast = sum(closes[-self.fast_len :]) / self.fast_len
        slow = sum(closes[-self.slow_len :]) / self.slow_len

        prev = closes[:-1]
        prev_fast = sum(prev[-self.fast_len :]) / self.fast_len
        prev_slow = sum(prev[-self.slow_len :]) / self.slow_len

        # 金叉：空仓时买入
        if prev_fast <= prev_slow and fast > slow and ctx.position.is_flat:
            stop, _ = self._sr_freeze(bar.close, ctx)
            if not self._sr_allows_entry(stop):
                return None
            qty = self._position_qty(ctx.cash, bar.close)
            if qty <= 0:
                return None
            return Signal(
                strategy_id=self.strategy_id,
                side="buy",
                order_type="market",
                inst_id=bar.inst_id,
                qty=qty,
                ts=bar.ts,
            )

        # 死叉：持多时平仓
        if prev_fast >= prev_slow and fast < slow and ctx.position.is_long:
            return Signal(
                strategy_id=self.strategy_id,
                side="sell",
                order_type="market",
                inst_id=bar.inst_id,
                qty=ctx.position.qty,
                ts=bar.ts,
            )

        return None

    def _position_qty(self, cash: Decimal, price: Decimal) -> Decimal:
        """按可用现金全仓买入，预留手续费。"""
        if price <= 0:
            return Decimal("0")
        return cash / (price * (1 + self.commission_rate))


__all__ = ["SmaCrossStrategy"]
