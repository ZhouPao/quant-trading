"""OpenInterest 持仓量模型（M2-14）。

代表永续合约的持仓量快照。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class OpenInterest:
    """持仓量快照。

    Attributes:
        inst_id: 产品ID，如 "BTC-USDT-SWAP"
        ts: 时间戳 UTC ms
        oi: 持仓量（张）
        oi_value: 持仓价值（USDT）
    """

    inst_id: str
    ts: int
    oi: Decimal
    oi_value: Decimal

    def __post_init__(self) -> None:
        if not self.inst_id:
            raise ValueError("inst_id must not be empty")
        if self.ts < 0:
            raise ValueError(f"ts must be non-negative, got {self.ts}")
        if self.oi < 0:
            raise ValueError(f"oi must be non-negative, got {self.oi}")
        if self.oi_value < 0:
            raise ValueError(f"oi_value must be non-negative, got {self.oi_value}")


__all__ = ["OpenInterest"]
