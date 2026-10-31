"""风控事件（MS4-B §4）。

不是每次判定都落库——放行且未调整的记录量大且无复盘价值。只有
拒绝 / 减量 / 硬停止三类值得留痕，用于回答「昨天为什么拒了这单」。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from quant.backtest.signal import Signal

if TYPE_CHECKING:  # 避免与 checker 循环导入：运行时只用到属性
    from quant.risk.checker import RiskDecision

#: 落库的决策类型。
DECISION_ADJUSTED = "adjusted"
DECISION_REJECTED = "rejected"
DECISION_HALTED = "halted"
#: 服务端算量留痕（M8-14）：回答「这一单的数量从哪来」
DECISION_SIZED = "sized"


def build_event_id(signal_id: str, ts: int, rule: str | None) -> str:
    """确定性 ID：同一信号同一规则重复判定只留一条。"""
    raw = f"{signal_id}|{ts}|{rule or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class RiskEvent:
    """一条风控事件。"""

    event_id: str
    ts: int
    inst_id: str
    signal_id: str
    decision: str
    strategy_id: str | None = None
    rule: str | None = None
    limit_name: str | None = None
    limit_value: Decimal | None = None
    actual_value: Decimal | None = None
    qty_before: Decimal | None = None
    qty_after: Decimal | None = None
    equity: Decimal | None = None
    reason: str = ""

    def as_row(self) -> dict[str, object]:
        """转成 SQLiteStore.insert_risk_event 的关键字参数。"""
        return {
            "event_id": self.event_id,
            "ts": self.ts,
            "inst_id": self.inst_id,
            "signal_id": self.signal_id,
            "decision": self.decision,
            "strategy_id": self.strategy_id,
            "rule": self.rule,
            "limit_name": self.limit_name,
            "limit_value": _text(self.limit_value),
            "actual_value": _text(self.actual_value),
            "qty_before": _text(self.qty_before),
            "qty_after": _text(self.qty_after),
            "equity": _text(self.equity),
            "reason": self.reason,
        }


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def should_log(decision: RiskDecision, signal: Signal) -> bool:
    """只记录拒绝 / 减量 / 硬停止。"""
    return (
        not decision.approved
        or decision.adjusted_qty != signal.qty
        or decision.halt
    )


def event_from_decision(
    decision: RiskDecision,
    signal: Signal,
    *,
    now_ms: int,
    equity: Decimal | None,
) -> RiskEvent:
    """由判定结果构造事件，决策类型按结果推导。"""
    if decision.halt:
        kind = DECISION_HALTED
    elif not decision.approved:
        kind = DECISION_REJECTED
    else:
        kind = DECISION_ADJUSTED
    return RiskEvent(
        event_id=build_event_id(signal.signal_id or "", now_ms, decision.rule),
        ts=now_ms,
        inst_id=signal.inst_id,
        signal_id=signal.signal_id or "",
        decision=kind,
        strategy_id=signal.strategy_id,
        rule=decision.rule,
        limit_name=decision.limit_name,
        limit_value=decision.limit_value,
        actual_value=decision.actual_value,
        qty_before=signal.qty,
        qty_after=decision.adjusted_qty,
        equity=equity,
        reason=decision.reason,
    )


def sized_event(
    signal: Signal,
    *,
    now_ms: int,
    equity: Decimal | None,
    raw_qty: Decimal,
    qty: Decimal,
    capped_by: str | None,
    size_pct: Decimal | None = None,
) -> RiskEvent:
    """服务端算量留痕：`qty_before` 记取整前，`qty_after` 记实际下单量。"""
    reason = f"server-side sizing pct={size_pct} raw={raw_qty} qty={qty}"
    if capped_by:
        reason += f" capped_by={capped_by}"
    return RiskEvent(
        event_id=build_event_id(signal.signal_id or "", now_ms, DECISION_SIZED),
        ts=now_ms,
        inst_id=signal.inst_id,
        signal_id=signal.signal_id or "",
        decision=DECISION_SIZED,
        strategy_id=signal.strategy_id,
        rule=capped_by or DECISION_SIZED,
        qty_before=raw_qty,
        qty_after=qty,
        equity=equity,
        reason=reason,
    )


__all__ = [
    "DECISION_ADJUSTED",
    "DECISION_HALTED",
    "DECISION_REJECTED",
    "DECISION_SIZED",
    "RiskEvent",
    "build_event_id",
    "event_from_decision",
    "should_log",
    "sized_event",
]
