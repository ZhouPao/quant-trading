"""执行层接口（M3）。

`Broker` 协议统一 mock/模拟盘/实盘三种实现：
- `Executor`：M2 内存模拟，不触达交易所
- `OkxBroker`：M3 接 OKX 模拟盘 REST 下单
- M5 实盘复用 OkxBroker，仅切换 demo 标志

Worker 只依赖 `Broker` 协议，不关心具体实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from quant.backtest.order import Fill, Order
from quant.backtest.signal import Signal


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """执行结果。

    fill 为 None 时表示订单已提交但未成交（status=pending）。
    """

    order: Order
    fill: Fill | None


@runtime_checkable
class Broker(Protocol):
    """执行层协议。"""

    async def execute(
        self,
        signal: Signal,
        qty: Decimal,
        market_price: Decimal | None = None,
    ) -> ExecutionResult:
        """提交订单。

        Args:
            signal: 原始信号
            qty: 风控调整后的下单数量（**基础币**，必须 > 0）。
                合约的张数换算是 broker 的职责，调用方不要自己乘除 ctVal。
            market_price: 市价单参考价（实盘可忽略）
        """
        ...


__all__ = ["Broker", "ExecutionResult"]
