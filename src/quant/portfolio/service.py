"""组合调仓执行服务（PRD M6-8/9）。

把 ``RebalancePlan`` 的 deltas 转成标准 ``Signal`` 后，经 **M8 唯一的
SignalQueue** 入队（与 webhook / 手工单同路径），并在 SQLite 留痕。

失败回滚语义（M6-9）：同一 ``(组合, 计划 ts, 单元)`` 只记录一次；
部分单元失败时**不重复追单**，失败原因留存，下一周期用新的 plan_ts
重新评估。``[portfolio] enabled=false`` 时 submit 直接拒绝，避免误触发交易。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from quant.backtest.signal import Signal

from .models import PortfolioConfig
from .rebalance import RebalanceError, RebalancePlan, Rebalancer

if TYPE_CHECKING:
    from quant.state.sqlite_store import SQLiteStore


class SignalSink(Protocol):
    """M8 SignalQueue 的最小契约（便于测试替换）。"""

    async def push(self, signal: Signal) -> None: ...


@dataclass(frozen=True, slots=True)
class RebalanceAttemptResult:
    unit_key: str
    signal_id: str
    status: str  # submitted | failed | skipped
    reason: str | None = None


class PortfolioRebalanceService:
    """组合调仓的显式执行入口；不自动调度，由 8-4 的页面/调度器调用。"""

    def __init__(
        self,
        config: PortfolioConfig,
        store: SQLiteStore,
        queue: SignalSink,
        *,
        enabled: bool,
    ) -> None:
        self._config = config
        self._store = store
        self._queue = queue
        self._enabled = enabled

    async def submit(self, plan: RebalancePlan) -> list[RebalanceAttemptResult]:
        if not self._enabled:
            raise RebalanceError(
                "[portfolio] enabled=false：组合层关闭时不允许提交调仓单"
            )
        if plan.portfolio_id != self._config.portfolio_id:
            raise RebalanceError("plan portfolio_id does not match config")

        previous = {
            str(row["unit_key"]): row
            for row in self._store.get_rebalance_attempts(
                plan.portfolio_id, plan.ts
            )
        }
        signals = {
            signal.signal_id: signal for signal in Rebalancer.to_signals(plan)
        }
        results: list[RebalanceAttemptResult] = []

        for delta in plan.deltas:
            if delta.unit_key in previous:
                # 已处理过（成功或失败）都不追单，留给下一周期重新评估
                row = previous[delta.unit_key]
                results.append(
                    RebalanceAttemptResult(
                        unit_key=delta.unit_key,
                        signal_id=delta.signal_id,
                        status="skipped",
                        reason=f"already {row['status']}: {row['reason'] or '-'}",
                    )
                )
                continue

            signal = signals[delta.signal_id]
            status = "submitted"
            reason: str | None = None
            try:
                # 先留痕再入队：即使队列异常，也能看到这笔调仓意图
                self._store.save_signal(signal, "pending")
                await self._queue.push(signal)
            except Exception as exc:  # noqa: BLE001 - 单单元失败不拖垮其余单元
                status = "failed"
                reason = f"{type(exc).__name__}: {exc}"
            self._store.save_rebalance_attempt(
                plan.portfolio_id,
                plan.ts,
                delta.unit_key,
                delta.signal_id,
                status,
                reason,
                plan.ts,
            )
            results.append(
                RebalanceAttemptResult(
                    unit_key=delta.unit_key,
                    signal_id=delta.signal_id,
                    status=status,
                    reason=reason,
                )
            )
        return results
