"""读取 monitor 发布的账户快照（engine 与 worker 共用）。

monitor 每轮把账户快照写入 Redis 并设置 TTL，TTL 过期即表示 monitor 离线。
engine 用其中的 `avail_usdt` 做仓位 sizing，worker 用 `total_equity` 做风控基数；
两者必须取自同一份快照，否则会出现「下单量被风控静默截断」的口径偏差。
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from quant.foundation.logger import get_logger

log = get_logger("execution.account")


async def read_account_field(
    redis,
    key: str,
    field: str,
    *,
    now_ms: int | None = None,
    max_age_ms: int | None = None,
) -> Decimal | None:
    """读取快照 `account.<field>`；键缺失、格式错误或值非正返回 None。

    读 Redis 失败不向上抛：调用方各自有兜底值，不能因为快照读取失败
    把 worker / engine 的主循环带崩。

    Args:
        now_ms: 当前毫秒时间戳；与 ``max_age_ms`` 一起传入时做新鲜度校验
        max_age_ms: 快照最大可接受年龄。monitor 卡死后键仍在 TTL 内，
            但数值已经过时；只判「有没有」会把过期权益当成有效值
    """
    try:
        raw = await redis.get(key)
    except Exception as e:  # noqa: BLE001
        log.warning(f"read account snapshot failed: {e}")
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        value = Decimal(str(payload["account"][field]))
    except (
        KeyError,
        TypeError,
        ValueError,
        InvalidOperation,
    ):
        log.warning(f"account snapshot malformed field={field}")
        return None
    if not value.is_finite() or value <= 0:
        log.warning(f"account snapshot {field}={value} not usable")
        return None
    if max_age_ms is not None:
        try:
            snapshot_ts = int(payload.get("ts") or 0)
        except (TypeError, ValueError):
            snapshot_ts = 0
        if snapshot_ts <= 0 or now_ms is None:
            log.warning("account snapshot missing usable ts")
            return None
        if now_ms - snapshot_ts > max_age_ms:
            log.warning(
                f"account snapshot stale: age={now_ms - snapshot_ts}ms "
                f"> {max_age_ms}ms"
            )
            return None
    return value


__all__ = ["read_account_field"]
