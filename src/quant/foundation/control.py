"""看板 → supervisor 的进程控制协议（M10-6）。

dashboard 与 supervisor 是不同进程，通过 Redis 队列解耦：
dashboard `LPUSH`，supervisor 每秒轮询 `LPOP`。协议常量放 foundation，
避免 dashboard 反向依赖 `cli.supervisor`。
"""
from __future__ import annotations

#: 控制命令队列
CONTROL_KEY = "quant:control:supervisor"
#: 命令审计（定长 1000）
CONTROL_AUDIT_KEY = "quant:control:audit"
#: 允许 stop/start 的进程。dashboard 自身不在内：停掉后页面无法打开，
#: 只能回到命令行手工拉起，不是「自愈」而是自锁。
CONTROLABLE_PROCESSES = frozenset(
    {"candles", "webhook", "worker", "engine", "monitor"}
)

__all__ = [
    "CONTROLABLE_PROCESSES",
    "CONTROL_AUDIT_KEY",
    "CONTROL_KEY",
]
