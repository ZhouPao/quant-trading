"""组合级风控建议（PRD M6-5）。

只产出「降权 / 暂停单元」的**建议**，不改限额、不下单、不直接暂停引擎：
M7 checker 仍是唯一的限额执行者，worker 仍是唯一下单路径（AGENTS #8/#14）。
建议由 ``Rebalancer`` 应用到目标权重上，最终只影响调仓单的金额。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from .models import PortfolioConfig, PortfolioSnapshot

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class RiskAction:
    """针对某单元的风控建议。

    ``kind`` ∈ reduce（按 ``factor`` 降权）/ pause（权重置 0）。
    ``reason`` 用于调仓单留痕，便于事后解释为什么少配了钱。
    """

    unit_key: str
    kind: str
    reason: str
    factor: Decimal = Decimal("0.5")


class PortfolioRisk:
    """按组合级阈值评估单元风险。"""

    def __init__(
        self,
        config: PortfolioConfig,
        correlations: Mapping[tuple[str, str], Decimal] | None = None,
    ) -> None:
        self._config = config
        self._correlations = dict(correlations or {})

    def evaluate(self, snapshot: PortfolioSnapshot) -> list[RiskAction]:
        keys = [unit.unit_key for unit in self._config.enabled_units()]
        if not keys:
            return []
        threshold = self._config.risk
        pending: dict[str, RiskAction] = {}

        def add(key: str, action: RiskAction) -> None:
            current = pending.get(key)
            # pause 严于 reduce；同为 reduce 时取更小的 factor（更严）
            if current is None or (current.kind == "reduce" and action.kind == "pause") or (
                current.kind == action.kind and action.factor < current.factor
            ):
                pending[key] = action

        if snapshot.drawdown > threshold.max_drawdown_pct:
            for key in keys:
                add(
                    key,
                    RiskAction(key, "reduce", "portfolio_drawdown", Decimal("0.5")),
                )

        if snapshot.equity > 0:
            for key in keys:
                contribution = snapshot.unit_equity.get(key, _ZERO) / snapshot.equity
                if contribution > threshold.max_unit_contribution_pct:
                    add(
                        key,
                        RiskAction(
                            key, "reduce", "unit_contribution", Decimal("0.5")
                        ),
                    )

        for (left, right), corr in self._correlations.items():
            if corr > threshold.max_correlation:
                for key in (left, right):
                    if key in keys:
                        add(
                            key,
                            RiskAction(key, "reduce", "unit_correlation", Decimal("0.5")),
                        )

        return [pending[key] for key in keys if key in pending]

    @staticmethod
    def apply(
        weights: Mapping[str, Decimal], actions: Sequence[RiskAction]
    ) -> dict[str, Decimal]:
        """把建议应用到权重后重新归一；全被暂停时返回全 0（组合层不擅自加仓）。"""
        by_key = {action.unit_key: action for action in actions}
        adjusted: dict[str, Decimal] = {}
        for key, weight in weights.items():
            action = by_key.get(key)
            if action is None:
                adjusted[key] = weight
            elif action.kind == "pause":
                adjusted[key] = _ZERO
            else:
                adjusted[key] = weight * action.factor
        total = sum(adjusted.values(), _ZERO)
        if total <= 0:
            return {key: _ZERO for key in adjusted}
        return {key: value / total for key, value in adjusted.items()}
