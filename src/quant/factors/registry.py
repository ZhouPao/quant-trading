"""因子注册表（M5-1）。

`@factor(...)` 在导入模块时完成注册；`builtin` 模块负责让内置因子被扫描到。
同 ID 重复注册直接报错——静默覆盖会让「到底跑的是哪个实现」变成谜。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from quant.factors.protocol import Factor

_REGISTRY: dict[str, Factor] = {}
_ALIASES: dict[str, str] = {}


def register(spec: Factor) -> Factor:
    """登记一个因子；ID 或别名冲突立即报错。"""
    if spec.factor_id in _REGISTRY or spec.factor_id in _ALIASES:
        raise ValueError(f"factor already registered: {spec.factor_id}")
    for alias in spec.aliases:
        if alias in _REGISTRY or alias in _ALIASES:
            raise ValueError(f"factor alias already registered: {alias}")
    _REGISTRY[spec.factor_id] = spec
    for alias in spec.aliases:
        _ALIASES[alias] = spec.factor_id
    return spec


def factor(
    *,
    factor_id: str,
    name: str,
    category: str,
    scope: str,
    lookback: int,
    params: Mapping[str, Any] | None = None,
    description: str = "",
    aliases: tuple[str, ...] = (),
) -> Callable[[Callable], Factor]:
    """装饰器：把 `compute(bars, params)` 注册为因子，返回 `Factor`。"""

    def decorator(fn: Callable) -> Factor:
        return register(
            Factor(
                factor_id=factor_id,
                name=name,
                category=category,
                scope=scope,
                lookback=lookback,
                compute=fn,
                default_params=dict(params or {}),
                description=description,
                aliases=aliases,
            )
        )

    return decorator


def get_factor(factor_id: str) -> Factor:
    """按 ID 或别名取因子；未注册抛 KeyError。"""
    resolved = _ALIASES.get(factor_id, factor_id)
    try:
        return _REGISTRY[resolved]
    except KeyError as e:
        raise KeyError(f"unknown factor: {factor_id}") from e


def list_factors() -> list[Factor]:
    """全部已注册因子（按 ID 升序）。"""
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def clear_registry() -> None:
    """清空注册表（测试用；生产不调用）。"""
    _REGISTRY.clear()
    _ALIASES.clear()


__all__ = [
    "clear_registry",
    "factor",
    "get_factor",
    "list_factors",
    "register",
]
