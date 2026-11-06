"""资金分配（PRD §4.6.3 / M6-3、M6-4）。

四种方法互斥：``equal`` / ``fixed`` / ``inverse_vol`` / ``vol_target``。
输出是「目标权重」与「目标金额」，**不下单**：调仓单由 M8 worker 生成，
总仓位仍受 M7 ``max_total_pct`` 约束。

``inverse_vol`` 与 ``vol_target`` 共享相对权重 ``1/σ``，区别在总敞口：
前者全额投入（Σw=1），后者按目标波动缩放（Σw=exposure≤1，其余为现金）。
组合波动按「单元间不相关」近似 ``sqrt(Σ (w_i·σ_i)^2)``，这是标准的
保守近似，不引入相关性估计（M6-5 的组合级风控另有相关性上限）。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal

from .models import PortfolioConfig, PortfolioUnit

_ZERO = Decimal("0")
_ONE = Decimal("1")

#: 每年 bar 数（365 天），用于把收益率标准差年化
_BARS_PER_YEAR: dict[str, int] = {
    "1m": 525_600,
    "5m": 105_120,
    "15m": 35_040,
    "30m": 17_520,
    "1H": 8_760,
    "4H": 2_190,
    "6H": 1_460,
    "12H": 730,
    "1D": 365,
}


class AllocationError(ValueError):
    """配置或输入数据不足以完成分配；显式报错，不静默给 0 权重。"""


def annualized_volatility(
    closes: Sequence[Decimal | float], bar_size: str = "1H"
) -> Decimal:
    """由收盘价序列估算年化波动率（对数收益样本标准差 × √每年 bar 数）。

    波动率是统计量而非金额，中间用 float 计算（AGENTS #1 只约束金额），
    返回 Decimal 便于与权重链衔接。样本不足或价格非正一律报错。
    """
    if bar_size not in _BARS_PER_YEAR:
        raise AllocationError(f"unknown bar_size {bar_size!r}")
    if len(closes) < 3:
        raise AllocationError("need at least 3 closes to estimate volatility")
    prices = [float(price) for price in closes]
    if any(price <= 0 or not math.isfinite(price) for price in prices):
        raise AllocationError("closes must be finite and positive")

    returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    vol = math.sqrt(variance * _BARS_PER_YEAR[bar_size])
    return Decimal(str(vol))


def _cap_and_normalize(
    raw: Mapping[str, Decimal], cap: Decimal, total: Decimal
) -> dict[str, Decimal]:
    """归一到 ``total``，并在不超过 ``cap`` 的前提下把超额部分按比例回分。"""
    raw_sum = sum(raw.values(), _ZERO)
    if raw_sum <= 0:
        raise AllocationError("sum of raw weights must be positive")
    weights = {key: value / raw_sum * total for key, value in raw.items()}

    if cap * len(weights) < total:
        raise AllocationError(
            f"max_unit_weight={cap} is infeasible for {len(weights)} units "
            f"(cap * units < {total})"
        )

    for _ in range(len(weights) + 1):
        over = [key for key, value in weights.items() if value > cap]
        if not over:
            return weights
        for key in over:
            weights[key] = cap
        rest = [key for key in weights if key not in over]
        remaining = total - cap * len(over)
        if not rest:
            return weights
        rest_sum = sum((weights[key] for key in rest), _ZERO)
        if rest_sum <= 0:
            share = remaining / len(rest)
            for key in rest:
                weights[key] = share
        else:
            for key in rest:
                weights[key] = weights[key] / rest_sum * remaining
    return weights


class Allocator:
    """按 ``PortfolioConfig`` 计算各单元目标权重/金额。

    ``volatilities`` 以 ``unit_key`` 为键、年化波动率为值，由调用方从
    ``BarStorage`` 用 ``annualized_volatility`` 估算后传入；组合层不直接
    读 parquet，保持模型可纯函数测试。
    """

    def __init__(
        self,
        config: PortfolioConfig,
        volatilities: Mapping[str, Decimal] | None = None,
    ) -> None:
        self._config = config
        self._volatilities = dict(volatilities or {})

    def _relative_weights(self, units: Sequence[PortfolioUnit]) -> dict[str, Decimal]:
        cfg = self._config
        if cfg.allocator == "equal":
            return {unit.unit_key: _ONE for unit in units}

        if cfg.allocator == "fixed":
            raw: dict[str, Decimal] = {}
            for unit in units:
                if unit.weight is None or unit.weight <= 0:
                    raise AllocationError(
                        f"unit {unit.unit_key} needs a positive weight "
                        "for fixed allocator"
                    )
                raw[unit.unit_key] = unit.weight
            return raw

        raw = {}
        for unit in units:
            vol = self._volatilities.get(unit.unit_key)
            if vol is None or vol <= 0:
                raise AllocationError(
                    f"unit {unit.unit_key} needs a positive volatility for "
                    f"{cfg.allocator} allocator"
                )
            raw[unit.unit_key] = _ONE / vol
        return raw

    def _exposure(self, relative: Mapping[str, Decimal]) -> Decimal:
        """vol_target 的目标敞口：组合波动超过目标时留现金，否则满仓。"""
        if self._config.allocator != "vol_target":
            return _ONE
        variance = _ZERO
        for key, weight in relative.items():
            vol = self._volatilities[key]
            variance += (weight * vol) ** 2
        portfolio_vol = Decimal(str(math.sqrt(float(variance))))
        if portfolio_vol <= 0:
            raise AllocationError("portfolio volatility must be positive")
        return min(_ONE, self._config.target_vol / portfolio_vol)

    def weights(self) -> dict[str, Decimal]:
        """目标权重：包含停用单元（权重 0），Σw ≤ 1。

        ``vol_target`` 的 Σw 可能小于 1，差额为现金；其余三法严格等于 1。
        """
        cfg = self._config
        result = {unit.unit_key: _ZERO for unit in cfg.units}
        units = cfg.enabled_units()
        if not units:
            return result

        raw = self._relative_weights(units)
        raw_sum = sum(raw.values(), _ZERO)
        if raw_sum <= 0:
            raise AllocationError("sum of raw weights must be positive")
        relative = {key: value / raw_sum for key, value in raw.items()}

        total = self._exposure(relative)
        scaled = {key: value * total for key, value in relative.items()}
        capped = _cap_and_normalize(scaled, cfg.max_unit_weight, total)
        result.update(capped)
        return result

    def allocate(self, equity: Decimal) -> dict[str, Decimal]:
        """目标金额（Decimal）：``equity × weight``，不下单、不取整。"""
        if equity < 0:
            raise AllocationError("equity must be non-negative")
        return {key: weight * equity for key, weight in self.weights().items()}


def allocate(
    equity: Decimal,
    config: PortfolioConfig,
    volatilities: Mapping[str, Decimal] | None = None,
) -> dict[str, Decimal]:
    """PRD §4.6.3 签名的便捷入口：``allocate(equity, config)``。"""
    return Allocator(config, volatilities).allocate(equity)
