"""因子协议（M5-1 / M5-2）。

约定：
- `compute(bars, params)` 接收**按时间升序**的 bar 序列，返回**与 bars 等长**的
  因子值列表；`None` 表示该 bar 无法计算（预热不足/停牌/零成交）。
- 只能访问当前及更早的 bar：实现里只能引用 `bars[: i + 1]` 的前缀，
  越界访问未来数据由单测固定。
- `lookback` 是静态声明（预热根数），用于把 `window_size`/`warmup_bars`
  自动抬高；参数变化不改变 `lookback`。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from quant.models.bar import Bar

#: 因子类别（PRD 4.5.1）
CATEGORIES = frozenset(
    {"momentum", "volatility", "trend", "volume", "microstructure"}
)

#: 计算范围：时序（单品种）与横截面（多品种同一 ts）
SCOPES = frozenset({"time_series", "cross_section"})


@dataclass(frozen=True, slots=True)
class Factor:
    """一个已注册因子的元信息与计算函数。"""

    factor_id: str
    name: str
    category: str
    scope: str
    lookback: int
    compute: Callable[
        [Sequence[Bar], Mapping[str, Any]], list[Decimal | None]
    ]
    default_params: Mapping[str, Any]
    description: str = ""
    #: 改名保留旧 ID，旧缓存仍可检索（M5-3）
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(
                f"unknown factor category {self.category!r}; "
                f"expect one of {sorted(CATEGORIES)}"
            )
        if self.scope not in SCOPES:
            raise ValueError(
                f"unknown factor scope {self.scope!r}; "
                f"expect one of {sorted(SCOPES)}"
            )
        if self.lookback < 0:
            raise ValueError("lookback must be non-negative")
        if not self.factor_id.strip():
            raise ValueError("factor_id must not be empty")


__all__ = ["CATEGORIES", "SCOPES", "Factor"]
