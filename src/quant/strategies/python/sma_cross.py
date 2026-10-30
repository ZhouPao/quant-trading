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
        ema_len: 趋势过滤的 EMA 周期，默认 200
        ema_filter: 是否启用趋势过滤，默认关闭
        ema_band_atr: 趋势门槛在 EMA 之上再抬高的 ATR 倍数，默认 0
        atr_len: ATR 周期，默认 14
        commission_rate: 手续费率，用于下单量预留，默认 0.001
        sr_*: 支撑压力止盈止损参数，见 SrExitMixin，默认关闭
    """

    @property
    def strategy_id(self) -> str:
        return "sma_cross"

    def on_init(self, ctx: Context) -> None:
        self.fast_len = int(ctx.params.get("fast_len", 10))
        self.slow_len = int(ctx.params.get("slow_len", 50))
        self.ema_len = int(ctx.params.get("ema_len", 200))
        self.ema_filter = bool(ctx.params.get("ema_filter", False))
        self.ema_band_atr = Decimal(str(ctx.params.get("ema_band_atr", "0")))
        self.atr_len = int(ctx.params.get("atr_len", 14))
        self.commission_rate = Decimal(
            str(ctx.params.get("commission_rate", "0.001"))
        )
        if self.fast_len <= 0 or self.slow_len <= 0 or self.ema_len <= 0:
            raise ValueError("fast_len, slow_len and ema_len must be positive")
        if self.atr_len <= 0:
            raise ValueError("atr_len must be positive")
        if self.ema_band_atr < 0:
            raise ValueError("ema_band_atr must be non-negative")
        if self.fast_len >= self.slow_len:
            raise ValueError(
                f"fast_len ({self.fast_len}) must be < slow_len ({self.slow_len})"
            )
        self._sr_init(ctx)

    def on_bar(self, bar: Bar, ctx: Context) -> Signal | None:
        # 需要 slow_len 根当前窗口 + 1 根用于计算前值；开启趋势过滤时还要
        # 攒够 ema_len 根收盘，带宽非 0 时还要 atr_len + 1 根算 ATR
        need = max(self.slow_len + 1, self.ema_len if self.ema_filter else 0)
        if self.ema_filter and self.ema_band_atr > 0:
            need = max(need, self.atr_len + 1)
        if len(ctx.bars_window) < need:
            return None

        closes = [b.close for b in ctx.bars_window]
        fast = sum(closes[-self.fast_len :]) / self.fast_len
        slow = sum(closes[-self.slow_len :]) / self.slow_len

        prev = closes[:-1]
        prev_fast = sum(prev[-self.fast_len :]) / self.fast_len
        prev_slow = sum(prev[-self.slow_len :]) / self.slow_len

        # 金叉：空仓时买入（开启趋势过滤时还要求越过趋势门槛）
        if (
            prev_fast <= prev_slow
            and fast > slow
            and ctx.position.is_flat
            and self._trend_ok(ctx.bars_window)
        ):
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

    def _trend_ok(self, bars: list[Bar]) -> bool:
        """趋势过滤：收盘价须越过 EMA + k×ATR；未开启过滤时恒为 True。

        门槛随 ATR 自适应：波动越大要求离 EMA 越远，抑制震荡段的假突破。
        `ema_band_atr = 0` 时门槛就是 EMA，与旧行为完全一致。
        """
        if not self.ema_filter:
            return True
        closes = [b.close for b in bars]
        ema = _ema_last(closes, self.ema_len)
        if ema is None:
            return False
        threshold = ema
        if self.ema_band_atr > 0:
            atr = _atr_last(bars, self.atr_len)
            if atr is None:
                return False
            threshold = ema + self.ema_band_atr * atr
        return closes[-1] > threshold

    def _position_qty(self, cash: Decimal, price: Decimal) -> Decimal:
        """按可用现金全仓买入，预留手续费。"""
        if price <= 0:
            return Decimal("0")
        return cash / (price * (1 + self.commission_rate))


def _ema_last(closes: list[Decimal], period: int) -> Decimal | None:
    """按最后 period 根收盘递推 EMA，样本不足时返回 None。

    以最早 period 根的简单平均作种子，之后逐根递推，避免依赖全量历史。
    """
    if period <= 0 or len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k = Decimal(2) / Decimal(period + 1)
    for c in closes[period:]:
        ema = (c - ema) * k + ema
    return ema


def _atr_last(bars: list[Bar], period: int) -> Decimal | None:
    """最近 period 个真实波幅的平均值（ATR 的等权变体）。

    只取窗口末尾 period + 1 根：首根无前收盘，仅作为 TR 计算的参照。
    刻意不做 Wilder 递推——那样 ATR 会依赖窗口长度，`window_size` 一改
    结果就变，回测与实盘的可见窗口不同时会漂移；等权窗口完全由最近
    period 根决定，跨环境可复现。样本不足返回 None。
    """
    if period <= 0 or len(bars) < period + 1:
        return None
    window = bars[-(period + 1) :]
    total = Decimal("0")
    for i in range(1, len(window)):
        prev, cur = window[i - 1], window[i]
        total += max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        )
    return total / period


__all__ = ["SmaCrossStrategy"]
