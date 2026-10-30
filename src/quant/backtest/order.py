"""Order / Fill 数据模型（spec §4.2）。

Order 是 worker 调用 Executor 创建的订单；Fill 是成交记录。
回测引擎模拟成交产生 Fill；M3/M5 由交易所返回。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from quant.backtest.signal import Signal

OrderStatus = Literal[
    "pending", "live", "partially_filled", "filled", "canceled", "rejected", "expired"
]


@dataclass(frozen=True, slots=True)
class Order:
    """订单。

    worker 调用 Executor 创建。回测引擎不创建 Order，直接产生 Fill。

    Attributes:
        order_id: 订单唯一 ID（worker/Executor 生成 UUID）
        signal: 触发该订单的 Signal
        status: 订单状态，见 OrderStatus（pending/live/partially_filled/
            filled/canceled/rejected/expired）
        created_ts: 订单创建时间戳 UTC ms
    """

    order_id: str
    signal: Signal
    status: OrderStatus
    created_ts: int

    def __post_init__(self) -> None:
        """构造时校验：order_id 非空 + status 枚举 + ts 非负。"""
        if not self.order_id:
            raise ValueError("order_id must be non-empty")
        if self.status not in (
            "pending",
            "live",
            "partially_filled",
            "filled",
            "canceled",
            "rejected",
            "expired",
        ):
            raise ValueError(f"unknown order status {self.status!r}")
        if self.created_ts < 0:
            raise ValueError(f"created_ts must be non-negative, got {self.created_ts}")


@dataclass(frozen=True, slots=True)
class Fill:
    """成交记录。

    回测引擎 Matcher 模拟成交产生；M3/M5 由 OKX 交易所返回（order filled 推送）。

    Attributes:
        order_id: 关联的订单 ID（回测模式由引擎生成 UUID）
        inst_id: 产品 ID
        side: 买卖方向 buy/sell
        qty: 成交数量（基础币），必须 > 0；现货买入已扣掉基础币手续费
        price: 成交价格，必须 > 0
        fee: 手续费（计价币金额），必须 >= 0
        ts: 成交时间戳 UTC ms
    """

    order_id: str
    inst_id: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    ts: int

    def __post_init__(self) -> None:
        """构造时校验：必填非空 + qty/price 正数 + fee 非负 + ts 非负 + side 枚举。"""
        if not self.order_id:
            raise ValueError("order_id must be non-empty")
        if not self.inst_id:
            raise ValueError("inst_id must be non-empty")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")
        if self.qty <= 0:
            raise ValueError(f"qty must be positive, got {self.qty}")
        if self.price <= 0:
            raise ValueError(f"price must be positive, got {self.price}")
        if self.fee < 0:
            raise ValueError(f"fee must be non-negative, got {self.fee}")
        if self.ts < 0:
            raise ValueError(f"ts must be non-negative, got {self.ts}")

    @property
    def value(self) -> Decimal:
        """成交金额（不含手续费）：qty * price。"""
        return self.qty * self.price

    @property
    def net_value(self) -> Decimal:
        """净成交金额（含手续费，buy 为正、sell 为负）：buy=+value+fee, sell=-value+fee。"""
        if self.side == "buy":
            return self.value + self.fee
        return -self.value - self.fee

    def to_json(self) -> dict:
        """序列化为可 JSON 化的 dict。"""
        return {
            "order_id": self.order_id,
            "inst_id": self.inst_id,
            "side": self.side,
            "qty": str(self.qty),
            "price": str(self.price),
            "fee": str(self.fee),
            "ts": self.ts,
        }

    @classmethod
    def from_json(cls, data: dict) -> Fill:
        """从 JSON dict 反序列化。"""
        return cls(
            order_id=data["order_id"],
            inst_id=data["inst_id"],
            side=data["side"],
            qty=Decimal(str(data["qty"])),
            price=Decimal(str(data["price"])),
            fee=Decimal(str(data["fee"])),
            ts=int(data["ts"]),
        )


__all__ = ["Order", "Fill", "OrderStatus"]
