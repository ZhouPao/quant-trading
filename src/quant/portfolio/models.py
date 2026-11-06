"""M6 组合管理领域模型（PRD §4.6.1）。

只描述「组合是什么、有哪些单元、怎么分配与再平衡」，**不产出调仓单、
不调用 OKX**。组合层按 PRD 边界只给目标权重，下单仍走 M8 worker，
风控仍走 M7 checker。

所有比例/金额一律 Decimal（AGENTS 硬规则 #1），时间一律 UTC 毫秒。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

#: 四种分配法（PRD M6-3）；单一组合只用一种，不做混合。
ALLOCATORS = ("equal", "fixed", "inverse_vol", "vol_target")


@dataclass(frozen=True, slots=True)
class PortfolioUnit:
    """一个「策略 × 品种」单元。

    ``params_ref`` 指向 ``config/strategies/`` 中的参数集；``weight`` 只在
    ``fixed`` 分配法下必填。``enabled=False`` 表示停用：**保留配置、持仓与
    历史，只是不再产生新信号**（PRD M6-2）。
    """

    strategy_id: str
    inst_id: str
    params_ref: str | None = None
    weight: Decimal | None = None
    enabled: bool = True

    @property
    def unit_key(self) -> str:
        """单元唯一键；同一组合内不允许重复。"""
        return f"{self.strategy_id}:{self.inst_id}"


@dataclass(frozen=True, slots=True)
class RebalanceConfig:
    """再平衡触发配置（M6-6/M6-7，8-3 使用；8-1 先落配置模型）。"""

    #: calendar=固定周期；threshold=漂移超阈值；both=两者任一触发
    mode: str = "calendar"
    interval_hours: int = 24
    drift_pct: Decimal = Decimal("0.05")
    cooldown_hours: int = 6


@dataclass(frozen=True, slots=True)
class PortfolioRiskConfig:
    """组合级风控阈值（M6-5，8-3 使用）。

    组合层只产出「降权/暂停单元」的建议，限额仍由 M7 同一套 checker 执行。
    """

    max_drawdown_pct: Decimal = Decimal("0.15")
    max_correlation: Decimal = Decimal("0.80")
    max_unit_contribution_pct: Decimal = Decimal("0.50")


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    """组合配置，落 ``config/portfolios/<id>.toml``。"""

    portfolio_id: str
    units: tuple[PortfolioUnit, ...]
    allocator: str = "equal"
    name: str = ""
    #: 单单元权重上限（M6-4）；归一后任何单元不得超过该值
    max_unit_weight: Decimal = Decimal("1")
    #: ``vol_target`` 法的年化目标波动；``vol_window_bars`` 供波动率估算
    target_vol: Decimal = Decimal("0.20")
    vol_window_bars: int = 30
    rebalance: RebalanceConfig = field(default_factory=RebalanceConfig)
    risk: PortfolioRiskConfig = field(default_factory=PortfolioRiskConfig)

    def __post_init__(self) -> None:
        if not self.portfolio_id:
            raise ValueError("portfolio_id must not be empty")
        if self.allocator not in ALLOCATORS:
            raise ValueError(
                f"unknown allocator {self.allocator!r}; choose from {ALLOCATORS}"
            )
        if not self.units:
            raise ValueError("portfolio must define at least one unit")
        if self.max_unit_weight <= 0 or self.max_unit_weight > 1:
            raise ValueError("max_unit_weight must be in (0, 1]")
        if self.target_vol <= 0:
            raise ValueError("target_vol must be positive")
        if self.vol_window_bars < 2:
            raise ValueError("vol_window_bars must be >= 2")
        if self.rebalance.mode not in ("calendar", "threshold", "both"):
            raise ValueError("rebalance.mode must be calendar/threshold/both")
        keys = [unit.unit_key for unit in self.units]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate unit_key in portfolio")
        for unit in self.units:
            if not unit.strategy_id or not unit.inst_id:
                raise ValueError("unit requires strategy_id and inst_id")

    def enabled_units(self) -> tuple[PortfolioUnit, ...]:
        """参与资金分配的单元；停用单元仍留在 ``units`` 中（权重记 0）。"""
        return tuple(unit for unit in self.units if unit.enabled)

    def unit_map(self) -> dict[str, PortfolioUnit]:
        """unit_key → 单元，供快照与归因按 key 回查。"""
        return {unit.unit_key: unit for unit in self.units}
