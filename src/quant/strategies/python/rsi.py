"""RSI 策略 Python 副本（spec §5.4）。

与 `strategies/pine/rsi.pine` 严格对齐：
- RSI 使用 Wilder 平滑（RMA，alpha=1/period），与 Pine `ta.rsi` 一致
- 首次 RSI 用前 period 个涨跌的简单平均做种子
- RSI < oversold 且空仓 → 买入；RSI > overbought 且持多 → 平仓
"""
from __future__ import annotations

from decimal import Decimal

from quant.backtest.context import Context
from quant.backtest.signal import Signal
from quant.backtest.strategy import ParamSpec, Strategy
from quant.models.bar import Bar
from quant.strategies.python.sr_exit import SrExitMixin


class RsiStrategy(SrExitMixin, Strategy):
    """RSI 超卖买入/超买平仓策略。

    参数（ctx.params）：
        rsi_period: RSI 周期，默认 14
        oversold: 超卖阈值，默认 30
        overbought: 超买阈值，默认 70
        commission_rate: 手续费率，用于下单量预留，默认 0.001
    """

    PARAM_SPEC = {
        "rsi_period": ParamSpec(
            "int", 14, min=2, max=200, description="RSI 周期", unit="根"
        ),
        "oversold": ParamSpec(
            "decimal", 30, min=0, max=100, description="超卖阈值"
        ),
        "overbought": ParamSpec(
            "decimal", 70, min=0, max=100, description="超买阈值"
        ),
    }

    @property
    def strategy_id(self) -> str:
        return "rsi"

    def on_init(self, ctx: Context) -> None:
        self.period = int(ctx.params.get("rsi_period", 14))
        self.oversold = Decimal(str(ctx.params.get("oversold", 30)))
        self.overbought = Decimal(str(ctx.params.get("overbought", 70)))
        self.commission_rate = Decimal(
            str(ctx.params.get("commission_rate", "0.001"))
        )
        if self.period <= 0:
            raise ValueError("rsi_period must be positive")
        if not self.oversold < self.overbought:
            raise ValueError("oversold must be < overbought")
        self._sr_init(ctx)

        self.last_rsi: Decimal | None = None
        self._prev_close: Decimal | None = None
        self._seed_gains: list[Decimal] = []
        self._seed_losses: list[Decimal] = []
        self._avg_gain: Decimal | None = None
        self._avg_loss: Decimal | None = None

    def on_bar(self, bar: Bar, ctx: Context) -> Signal | None:
        rsi = self._update_rsi(bar)
        self.last_rsi = rsi
        if rsi is None:
            return None

        if rsi < self.oversold and ctx.position.is_flat:
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

        if rsi > self.overbought and ctx.position.is_long:
            return Signal(
                strategy_id=self.strategy_id,
                side="sell",
                order_type="market",
                inst_id=bar.inst_id,
                qty=ctx.position.qty,
                ts=bar.ts,
            )

        return None

    def _update_rsi(self, bar: Bar) -> Decimal | None:
        """Wilder RSI。数据不足时返回 None。"""
        if self._prev_close is None:
            self._prev_close = bar.close
            return None

        change = bar.close - self._prev_close
        self._prev_close = bar.close
        gain = change if change > 0 else Decimal("0")
        loss = -change if change < 0 else Decimal("0")

        if self._avg_gain is None:
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)
            if len(self._seed_gains) < self.period:
                return None
            self._avg_gain = sum(self._seed_gains) / self.period
            self._avg_loss = sum(self._seed_losses) / self.period
        else:
            self._avg_gain = (
                self._avg_gain * (self.period - 1) + gain
            ) / self.period
            self._avg_loss = (
                self._avg_loss * (self.period - 1) + loss
            ) / self.period

        if self._avg_loss == 0:
            return Decimal("100") if self._avg_gain > 0 else Decimal("50")

        rs = self._avg_gain / self._avg_loss
        return Decimal("100") - Decimal("100") / (1 + rs)

    def _position_qty(self, cash: Decimal, price: Decimal) -> Decimal:
        """按可用现金全仓买入，预留手续费。"""
        if price <= 0:
            return Decimal("0")
        return cash / (price * (1 + self.commission_rate))


__all__ = ["RsiStrategy"]
