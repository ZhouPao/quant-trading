"""策略参数的单一真相源（MS4-C §3.2）。

合并顺序：

    toml [params]  →  DB 覆盖（strategy_params）  →  调用方显式覆盖（回测请求）

实盘引擎与回测都必须走这里，否则「改一次参数要改两处」，两边口径迟早分叉。
DB 覆盖只在 `PARAM_SPEC` 声明过的参数上生效：未声明的参数（`commission_rate`、
`sr_*`）仍从 toml 读取，避免手工建的表把策略静默改成另一种行为。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant.backtest.strategy import ParamSpec, Strategy
from quant.foundation.logger import get_logger
from quant.state.sqlite_store import SQLiteStore
from quant.strategies.loader import load_strategy_class

log = get_logger("strategies.params")

TOML = "toml"
DB = "db"
REQUEST = "request"

_BOOL_TRUE = frozenset({"true", "1", "yes", "on"})
_BOOL_FALSE = frozenset({"false", "0", "no", "off"})


class ParamError(Exception):
    """参数值不合法（类型/范围/枚举），消息可直接返回给页面。"""


def coerce_value(spec: ParamSpec, raw: Any) -> Any:
    """按 `PARAM_SPEC` 把字符串/数字转成目标类型并校验范围。

    Raises:
        ParamError: 类型不匹配、越界或不在 choices 内
    """
    kind = spec.type
    try:
        if kind == "int":
            if isinstance(raw, bool):
                raise ValueError("bool is not int")
            # JSON 数字 3.7 会被 int() 静默截断，字符串 "3.7" 却报错：
            # 统一拒绝非整数浮点（P2 口径）
            if isinstance(raw, float) and not raw.is_integer():
                raise ValueError("non-integral float")
            value: Any = int(raw)
        elif kind == "float":
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("non-finite float")
        elif kind == "decimal":
            value = Decimal(str(raw))
            if not value.is_finite():
                raise ValueError("non-finite decimal")
        elif kind == "bool":
            value = _to_bool(raw)
        elif kind in ("str", "choice"):
            value = str(raw)
        else:
            raise ParamError(f"unsupported param type {kind!r}")
    except (TypeError, ValueError, InvalidOperation) as e:
        raise ParamError(f"invalid {kind} value {raw!r}: {e}") from e

    if spec.choices is not None and value not in spec.choices:
        raise ParamError(f"{value!r} not in {list(spec.choices)}")
    _check_range(spec, value)
    return value


def format_value(spec: ParamSpec, value: Any) -> str:
    """把参数值规范成入库文本（bool 用 true/false，Decimal 用十进制字面量）。"""
    if spec.type == "bool":
        return "true" if value else "false"
    return str(value)


def resolve_strategy(
    strategy_id: str,
    config_dir: Path | str,
    store: SQLiteStore | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    resolve_from_db: bool = True,
) -> tuple[type[Strategy], dict[str, Any], dict[str, str]]:
    """返回 (策略类, 合并后参数, 每个字段的来源)。

    Args:
        store: 为 None 或 `resolve_from_db=False` 时完全忽略 DB 覆盖
            （回滚开关 `params.resolve_from_db`）。
        overrides: 显式传参，优先级最高但**不写回** DB。
    """
    strategy_cls, toml_params = load_strategy_class(strategy_id, config_dir)
    spec_map = strategy_cls.PARAM_SPEC
    params: dict[str, Any] = dict(toml_params)
    sources: dict[str, str] = {key: TOML for key in toml_params}

    if store is not None and resolve_from_db:
        for row in store.get_strategy_params(strategy_id):
            name = row["param"]
            spec = spec_map.get(name)
            if spec is None:
                # 未声明的参数不允许覆盖：宁可告警也不用库里的值
                log.warning(
                    f"ignore db override not in PARAM_SPEC "
                    f"strategy={strategy_id} param={name}"
                )
                continue
            try:
                params[name] = coerce_value(spec, row["value"])
            except ParamError as e:
                log.warning(
                    f"ignore invalid db override strategy={strategy_id} "
                    f"param={name}: {e}"
                )
                continue
            sources[name] = DB

    for name, raw in (overrides or {}).items():
        spec = spec_map.get(name)
        if spec is not None:
            params[name] = coerce_value(spec, raw)
        else:
            # 未声明参数（commission_rate / sr_* 等）仍允许回测请求显式覆盖做
            # 敏感性研究，但原先 raw 原样直通：任意键、类型混淆、NaN/Inf 都能
            # 注入并污染回测结果（P1）。限制为「键存在于 toml + 与默认值同类型
            # + 有限值」，零费率这类研究仍可用且结果 params 可追溯。
            if name not in toml_params:
                raise ParamError(
                    f"unknown param {name!r}: not declared in PARAM_SPEC or [params]"
                )
            params[name] = coerce_undeclared(name, raw, toml_params[name])
        sources[name] = REQUEST

    return strategy_cls, params, sources


def coerce_undeclared(name: str, raw: Any, default: Any) -> Any:
    """校验未声明参数的显式覆盖：键必须真实存在、类型随 toml 默认值、值有限。"""
    try:
        if isinstance(default, bool):
            return _to_bool(raw)
        if isinstance(default, int):
            if isinstance(raw, bool) or (
                isinstance(raw, float) and not raw.is_integer()
            ):
                raise ValueError("non-integral value for int param")
            return int(raw)
        if isinstance(default, (float, Decimal)):
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("non-finite value")
            return value
        return str(raw)
    except (TypeError, ValueError, InvalidOperation) as e:
        raise ParamError(f"invalid value for {name!r}: {raw!r}: {e}") from e


def resolve_params(
    strategy_id: str,
    config_dir: Path | str,
    store: SQLiteStore | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    resolve_from_db: bool = True,
) -> tuple[dict[str, Any], dict[str, str]]:
    """只要参数与来源时用这个（页面展示 / 测试）。"""
    _cls, params, sources = resolve_strategy(
        strategy_id,
        config_dir,
        store,
        overrides=overrides,
        resolve_from_db=resolve_from_db,
    )
    return params, sources


def param_spec_payload(cls: type[Strategy]) -> list[dict[str, Any]]:
    """把 `PARAM_SPEC` 转成可 JSON 化的列表（键序 = 声明序）。"""
    return [
        {
            "name": name,
            "type": spec.type,
            "default": (
                format_value(spec, spec.default) if spec.type == "bool" else spec.default
            ),
            "min": None if spec.min is None else str(spec.min),
            "max": None if spec.max is None else str(spec.max),
            "choices": list(spec.choices) if spec.choices else None,
            "description": spec.description,
            "unit": spec.unit,
        }
        for name, spec in cls.PARAM_SPEC.items()
    ]


def _to_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


def _check_range(spec: ParamSpec, value: Any) -> None:
    if spec.min is None and spec.max is None:
        return
    if spec.type in ("str", "choice", "bool"):
        return
    low = Decimal(str(spec.min)) if spec.min is not None else None
    high = Decimal(str(spec.max)) if spec.max is not None else None
    try:
        current = Decimal(str(value))
        if low is not None and current < low:
            raise ParamError(f"{value} < min {spec.min}")
        if high is not None and current > high:
            raise ParamError(f"{value} > max {spec.max}")
    except InvalidOperation as e:
        # NaN 等非法 Decimal 比较会抛 InvalidOperation，不能漏到 HTTP 500（P2）
        raise ParamError(f"invalid numeric value {value!r}") from e


__all__ = [
    "DB",
    "REQUEST",
    "TOML",
    "ParamError",
    "coerce_undeclared",
    "coerce_value",
    "format_value",
    "param_spec_payload",
    "resolve_params",
    "resolve_strategy",
]
