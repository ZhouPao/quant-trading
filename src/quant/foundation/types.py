"""Foundation types: 通用类型别名（spec §7.3 之外补充）。

保证全项目使用一致的精度类型，避免 float 在价格/数量上的精度漂移。
"""
from __future__ import annotations

from decimal import Decimal
from typing import NewType

# 价格与数量使用 Decimal，避免 float 二进制精度问题
Price = Decimal
Qty = Decimal

# 时间戳：毫秒级整数（OKX 约定）
TimestampMs = NewType("TimestampMs", int)

# OKX inst_id 形如 "BTC-USDT"
InstId = str

# bar 形如 "1m"/"5m"/"1H"/"1D"（OKX 命名）
BarSize = str
