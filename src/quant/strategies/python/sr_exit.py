"""支撑/压力位止盈止损（多仓）。

`SmaCrossStrategy` 与 `RsiStrategy` 共用的可选能力，默认关闭
（`sr_enabled = false`），由策略 toml 的 `[params]` 显式开启。

规则：
- 开仓信号产生时，用当前窗口已确认的摆动点算档位，并冻结：
  止损 = 开仓价下方最近支撑，止盈 = 开仓价上方最近压力
- 没有支撑 → 放弃开仓（无保护位不开仓）；没有压力 → 只止损不止盈
- 持仓期间价位不变；bar 内触及即按该价位成交（不套滑点）
- 同一根 bar 内止损与止盈都触及 → 按止损处理（保守）

价位在「产生开仓信号」时冻结而不是等成交回调，因为 live 引擎补评估
只用虚拟成交维护持仓、不会触发 `on_trade`；以信号 bar 收盘价定档既
不依赖成交价，也不引入未来数据。

口径与看板 `web/src/chart/indicators.ts` 一致，见 `quant.indicators`。
"""
from __future__ import annotations

from decimal import Decimal

from quant.backtest.context import Context
from quant.backtest.order import Fill
from quant.indicators.support_resistance import nearest_levels, pivot_levels
from quant.models.bar import Bar


class SrExitMixin:
    """给多仓策略加一层支撑/压力止盈止损。

    用法：策略类写成 `class X(SrExitMixin, Strategy)`，在 `on_init` 里调用
    `self._sr_init(ctx)`，开仓分支里先 `self._sr_freeze(bar.close, ctx)` 定档，
    再用 `self._sr_allows_entry(stop)` 决定是否下单。`on_trade` 与
    `protective_exit` 由本类提供。

    参数（ctx.params）：
        sr_enabled: 总开关，默认 false
        sr_left_right: 摆动点左右确认根数，默认 3
        sr_tolerance_pct: 聚类相对容差（百分数），默认 0.5
        sr_max_levels: 最多保留档数，默认 5
    """

    sr_enabled: bool = False
    sr_left_right: int = 3
    sr_tolerance_pct: Decimal = Decimal("0.5")
    sr_max_levels: int = 5
    _sr_stop: Decimal | None = None
    _sr_target: Decimal | None = None

    def _sr_init(self, ctx: Context) -> None:
        """读取参数并校验。由策略的 on_init 调用。"""
        self.sr_enabled = bool(ctx.params.get("sr_enabled", False))
        self.sr_left_right = int(ctx.params.get("sr_left_right", 3))
        self.sr_tolerance_pct = Decimal(str(ctx.params.get("sr_tolerance_pct", "0.5")))
        self.sr_max_levels = int(ctx.params.get("sr_max_levels", 5))
        if self.sr_left_right <= 0:
            raise ValueError("sr_left_right must be positive")
        if self.sr_tolerance_pct < 0:
            raise ValueError("sr_tolerance_pct must be non-negative")
        if self.sr_max_levels <= 0:
            raise ValueError("sr_max_levels must be positive")
        self._sr_stop = None
        self._sr_target = None

    def _sr_freeze(
        self, price: Decimal, ctx: Context
    ) -> tuple[Decimal | None, Decimal | None]:
        """按开仓价冻结止损/止盈并返回。未启用时返回 (None, None)。

        用当前窗口即可：`pivot_levels` 只输出最晚 `left_right` 根之前、
        且已被后续 bar 确认的摆动点，不构成未来数据。

        返回值只用于是否允许开仓的判定；实际冻结状态写回实例。
        """
        if not self.sr_enabled:
            self._sr_stop = None
            self._sr_target = None
            return (None, None)
        levels = pivot_levels(
            ctx.bars_window,
            left_right=self.sr_left_right,
            tolerance_pct=self.sr_tolerance_pct,
            max_levels=self.sr_max_levels,
        )
        support, resistance = nearest_levels(levels, price)
        self._sr_stop = support.price if support is not None else None
        self._sr_target = resistance.price if resistance is not None else None
        return (self._sr_stop, self._sr_target)

    def _sr_allows_entry(self, stop: Decimal | None) -> bool:
        """无支撑时不允许开仓；未启用时始终允许。"""
        return (not self.sr_enabled) or stop is not None

    def on_trade(self, trade: Fill, ctx: Context) -> None:
        """卖出成交后清空冻结价位。"""
        super().on_trade(trade, ctx)
        if trade.side != "buy":
            self._sr_stop = None
            self._sr_target = None

    def protective_exit(self, bar: Bar, ctx: Context) -> Decimal | None:
        """bar 内触及止损/止盈时返回该价位，止损优先。

        跳空处理：开盘已越过价位时按开盘价成交（止损会更差、止盈会更好），
        与真实挂单一致。

        live 引擎每轮都会重建策略实例、且持仓已存在时不会再走开仓分支，
        因此持仓但无冻结档位时，用持仓均价在该轮重新定档。重新定档随窗口
        缓慢漂移（回测中不会发生：开仓分支总会先冻结）。
        """
        if not self.sr_enabled:
            return None
        if self._sr_stop is None and ctx.position.is_long:
            self._sr_freeze(ctx.position.avg_price, ctx)
        if self._sr_stop is None:
            return None
        if bar.low <= self._sr_stop:
            return min(self._sr_stop, bar.open)
        if self._sr_target is not None and bar.high >= self._sr_target:
            return max(self._sr_target, bar.open)
        return None


__all__ = ["SrExitMixin"]
