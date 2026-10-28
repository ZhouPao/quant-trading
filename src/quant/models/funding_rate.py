"""FundingRate 资金费率模型（spec §5.5）。

代表永续合约的资金费率结算记录。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class FundingRate:
    """资金费率结算记录。

    Attributes:
        inst_id: 产品ID，如 "BTC-USDT-SWAP"
        ts: 结算时间戳 UTC ms
        rate: 资金费率（正数=多付空，负数=空付多）
        next_funding_ts: 下次结算时间 UTC ms
    """

    inst_id: str
    ts: int
    rate: Decimal
    next_funding_ts: int

    def __post_init__(self) -> None:
        if not self.inst_id:
            raise ValueError("inst_id must not be empty")
        if self.ts < 0:
            raise ValueError(f"ts must be non-negative, got {self.ts}")
        if self.next_funding_ts < 0:
            raise ValueError(
                f"next_funding_ts must be non-negative, got {self.next_funding_ts}"
            )
        # rate 可正可负，不限制符号；但通常 |rate| < 0.1（10%），超过视为异常
        if abs(self.rate) >= 1:
            raise ValueError(
                f"rate |value| >= 1 looks abnormal, got {self.rate} for {self.inst_id}@{self.ts}"
            )


__all__ = ["FundingRate"]
