"""因子库（M5）。

对外入口：
- `@factor(...)` 注册因子；`get_factor` / `list_factors` 查询
- `param_hash(params)` 参数版本（缓存键）
- `FactorStore` 因子值缓存
- 导入本包即注册 `builtin` 内的示例因子
"""
from __future__ import annotations

from quant.factors import builtin  # noqa: F401  导入即注册内置因子
from quant.factors.params import param_hash
from quant.factors.protocol import CATEGORIES, SCOPES, Factor
from quant.factors.provider import FILL_MODES, FactorProvider
from quant.factors.registry import (
    factor,
    get_factor,
    list_factors,
    register,
)
from quant.factors.storage import FactorStore

__all__ = [
    "CATEGORIES",
    "FILL_MODES",
    "SCOPES",
    "Factor",
    "FactorProvider",
    "FactorStore",
    "factor",
    "get_factor",
    "list_factors",
    "param_hash",
    "register",
]
