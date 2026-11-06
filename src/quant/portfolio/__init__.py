"""M6 组合管理（PRD §4.6）。

8-1 只交付「配置模型 + 加载 + 四种资金分配 + 归一与上限 + 单策略兼容开关」。
按 PRD 边界，本包**不产出调仓单、不调用 OKX**：下单走 M8 worker，
风控走 M7 checker。快照/归因/组合回测/再平衡分别留给 8-2、8-3。
"""
from __future__ import annotations

from .allocator import (
    AllocationError,
    Allocator,
    allocate,
    annualized_volatility,
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
    PortfolioUnit,
    RebalanceConfig,
)

__all__ = [
    "ALLOCATORS",
    "AllocationError",
    "Allocator",
    "PortfolioConfig",
    "PortfolioConfigError",
    "PortfolioRiskConfig",
    "PortfolioUnit",
    "RebalanceConfig",
    "allocate",
    "annualized_volatility",
    "load_portfolio",
    "load_portfolios",
    "portfolio_from_dict",
]
