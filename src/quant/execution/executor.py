"""执行层（M2 mock，M3 接 OKX 模拟盘，M5 接实盘）。

M2 只负责把信号转成 Order/Fill 并返回，不触达交易所。
"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal

from quant.backtest.order import Fill, Order
from quant.backtest.signal import Signal
from quant.execution.broker import ExecutionResult
from quant.foundation.logger import get_logger

log = get_logger("execution.executor")


class Executor:
    """M2 模拟执行器。

    市价单以传入的 market_price 成交；限价单以 signal.price 成交。
    无可用价格时不成交（返回 pending），由 M3 接真实行情/交易所替代。
    """

    def __init__(self, commission_rate: Decimal = Decimal("0.001")) -> None:
        if commission_rate < 0:
            raise ValueError("commission_rate must be non-negative")
        self.commission_rate = commission_rate

    async def execute(
        self,
        signal: Signal,
        qty: Decimal,
        market_price: Decimal | None = None,
    ) -> ExecutionResult:
        """模拟执行。

        Args:
            signal: 原始信号
            qty: 风控调整后的下单数量（必须 > 0）
            market_price: 市价单参考价
        """
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")

        now_ms = int(time.time() * 1000)
        order_id = str(uuid.uuid4())
        price = signal.price if signal.order_type == "limit" else market_price

        if price is None or price <= 0:
            order = Order(
                order_id=order_id,
                signal=signal,
                status="pending",
                created_ts=now_ms,
            )
            log.info(f"[MOCK] order pending (no price) signal={signal.signal_id}")
            return ExecutionResult(order=order, fill=None)

        fill = Fill(
            order_id=order_id,
            inst_id=signal.inst_id,
            side=signal.side,
            qty=qty,
            price=price,
            fee=price * qty * self.commission_rate,
            ts=now_ms,
        )
        order = Order(
            order_id=order_id,
            signal=signal,
            status="filled",
            created_ts=now_ms,
        )
        log.info(
            f"[MOCK] filled {signal.side} {qty}@{price} "
            f"inst={signal.inst_id} signal={signal.signal_id}"
        )
        return ExecutionResult(order=order, fill=fill)


__all__ = ["Executor", "ExecutionResult"]
