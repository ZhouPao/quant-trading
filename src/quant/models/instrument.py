"""Instrument 产品配置模型（spec §5.4）。

代表 OKX 单个交易对的产品配置（来自 /api/v5/public/instruments）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Instrument:
    """产品配置。

    Attributes:
        inst_id: 产品ID，如 "BTC-USDT" / "BTC-USDT-SWAP"
        inst_type: SPOT/SWAP/FUTURES/OPTION
        base_ccy: 基础币，如 "BTC"
        quote_ccy: 计价币，如 "USDT"
        settle_ccy: 交割币；SPOT 为空字符串
        tick_sz: 价格精度（最小价格变动单位）
        lot_sz: 数量精度
        min_sz: 最小下单量
        ct_val: 合约面值；SPOT 为 1
        state: 状态：live/canceled/expired
        list_time: 上线时间 UTC ms
    """

    inst_id: str
    inst_type: str
    base_ccy: str
    quote_ccy: str
    settle_ccy: str
    tick_sz: Decimal
    lot_sz: Decimal
    min_sz: Decimal
    ct_val: Decimal
    state: str
    list_time: int

    def __post_init__(self) -> None:
        if not self.inst_id:
            raise ValueError("inst_id must not be empty")
        if self.inst_type not in ("SPOT", "SWAP", "FUTURES", "OPTION"):
            raise ValueError(f"unknown inst_type: {self.inst_type}")
        if self.tick_sz <= 0:
            raise ValueError(f"tick_sz must be positive, got {self.tick_sz}")
        if self.lot_sz <= 0:
            raise ValueError(f"lot_sz must be positive, got {self.lot_sz}")
        if self.min_sz < 0:
            raise ValueError(f"min_sz must be non-negative, got {self.min_sz}")
        if self.ct_val <= 0:
            raise ValueError(f"ct_val must be positive, got {self.ct_val}")
        if self.list_time < 0:
            raise ValueError(f"list_time must be non-negative, got {self.list_time}")
        if self.inst_type == "SPOT":
            if self.settle_ccy != "":
                raise ValueError(
                    f"SPOT instrument settle_ccy must be empty, got '{self.settle_ccy}'"
                )
            if self.ct_val != 1:
                raise ValueError(f"SPOT ct_val must be 1, got {self.ct_val}")

    def is_spot(self) -> bool:
        return self.inst_type == "SPOT"

    def is_swap(self) -> bool:
        return self.inst_type == "SWAP"

    def is_live(self) -> bool:
        return self.state == "live"


__all__ = ["Instrument"]
