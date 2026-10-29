"""成交模拟器 Matcher（spec §4.4）。

规则：
- 市价单：按下一根 bar 的开盘价成交；买方向加滑点、卖方向减滑点
- 限价单：下一根 bar 价格穿透限价即成交，成交价 = 限价；未穿透抛 NoFillError
- 手续费：price * qty * commission_rate
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from quant.backtest.order import Fill
from quant.backtest.signal import Signal
from quant.foundation.errors import NoFillError
from quant.models.bar import Bar


class Matcher:
    """成交模拟器。

    Attributes:
        slippage_rate: 滑点率（0.0005 = 5bp = 0.05%），必须 >= 0
        commission_rate: 手续费率（0.001 = 0.1% OKX taker），必须 >= 0
    """

    def __init__(
        self,
        slippage_rate: Decimal = Decimal("0"),
        commission_rate: Decimal = Decimal("0.001"),
    ) -> None:
        if slippage_rate < 0:
            raise ValueError(f"slippage_rate must be non-negative, got {slippage_rate}")
        if commission_rate < 0:
            raise ValueError(
                f"commission_rate must be non-negative, got {commission_rate}"
            )
        if slippage_rate >= 1:
            raise ValueError(f"slippage_rate must be < 1, got {slippage_rate}")
        self.slippage_rate = slippage_rate
        self.commission_rate = commission_rate

    def match(self, signal: Signal, next_bar: Bar) -> Fill:
        """根据 signal 在 next_bar 上模拟成交。

        Args:
            signal: 上一根 bar 产生的信号
            next_bar: 下一根 bar（信号在 next_bar 开盘时执行）

        Returns:
            Fill 成交记录，ts 为 next_bar.ts

        Raises:
            ValueError: signal 与 next_bar 品种不一致
            NoFillError: 限价单未穿透
        """
        if signal.inst_id != next_bar.inst_id:
            raise ValueError(
                f"signal inst_id {signal.inst_id} != bar inst_id {next_bar.inst_id}"
            )

        if signal.order_type == "market":
            price = self._apply_slippage(next_bar.open, signal.side)
        else:
            price = self._match_limit(signal, next_bar)

        fee = price * signal.qty * self.commission_rate
        return Fill(
            order_id=str(uuid.uuid4()),
            inst_id=signal.inst_id,
            side=signal.side,
            qty=signal.qty,
            price=price,
            fee=fee,
            ts=next_bar.ts,
        )

    def fill_at(
        self,
        inst_id: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        ts: int,
    ) -> Fill:
        """按指定价位直接成交，用于 bar 内触发的保护性出场。

        保护性出场是「挂在结构位上的单子」，触及即成交，因此不套滑点。

        Args:
            inst_id: 产品 ID
            side: 成交方向 buy/sell
            qty: 成交数量，必须 > 0
            price: 成交价，必须 > 0
            ts: 成交时间戳 UTC ms

        Returns:
            Fill 成交记录

        Raises:
            ValueError: qty 或 price 非正
        """
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")
        if price <= 0:
            raise ValueError(f"price must be positive, got {price}")
        return Fill(
            order_id=str(uuid.uuid4()),
            inst_id=inst_id,
            side=side,
            qty=qty,
            price=price,
            fee=price * qty * self.commission_rate,
            ts=ts,
        )

    def _apply_slippage(self, price: Decimal, side: str) -> Decimal:
        """市价单滑点：买入抬价、卖出压价。"""
        if side == "buy":
            return price * (1 + self.slippage_rate)
        return price * (1 - self.slippage_rate)

    def _match_limit(self, signal: Signal, bar: Bar) -> Decimal:
        """限价单穿透判断。成交价固定为限价。"""
        if signal.price is None:  # Signal 构造已校验，防御性检查
            raise ValueError("limit order must have price")
        if signal.side == "buy":
            if bar.low <= signal.price:
                return signal.price
        else:
            if bar.high >= signal.price:
                return signal.price
        raise NoFillError(
            f"limit {signal.side} {signal.price} not touched "
            f"(bar range [{bar.low}, {bar.high}])"
        )


__all__ = ["Matcher"]
