"""Position 持仓状态数据模型（spec §4.2）。

记录单品种的当前持仓数量、平均成本、已实现盈亏。
回测引擎每根 bar 更新 equity；worker 在每次成交后更新。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant.backtest.order import Fill


@dataclass(slots=True)
class Position:
    """持仓状态。

    正 qty=多头，负 qty=空头，0=空仓。
    avg_price 是开仓方向的平均成本；平仓时按平仓价 vs avg_price 计算 realized_pnl。

    Attributes:
        inst_id: 产品 ID
        qty: 持仓数量（正=多，负=空），可为 0
        avg_price: 平均成本价，qty=0 时为 0
        realized_pnl: 累计已实现盈亏（计价币），初始 0
    """

    inst_id: str
    qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        """构造时校验：inst_id 非空 + avg_price 非负 + qty=0 时 avg_price=0。"""
        if not self.inst_id:
            raise ValueError("inst_id must be non-empty")
        if self.avg_price < 0:
            raise ValueError(f"avg_price must be non-negative, got {self.avg_price}")
        if self.qty == 0 and self.avg_price != 0:
            raise ValueError(
                f"avg_price must be 0 when qty=0, got avg_price={self.avg_price}"
            )

    @property
    def is_flat(self) -> bool:
        """是否空仓。"""
        return self.qty == 0

    @property
    def is_long(self) -> bool:
        """是否多头持仓。"""
        return self.qty > 0

    @property
    def is_short(self) -> bool:
        """是否空头持仓。"""
        return self.qty < 0

    def apply_fill(self, fill: Fill) -> None:
        """应用一笔成交，更新 qty/avg_price/realized_pnl。

        算法：
        - 加仓：新 avg_price = (旧 qty * 旧 avg + 成交 qty * 成交价) / (旧 qty + 成交 qty)
        - 减仓：realized_pnl += (成交价 - 旧 avg) * 成交 qty * 方向符号
                若全平则 avg_price 归 0；若反手则新方向 avg=成交价

        注意：buy 增加多头/减少空头；sell 减少多头/增加空头。

        Args:
            fill: 成交记录
        """
        if fill.inst_id != self.inst_id:
            raise ValueError(
                f"fill inst_id {fill.inst_id} != position inst_id {self.inst_id}"
            )

        signed_qty = fill.qty if fill.side == "buy" else -fill.qty
        new_qty = self.qty + signed_qty

        # 同方向加仓：更新 avg_price
        if (self.is_long and signed_qty > 0) or (self.is_short and signed_qty < 0):
            self.avg_price = (
                self.qty * self.avg_price + signed_qty * fill.price
            ) / new_qty

        # 反向减仓（包括反手）：实现盈亏
        elif (self.is_long and signed_qty < 0) or (self.is_short and signed_qty > 0):
            closing_qty = min(abs(signed_qty), abs(self.qty))
            # 多头减仓：pnl = (price - avg) * closing_qty
            # 空头减仓：pnl = (avg - price) * closing_qty
            if self.is_long:
                pnl = (fill.price - self.avg_price) * closing_qty
            else:
                pnl = (self.avg_price - fill.price) * closing_qty
            self.realized_pnl += pnl

            # 反手：avg_price 重置为成交价
            if abs(signed_qty) > abs(self.qty):
                self.avg_price = fill.price
            elif new_qty == 0:
                self.avg_price = Decimal("0")

        # 从空仓开仓
        elif self.is_flat:
            self.avg_price = fill.price

        self.qty = new_qty

    def market_value(self, current_price: Decimal) -> Decimal:
        """当前持仓市值（按 current_price 估值）。"""
        return self.qty * current_price

    def unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """未实现盈亏（按 current_price 估值）。

        多头：(current - avg) * qty
        空头：(avg - current) * abs(qty)
        """
        if self.is_long:
            return (current_price - self.avg_price) * self.qty
        if self.is_short:
            return (self.avg_price - current_price) * abs(self.qty)
        return Decimal("0")

    def to_json(self) -> dict:
        """序列化为可 JSON 化的 dict。"""
        return {
            "inst_id": self.inst_id,
            "qty": str(self.qty),
            "avg_price": str(self.avg_price),
            "realized_pnl": str(self.realized_pnl),
        }

    @classmethod
    def from_json(cls, data: dict) -> Position:
        """从 JSON dict 反序列化。"""
        return cls(
            inst_id=data["inst_id"],
            qty=Decimal(str(data["qty"])),
            avg_price=Decimal(str(data["avg_price"])),
            realized_pnl=Decimal(str(data["realized_pnl"])),
        )


__all__ = ["Position"]
