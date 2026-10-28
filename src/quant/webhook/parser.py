"""TradingView alert 解析器（spec §6.2）。

输入示例（TV alert message 的 JSON 字符串）：
    {"strategy_id": "sma_cross", "side": "buy", "order_type": "market",
     "inst_id": "BTC-USDT", "qty": "0.001", "price": null}

解析失败一律抛 ParseError；不做静默容错。
"""
from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal, InvalidOperation

from quant.backtest.signal import Signal
from quant.foundation.errors import ParseError

_REQUIRED = ("strategy_id", "side", "order_type", "inst_id", "qty")


class AlertParser:
    """alert JSON → Signal。"""

    def parse(self, body: str, now_ms: int | None = None) -> Signal:
        """解析并构造 Signal。

        Args:
            body: 原始 JSON 字符串
            now_ms: 信号时间戳；默认取当前 UTC ms（便于测试注入）

        Raises:
            ParseError: JSON 非法/缺字段/字段类型错
        """
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ParseError(f"invalid json: {e}") from e

        if not isinstance(data, dict):
            raise ParseError("alert payload must be a json object")

        for key in _REQUIRED:
            if key not in data or data[key] in (None, ""):
                raise ParseError(f"missing field: {key}")

        try:
            qty = Decimal(str(data["qty"]))
        except (InvalidOperation, ValueError) as e:
            raise ParseError(f"invalid qty: {data['qty']!r}") from e

        price: Decimal | None = None
        if data.get("price") is not None:
            try:
                price = Decimal(str(data["price"]))
            except (InvalidOperation, ValueError) as e:
                raise ParseError(f"invalid price: {data['price']!r}") from e

        if data["side"] not in ("buy", "sell"):
            raise ParseError(f"invalid side: {data['side']!r}")
        if data["order_type"] not in ("market", "limit"):
            raise ParseError(f"invalid order_type: {data['order_type']!r}")

        ts = int(now_ms) if now_ms is not None else int(time.time() * 1000)

        try:
            return Signal(
                strategy_id=str(data["strategy_id"]),
                side=data["side"],
                order_type=data["order_type"],
                inst_id=str(data["inst_id"]),
                qty=qty,
                ts=ts,
                price=price,
                signal_id=str(uuid.uuid4()),
            )
        except ValueError as e:
            raise ParseError(str(e)) from e


__all__ = ["AlertParser"]
