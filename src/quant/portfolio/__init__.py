"""M6 组合管理（PRD §4.6）。

8-1 交付「配置模型 + 加载 + 四种资金分配 + 归一与上限 + 单策略兼容开关」；
8-2 交付「快照与存储 + 绩效归因 + 组合回测」。
按 PRD 边界，本包**不产出调仓单、不调用 OKX**：下单走 M8 worker，
风控走 M7 checker。组合级风控/再平衡/调仓单/组合页留给 8-3、8-4。
"""
from __future__ import annotations

from .allocator import (
    AllocationError,
    Allocator,
    allocate,
    annualized_volatility,
)
from .attribution import Attribution, AttributionError, AttributionRow
from .backtest import (
    PortfolioBacktest,
    PortfolioBacktestError,
    PortfolioBacktestResult,
)
from .loader import (
    PortfolioConfigError,
    load_portfolio,
    load_portfolios,
    portfolio_from_dict,
)
from .models import (
    ALLOCATORS,
    PortfolioConfig,
    PortfolioRiskConfig,
    PortfolioSnapshot,
    PortfolioUnit,
    RebalanceConfig,
)
from .state import PortfolioStore

__all__ = [
    "ALLOCATORS",
    "AllocationError",
    "Allocator",
    "Attribution",
    "AttributionError",
    "AttributionRow",
    "PortfolioBacktest",
    "PortfolioBacktestError",
    "PortfolioBacktestResult",
    "PortfolioConfig",
    "PortfolioConfigError",
    "PortfolioRiskConfig",
    "PortfolioSnapshot",
    "PortfolioStore",
    "PortfolioUnit",
    "RebalanceConfig",
    "allocate",
    "annualized_volatility",
    "load_portfolio",
    "load_portfolios",
    "portfolio_from_dict",
]
