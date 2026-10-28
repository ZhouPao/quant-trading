"""Signal 交易信号数据模型（spec §4.2）。

代表策略产生的交易信号，与 webhook alert JSON 字段一一对应。
所有数值字段用 Decimal 保证精度，时间用 int UTC ms（与 M1 一致）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]


@dataclass(frozen=True, slots=True)
class Signal:
    """交易信号。

    与 webhook alert JSON 字段一一对应（spec §6.2）：
    {
      "strategy_id": "sma_cross",
      "side": "buy",
      "order_type": "market",
      "inst_id": "BTC-USDT",
      "qty": "0.001",
      "price": null
    }

    回测模式由引擎构造，signal_id 留空字符串；webhook 模式由 parser 生成 UUID。

    Attributes:
        strategy_id: 策略唯一 ID（与 Pine strategy(...) id 一致）
        side: 买卖方向 buy/sell
        order_type: 订单类型 market/limit
        inst_id: 产品 ID，如 "BTC-USDT"
        qty: 下单数量（基础币），必须 > 0
        ts: 信号产生时间戳 UTC ms
        price: 限价单价格；market 单为 None
        signal_id: 信号唯一 ID；回测模式空字符串，webhook 模式 UUID
    """

    strategy_id: str
    side: Side
    order_type: OrderType
    inst_id: str
    qty: Decimal
    ts: int
    price: Decimal | None = None
    signal_id: str = ""

    def __post_init__(self) -> None:
        """构造时校验：必填非空 + side/order_type 枚举 + qty 正数 + price 与 order_type 一致。"""
        if not self.strategy_id:
            raise ValueError("strategy_id must be non-empty")
        if not self.inst_id:
            raise ValueError("inst_id must be non-empty")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"order_type must be 'market' or 'limit', got {self.order_type!r}")
        if self.qty <= 0:
            raise ValueError(f"qty must be positive, got {self.qty}")
        if self.ts < 0:
            raise ValueError(f"ts must be non-negative, got {self.ts}")
        if self.order_type == "market" and self.price is not None:
            raise ValueError(
                f"market order must not have price, got price={self.price}"
            )
        if self.order_type == "limit" and self.price is None:
            raise ValueError("limit order must have price")
        if self.price is not None and self.price <= 0:
            raise ValueError(f"price must be positive, got {self.price}")

    def to_json(self) -> dict:
        """序列化为可 JSON 化的 dict（Decimal → str）。"""
        return {
            "strategy_id": self.strategy_id,
            "side": self.side,
            "order_type": self.order_type,
            "inst_id": self.inst_id,
            "qty": str(self.qty),
            "ts": self.ts,
            "price": str(self.price) if self.price is not None else None,
            "signal_id": self.signal_id,
        }

    @classmethod
    def from_json(cls, data: dict) -> Signal:
        """从 JSON dict 反序列化（str → Decimal）。"""
        return cls(
            strategy_id=data["strategy_id"],
            side=data["side"],
            order_type=data["order_type"],
            inst_id=data["inst_id"],
            qty=Decimal(str(data["qty"])),
            ts=int(data["ts"]),
            price=Decimal(str(data["price"])) if data.get("price") is not None else None,
            signal_id=data.get("signal_id", ""),
        )


__all__ = ["Signal", "Side", "OrderType"]
