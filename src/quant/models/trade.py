"""Trade 数据模型（M2-14 订单流足迹图）。

代表单笔市场逐笔成交（tick）。数值字段用 Decimal 保精度，时间用 int UTC ms。
frozen + slots，与 Bar 一致的内存友好约定。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Trade:
    """单笔市场成交（OKX /market/trades 与 WS trades 频道）。

    Attributes:
        ts: 成交时间戳 UTC ms
        inst_id: 产品ID，如 "BTC-USDT-SWAP"
        trade_id: OKX 成交ID（全局唯一），去重键
        px: 成交价
        sz: 成交量（基础币）
        side: 主动方向 — 'buy'=主动买(taker buy)，'sell'=主动卖(taker sell)
    """

    ts: int
    inst_id: str
    trade_id: str
    px: Decimal
    sz: Decimal
    side: str

    def __post_init__(self) -> None:
        """构造时校验：时间戳/价格/数量合法性 + side 取值。"""
        if self.ts < 0:
            raise ValueError(f"ts must be non-negative, got {self.ts}")
        if self.px <= 0:
            raise ValueError(f"px must be positive, got {self.px}")
        if self.sz <= 0:
            raise ValueError(f"sz must be positive, got {self.sz}")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy'/'sell', got {self.side!r}")
