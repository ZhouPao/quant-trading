"""组合配置加载（PRD §4.6.2）。

配置落 ``config/portfolios/<id>.toml``，与 ``config/strategies/`` 同级，
真实文件不入 git。目录不存在时返回空列表——这正是 M6-13「未配组合时
系统行为与现在完全一致」的入口。
"""
from __future__ import annotations

import tomllib
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .models import (
    PortfolioConfig,
    PortfolioRiskConfig,
    PortfolioUnit,
    RebalanceConfig,
)


class PortfolioConfigError(ValueError):
    """组合配置文件非法；显式报错而不是静默跳过（AGENTS 硬规则 #7 精神）。"""


def _dec(value: Any, field_name: str) -> Decimal:
    """TOML 数值 → Decimal。先转字符串，避免 float 二进制误差进金额链。"""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PortfolioConfigError(
            f"{field_name} is not a number: {value!r}"
        ) from exc
    if not result.is_finite():
        raise PortfolioConfigError(f"{field_name} must be finite: {value!r}")
    return result


def _nested(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise PortfolioConfigError(f"[{key}] must be a TOML table")
    return value


def portfolio_from_dict(
    data: Mapping[str, Any], *, portfolio_id: str | None = None
) -> PortfolioConfig:
    """把已解析的 TOML dict 转成 ``PortfolioConfig``，逐字段校验。"""
    pid = portfolio_id or str(data.get("portfolio_id", ""))
    if not pid:
        raise PortfolioConfigError("portfolio_id is required")

    raw_units = data.get("units")
    if not isinstance(raw_units, list) or not raw_units:
        raise PortfolioConfigError(f"{pid}: [[units]] must define at least one unit")

    units: list[PortfolioUnit] = []
    for raw in raw_units:
        if not isinstance(raw, Mapping):
            raise PortfolioConfigError(f"{pid}: each unit must be a TOML table")
        strategy_id = str(raw.get("strategy_id", ""))
        inst_id = str(raw.get("inst_id", ""))
        if not strategy_id or not inst_id:
            raise PortfolioConfigError(
                f"{pid}: unit requires strategy_id and inst_id, got {dict(raw)!r}"
            )
        weight = None
        if raw.get("weight") is not None:
            weight = _dec(raw["weight"], f"{pid}.units.weight")
            if weight <= 0:
                raise PortfolioConfigError(f"{pid}: unit weight must be > 0")
        units.append(
            PortfolioUnit(
                strategy_id=strategy_id,
                inst_id=inst_id,
                params_ref=(
                    str(raw["params_ref"]) if raw.get("params_ref") is not None else None
                ),
                weight=weight,
                enabled=bool(raw.get("enabled", True)),
            )
        )

    reb = _nested(data, "rebalance")
    risk = _nested(data, "risk")
    kwargs: dict[str, Any] = {
        "portfolio_id": pid,
        "units": tuple(units),
        "allocator": str(data.get("allocator", "equal")),
        "name": str(data.get("name", "")),
    }
    for key in ("max_unit_weight", "target_vol"):
        if key in data:
            kwargs[key] = _dec(data[key], f"{pid}.{key}")
    if "vol_window_bars" in data:
        kwargs["vol_window_bars"] = int(data["vol_window_bars"])
    if reb:
        kwargs["rebalance"] = RebalanceConfig(
            mode=str(reb.get("mode", "calendar")),
            interval_hours=int(reb.get("interval_hours", 24)),
            drift_pct=_dec(reb.get("drift_pct", "0.05"), f"{pid}.drift_pct"),
            cooldown_hours=int(reb.get("cooldown_hours", 6)),
        )
    if risk:
        kwargs["risk"] = PortfolioRiskConfig(
            max_drawdown_pct=_dec(
                risk.get("max_drawdown_pct", "0.15"), f"{pid}.max_drawdown_pct"
            ),
            max_correlation=_dec(
                risk.get("max_correlation", "0.80"), f"{pid}.max_correlation"
            ),
            max_unit_contribution_pct=_dec(
                risk.get("max_unit_contribution_pct", "0.50"),
                f"{pid}.max_unit_contribution_pct",
            ),
        )

    try:
        return PortfolioConfig(**kwargs)
    except ValueError as exc:
        raise PortfolioConfigError(f"{pid}: {exc}") from exc


def load_portfolio(path: Path) -> PortfolioConfig:
    """加载单个组合；文件 ID 缺省时用文件名（不含扩展名）。"""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PortfolioConfigError(f"cannot read {path}: {exc}") from exc
    return portfolio_from_dict(
        data, portfolio_id=str(data.get("portfolio_id") or path.stem)
    )


def load_portfolios(config_dir: Path) -> list[PortfolioConfig]:
    """加载目录下所有 ``*.toml``（忽略 ``*.toml.example``）。

    目录不存在返回空列表：单策略模式下不需要任何组合配置。
    单个文件非法则抛错，不静默漏掉一个组合。
    """
    if not config_dir.exists():
        return []
    result = []
    for path in sorted(config_dir.glob("*.toml")):
        if path.name.endswith(".toml.example"):
            continue
        result.append(load_portfolio(path))
    return result
