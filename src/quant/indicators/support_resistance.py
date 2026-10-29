"""支撑/压力位识别（与看板 `web/src/chart/indicators.ts` 的 pivotLevels 对齐）。

算法：
1. 找出左右各 `left_right` 根确认的严格摆动高/低点
2. 把价格升序后相近的档位聚类（相对容差 `tolerance_pct`%）
3. 按触碰次数取前 `max_levels` 档，再按价格升序返回

移动平均之外的这类结构指标容易被写成两套不一致的实现，
故前后端各写一份、用同一组输入输出做对齐用例（见 tests）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant.models.bar import Bar


@dataclass(frozen=True, slots=True)
class Level:
    """一档支撑/压力位。

    Attributes:
        price: 档位价格（聚类均值）
        touches: 该档位被摆动点触碰的次数
    """

    price: Decimal
    touches: int

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError(f"price must be positive, got {self.price}")
        if self.touches <= 0:
            raise ValueError(f"touches must be positive, got {self.touches}")


def pivot_levels(
    bars: list[Bar],
    left_right: int = 3,
    tolerance_pct: Decimal | str = Decimal("0.5"),
    max_levels: int = 5,
) -> list[Level]:
    """识别支撑/压力位。

    Args:
        bars: K 线序列（时间升序）；最后 `left_right` 根不参与判定（右侧未确认）
        left_right: 摆动点左右各需确认的根数，必须 > 0
        tolerance_pct: 聚类相对容差（百分数，0.5 表示 0.5%），必须 >= 0
        max_levels: 最多返回档数，必须 > 0

    Returns:
        按价格升序排列的档位列表；无可用档位时返回空列表

    Raises:
        ValueError: 参数非法
    """
    if left_right <= 0:
        raise ValueError(f"left_right must be positive, got {left_right}")
    if max_levels <= 0:
        raise ValueError(f"max_levels must be positive, got {max_levels}")
    tolerance = Decimal(str(tolerance_pct))
    if tolerance < 0:
        raise ValueError(f"tolerance_pct must be non-negative, got {tolerance}")
    if len(bars) < left_right * 2 + 1:
        return []

    prices: list[Decimal] = []
    for i in range(left_right, len(bars) - left_right):
        candidate = bars[i]
        is_high = True
        is_low = True
        for x in range(i - left_right, i + left_right + 1):
            if x == i:
                continue
            other = bars[x]
            if other.high >= candidate.high:
                is_high = False
            if other.low <= candidate.low:
                is_low = False
            if not is_high and not is_low:
                break
        if is_high:
            prices.append(candidate.high)
        if is_low:
            prices.append(candidate.low)

    if not prices:
        return []

    prices.sort()
    ratio = tolerance / Decimal("100")

    clusters: list[list[Decimal]] = []  # [sum, count]
    for price in prices:
        if clusters:
            total, count = clusters[-1]
            avg = total / count
            if abs(price - avg) / avg <= ratio:
                clusters[-1] = [total + price, count + Decimal("1")]
                continue
        clusters.append([price, Decimal("1")])

    ranked = sorted(clusters, key=lambda item: item[1], reverse=True)[:max_levels]
    levels = [Level(price=(total / count), touches=int(count)) for total, count in ranked]
    return sorted(levels, key=lambda level: level.price)


def nearest_levels(
    levels: list[Level], price: Decimal
) -> tuple[Level | None, Level | None]:
    """在给定档位中找出价格下方最近支撑与上方最近压力。

    与前端分类口径一致：价格 >= 参考价视作压力，严格低于视作支撑。

    Args:
        levels: `pivot_levels` 的返回值（升序，顺序不敏感）
        price: 参考价（通常为开仓价）

    Returns:
        (support, resistance)，不存在的一侧为 None
    """
    support: Level | None = None
    resistance: Level | None = None
    for level in levels:
        if level.price < price:
            support = level
        elif resistance is None:
            resistance = level
    return support, resistance


__all__ = ["Level", "nearest_levels", "pivot_levels"]
