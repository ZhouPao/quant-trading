"""风控限额合并（MS4-B §3）。

四层参数体系：① 代码默认 → ② 全局配置 → ③ 策略覆盖 → ④ 在线覆盖。
其中「取严」只发生在 ②③：两者都配置时取更严（更小）的一方；
④ 直接覆盖，不参与取严（在线调整是人工作出的明确决定）。

字段与方向见 spec §3.1：五个字段全部「越小越严」。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from quant.foundation.config import RiskDefaults

#: 参与合并的字段，顺序与 spec §3.1 一致。
LIMIT_FIELDS: tuple[str, ...] = (
    "max_position_pct",
    "max_total_pct",
    "max_order_qty",
    "daily_loss_limit",
    "consecutive_loss_limit",
)

#: ① 代码默认值（与 RiskConfig 的字段默认值保持一致）。
#: 全局层字段为 None 时落到这里，source 记为 ``default``。
CODE_DEFAULTS: dict[str, Decimal | int] = {
    "max_position_pct": Decimal("0.3"),
    "max_total_pct": Decimal("0.8"),
    "max_order_qty": Decimal("10000"),
    "daily_loss_limit": Decimal("0.05"),
    "consecutive_loss_limit": 3,
}

#: 只有整数类型字段。
INT_FIELDS = frozenset({"consecutive_loss_limit"})

SOURCE_DEFAULT = "default"
SOURCE_GLOBAL = "global"
SOURCE_STRATEGY = "strategy"
SOURCE_OVERRIDE = "override"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """合并后的只读限额视图。"""

    strategy_id: str
    max_position_pct: Decimal
    max_total_pct: Decimal
    max_order_qty: Decimal
    daily_loss_limit: Decimal
    consecutive_loss_limit: int
    applied: dict[str, str] = field(default_factory=dict)

    def source(self, name: str) -> str:
        return self.applied.get(name, SOURCE_DEFAULT)

    def as_dict(self) -> dict[str, object]:
        """按字段返回「值 + 来源」，用于 `/api/risk-limits` 与 Redis 发布。"""
        return {
            name: {"value": self.value(name), "source": self.source(name)}
            for name in LIMIT_FIELDS
        }

    def value(self, name: str) -> Decimal | int:
        return getattr(self, name)


def _coerce(name: str, raw: object) -> Decimal | int | None:
    """把配置值转成字段类型；非法值返回 None（视为未配置）。"""
    if raw is None:
        return None
    try:
        if name in INT_FIELDS:
            if isinstance(raw, bool):
                return None
            return int(raw)
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def resolve_limits(
    strategy_id: str,
    defaults: RiskDefaults | None = None,
    strategy_risk: Mapping[str, object] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> RiskLimits:
    """逐字段合并四层参数并记录来源。

    Args:
        strategy_id: 策略标识
        defaults: 全局层（``[risk]``）。字段为 None 表示未配置，不参与取严
        strategy_risk: 策略层（``strategies/{id}.toml`` 的 ``[risk]`` 段），可为 None
        overrides: 在线覆盖，键为字段名，值为字符串或数字

    Returns:
        RiskLimits；``applied`` 记录每个字段最终来自哪一层。

    Notes:
        ②③ 取严时若两者相等，来源记为 ``strategy``（更具体的一层），
        这样页面上的来源标注更贴近「谁在约束这个策略」的直觉。
    """
    strategy_risk = strategy_risk or {}
    overrides = overrides or {}

    values: dict[str, Decimal | int] = dict(CODE_DEFAULTS)
    applied: dict[str, str] = dict.fromkeys(LIMIT_FIELDS, SOURCE_DEFAULT)

    for name in LIMIT_FIELDS:
        global_value = (
            _coerce(name, getattr(defaults, name, None))
            if defaults is not None
            else None
        )
        strategy_value = _coerce(name, strategy_risk.get(name))

        if global_value is not None:
            values[name] = global_value
            applied[name] = SOURCE_GLOBAL
        if strategy_value is not None:
            # 取严：两者都有时取更小的一方；相等时记 strategy
            if global_value is not None and global_value < strategy_value:
                applied[name] = SOURCE_GLOBAL
            else:
                values[name] = strategy_value
                applied[name] = SOURCE_STRATEGY

        override_value = _coerce(name, overrides.get(name))
        if override_value is not None:
            values[name] = override_value
            applied[name] = SOURCE_OVERRIDE

    return RiskLimits(
        strategy_id=strategy_id,
        max_position_pct=values["max_position_pct"],  # type: ignore[arg-type]
        max_total_pct=values["max_total_pct"],  # type: ignore[arg-type]
        max_order_qty=values["max_order_qty"],  # type: ignore[arg-type]
        daily_loss_limit=values["daily_loss_limit"],  # type: ignore[arg-type]
        consecutive_loss_limit=values["consecutive_loss_limit"],  # type: ignore[arg-type]
        applied=applied,
    )


def parse_override_hash(raw: Mapping[str, object], strategy_id: str) -> dict[str, str]:
    """从 Redis hash（``{strategy}:{param}`` → 值）里取出该策略的覆盖。

    非本策略的键被忽略；键格式不符合约定的记录跳过而不是报错，
    避免一条脏数据让整个风控读取失败。
    """
    prefix = f"{strategy_id}:"
    result: dict[str, str] = {}
    for key, value in raw.items():
        text = str(key)
        if not text.startswith(prefix):
            continue
        name = text[len(prefix) :]
        if name in LIMIT_FIELDS:
            result[name] = str(value)
    return result


__all__ = [
    "CODE_DEFAULTS",
    "INT_FIELDS",
    "LIMIT_FIELDS",
    "SOURCE_DEFAULT",
    "SOURCE_GLOBAL",
    "SOURCE_OVERRIDE",
    "SOURCE_STRATEGY",
    "RiskLimits",
    "parse_override_hash",
    "resolve_limits",
]
