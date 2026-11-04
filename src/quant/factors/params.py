"""因子参数版本（M5-3）。

同一因子的不同参数集必须独立缓存：`param_hash` 是缓存目录的一级键。
规范化规则固定为「键排序 + 紧凑分隔符」，保证同一组参数只有一种序列化结果。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any


def _jsonable(value: Any) -> Any:
    """Decimal 转文本，避免 float 精度影响哈希。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def param_hash(params: Mapping[str, Any]) -> str:
    """参数集 -> 12 位 sha256 前缀（可读性与防碰撞的折中）。"""
    payload = json.dumps(
        {str(k): _jsonable(v) for k, v in params.items()},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


__all__ = ["param_hash"]
