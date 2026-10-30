"""订单状态机（MS4-B §7）。

只允许单向迁移，终态不可回退。非法迁移**不抛异常**：对账时远端状态
可能乱跳（例如本地已判 canceled、远端又报 live），抛错会把整轮对账打断；
记录 WARNING 并保持原状态，让下一轮继续收敛。
"""
from __future__ import annotations

#: 终态：不可再迁移。
TERMINAL_STATES = frozenset({"filled", "canceled", "rejected", "expired"})

#: 合法迁移表。`pending → filled` 不是冗余：市价单在 OKX 侧可能一次回包
#: 就是完全成交，没有 live 阶段；若强制「必须经过 live」会把市价单判成非法。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset(
        {"live", "partially_filled", "filled", "canceled", "rejected", "expired"}
    ),
    "live": frozenset(
        {"partially_filled", "filled", "canceled", "expired"}
    ),
    "partially_filled": frozenset({"filled", "canceled"}),
}


def can_transition(current: str, target: str) -> bool:
    """判断迁移是否合法；相同状态视为幂等成功。"""
    if current == target:
        return True
    if current in TERMINAL_STATES:
        return False
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


__all__ = ["ALLOWED_TRANSITIONS", "TERMINAL_STATES", "can_transition"]
