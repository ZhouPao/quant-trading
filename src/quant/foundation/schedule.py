"""本地时间调度小工具：`HH:MM` 解析与「每日一次」判定。

日报（report.daily）与数据保留清理（storage.retention）共用同一套语义：
本地时间过了 `at` 且当天还没跑过，才算到点。

另提供 UTC+8 当日零点：风控「日亏损」按该口径重置，不随宿主机时区变化。
"""
from __future__ import annotations

import time

DAY_MS = 86_400_000
UTC8_OFFSET_MS = 8 * 3600 * 1000


def day_start_ms_utc8(now_ms: int | None = None) -> int:
    """UTC+8 当日 00:00 的毫秒时间戳。

    先把时间戳平移到 UTC+8，按天取整后再平移回来；这样无论宿主机
    时区如何设置，日亏损都在北京时间零点重置。
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return ((now + UTC8_OFFSET_MS) // DAY_MS) * DAY_MS - UTC8_OFFSET_MS


def parse_at(value: str) -> int | None:
    """`HH:MM` -> 当日分钟数；格式非法返回 None。"""
    try:
        hh, mm = value.split(":")
        hour, minute = int(hh), int(mm)
    except (AttributeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def is_due(now_ms: int, at_minutes: int, last_date: str | None) -> bool:
    """当天是否该执行：已过 `at` 且当天尚未执行。"""
    now = time.localtime(now_ms / 1000)
    if last_date == time.strftime("%Y-%m-%d", now):
        return False
    return now.tm_hour * 60 + now.tm_min >= at_minutes


__all__ = ["day_start_ms_utc8", "is_due", "parse_at"]
