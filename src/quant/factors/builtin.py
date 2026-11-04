"""内置示例因子（M5-1/M5-2 的落地样板，不是完整因子库）。

只放确定性、无参数的两个时序因子，用于验证
「注册 → 计算 → 缓存 → 读取」链路；横截面与评价指标在 7-2/7-3。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from quant.factors.registry import factor
from quant.models.bar import Bar

LOOKBACK = 20


@factor(
    factor_id="momentum_20",
    name="动量(20)",
    category="momentum",
    scope="time_series",
    lookback=LOOKBACK,
    description="close / close[-20] - 1，前 20 根为空",
)
def momentum_20(
    bars: Sequence[Bar], _params: Mapping[str, Any]
) -> list[Decimal | None]:
    values: list[Decimal | None] = []
    for i, bar in enumerate(bars):
        if i < LOOKBACK:
            values.append(None)
            continue
        prev = bars[i - LOOKBACK].close
        values.append(bar.close / prev - 1 if prev > 0 else None)
    return values


@factor(
    factor_id="volatility_20",
    name="波动率(20)",
    category="volatility",
    scope="time_series",
    lookback=LOOKBACK,
    description="近 20 根收益率的标准差（总体），前 20 根为空",
)
def volatility_20(
    bars: Sequence[Bar], _params: Mapping[str, Any]
) -> list[Decimal | None]:
    values: list[Decimal | None] = []
    for i in range(len(bars)):
        if i < LOOKBACK:
            values.append(None)
            continue
        window = bars[i - LOOKBACK : i + 1]
        returns = [
            window[j].close / window[j - 1].close - 1
            for j in range(1, len(window))
            if window[j - 1].close > 0
        ]
        if len(returns) != LOOKBACK:
            values.append(None)
            continue
        mean = sum(returns) / len(returns)
        variance = sum((item - mean) ** 2 for item in returns) / len(returns)
        values.append(variance.sqrt())
    return values


__all__ = ["LOOKBACK", "momentum_20", "volatility_20"]
