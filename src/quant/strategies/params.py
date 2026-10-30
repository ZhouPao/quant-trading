"""策略参数的单一真相源（MS4-C §3.2）。

合并顺序：

    toml [params]  →  DB 覆盖（strategy_params）  →  调用方显式覆盖（回测请求）

实盘引擎与回测都必须走这里，否则「改一次参数要改两处」，两边口径迟早分叉。
DB 覆盖只在 `PARAM_SPEC` 声明过的参数上生效：未声明的参数（`commission_rate`、
`sr_*`）仍从 toml 读取，避免手工建的表把策略静默改成另一种行为。
"""
from __future__ import annotations

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
            value: Any = int(raw)
        elif kind == "float":
            value = float(raw)
        elif kind == "decimal":
            value = Decimal(str(raw))
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
        params[name] = coerce_value(spec, raw) if spec else raw
        sources[name] = REQUEST

    return strategy_cls, params, sources


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
    current = Decimal(str(value))
    if low is not None and current < low:
        raise ParamError(f"{value} < min {spec.min}")
    if high is not None and current > high:
        raise ParamError(f"{value} > max {spec.max}")


__all__ = [
    "DB",
    "REQUEST",
    "TOML",
    "ParamError",
    "coerce_value",
    "format_value",
    "param_spec_payload",
    "resolve_params",
    "resolve_strategy",
]
