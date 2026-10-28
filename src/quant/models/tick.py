"""Tick 数据模型（spec §5.3）。

代表实时盘口快照（best bid/ask + last trade）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Tick:
    """实时盘口快照。

    Attributes:
        ts: 时间戳 UTC ms
        inst_id: 产品ID
        bid_px: 最优买价
        bid_sz: 最优买量
        ask_px: 最优卖价
        ask_sz: 最优卖量
        last_px: 最新成交价
        last_sz: 最新成交量
    """

    ts: int
    inst_id: str
    bid_px: Decimal
    bid_sz: Decimal
    ask_px: Decimal
    ask_sz: Decimal
    last_px: Decimal
    last_sz: Decimal

    def __post_init__(self) -> None:
        if self.ts < 0:
            raise ValueError(f"ts must be non-negative, got {self.ts}")
        if self.bid_px <= 0:
            raise ValueError(f"bid_px must be positive, got {self.bid_px}")
        if self.ask_px <= 0:
            raise ValueError(f"ask_px must be positive, got {self.ask_px}")
        if self.bid_sz < 0:
            raise ValueError(f"bid_sz must be non-negative, got {self.bid_sz}")
        if self.ask_sz < 0:
            raise ValueError(f"ask_sz must be non-negative, got {self.ask_sz}")
        if self.last_px < 0:
            raise ValueError(f"last_px must be non-negative, got {self.last_px}")
        if self.last_sz < 0:
            raise ValueError(f"last_sz must be non-negative, got {self.last_sz}")
        if self.bid_px > self.ask_px:
            raise ValueError(
                f"bid_px ({self.bid_px}) must be <= ask_px ({self.ask_px}) "
                f"for {self.inst_id}@{self.ts}"
            )


__all__ = ["Tick"]
