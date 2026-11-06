"""再平衡触发与调仓计划（PRD M6-6/7/8）。

``Rebalancer.evaluate(snapshot, config) -> RebalancePlan | None``：日历 /
阈值 / 两者任一触发，冷却期内不触发，无动作返回 None。
计划里的每个 delta 转成**标准 Signal**（``source=portfolio``），后续只经
M8 ``SignalQueue`` → worker → 风控 → 下单，不新开下单路径。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from quant.backtest.signal import Signal

from .allocator import Allocator
from .models import PortfolioConfig, PortfolioSnapshot
from .risk import PortfolioRisk, RiskAction

_ZERO = Decimal("0")
_HOUR_MS = 3_600_000


class RebalanceError(ValueError):
    """快照/配置不足以生成计划；显式报错，不静默跳过。"""


@dataclass(frozen=True, slots=True)
class RebalanceDelta:
    """一个单元的目标金额增量（正=买入，负=卖出）。"""

    unit_key: str
    strategy_id: str
    inst_id: str
    side: str
    current_weight: Decimal
    target_weight: Decimal
    amount: Decimal
    signal_id: str


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    """一次调仓计划；deltas 为空的计划不会对外返回。"""

    portfolio_id: str
    ts: int
    equity: Decimal
    reason: str
    deltas: tuple[RebalanceDelta, ...] = field(default_factory=tuple)

    def delta_by_key(self) -> dict[str, RebalanceDelta]:
        return {delta.unit_key: delta for delta in self.deltas}


class Rebalancer:
    """按配置判断何时调仓、调多少。"""

    def __init__(
        self,
        config: PortfolioConfig,
        volatilities: Mapping[str, Decimal] | None = None,
        correlations: Mapping[tuple[str, str], Decimal] | None = None,
        min_delta_pct: Decimal = Decimal("0.005"),
    ) -> None:
        if min_delta_pct < 0:
            raise RebalanceError("min_delta_pct must be non-negative")
        self._config = config
        self._allocator = Allocator(config, volatilities)
        self._risk = PortfolioRisk(config, correlations)
        self._min_delta_pct = min_delta_pct

    def _should_trigger(
        self,
        drift: Decimal,
        now_ts: int,
        last_rebalance_ts: int | None,
    ) -> str | None:
        cfg = self._config.rebalance
        if (
            last_rebalance_ts is not None
            and now_ts - last_rebalance_ts < cfg.cooldown_hours * _HOUR_MS
        ):
            return None
        calendar_due = (
            last_rebalance_ts is None
            or now_ts - last_rebalance_ts >= cfg.interval_hours * _HOUR_MS
        )
        threshold_due = drift > cfg.drift_pct
        if cfg.mode == "calendar":
            return "calendar" if calendar_due else None
        if cfg.mode == "threshold":
            return "threshold" if threshold_due else None
        if calendar_due and threshold_due:
            return "calendar+threshold"
        if calendar_due:
            return "calendar"
        if threshold_due:
            return "threshold"
        return None

    def evaluate(
        self,
        snapshot: PortfolioSnapshot,
        *,
        last_rebalance_ts: int | None = None,
        now_ts: int | None = None,
        risk_actions: Sequence[RiskAction] | None = None,
    ) -> RebalancePlan | None:
        if snapshot.equity <= 0:
            raise RebalanceError("snapshot equity must be positive")
        now = snapshot.ts if now_ts is None else now_ts
        base_weights = self._allocator.weights()
        actions = list(risk_actions) if risk_actions is not None else self._risk.evaluate(snapshot)
        target_weights = PortfolioRisk.apply(base_weights, actions)
        if sum(target_weights.values(), _ZERO) <= 0:
            return None

        drift = max(
            (
                abs(target_weights.get(key, _ZERO) - snapshot.weights.get(key, _ZERO))
                for key in base_weights
            ),
            default=_ZERO,
        )
        reason = self._should_trigger(drift, now, last_rebalance_ts)
        if reason is None:
            return None

        unit_map = self._config.unit_map()
        deltas: list[RebalanceDelta] = []
        for key, target in target_weights.items():
            current = snapshot.weights.get(key, _ZERO)
            delta_weight = target - current
            if abs(delta_weight) < self._min_delta_pct:
                continue
            unit = unit_map.get(key)
            if unit is None:
                raise RebalanceError(f"unit {key} missing from config")
            amount = delta_weight * snapshot.equity
            deltas.append(
                RebalanceDelta(
                    unit_key=key,
                    strategy_id=unit.strategy_id,
                    inst_id=unit.inst_id,
                    side="buy" if amount > 0 else "sell",
                    current_weight=current,
                    target_weight=target,
                    amount=amount,
                    # 由 signal_id 派生的幂等键：重试同一计划不会重复发单
                    signal_id=f"portfolio-{self._config.portfolio_id}-{snapshot.ts}-{key}",
                )
            )
        if not deltas:
            return None
        return RebalancePlan(
            portfolio_id=self._config.portfolio_id,
            ts=snapshot.ts,
            equity=snapshot.equity,
            reason=reason,
            deltas=tuple(deltas),
        )

    @staticmethod
    def to_signals(plan: RebalancePlan) -> list[Signal]:
        """把 deltas 转成标准 Signal：qty 留给 M8-14 服务端按权益算量。"""
        if plan.equity <= 0:
            raise RebalanceError("plan equity must be positive")
        return [
            Signal(
                strategy_id=delta.strategy_id,
                side="buy" if delta.amount > 0 else "sell",  # type: ignore[arg-type]
                order_type="market",
                inst_id=delta.inst_id,
                qty=None,
                ts=plan.ts,
                signal_id=delta.signal_id,
                size_pct=abs(delta.amount) / plan.equity,
                source="portfolio",
            )
            for delta in plan.deltas
        ]
