"""风控配置（spec §10，MS4-B §3/§13）。

策略 toml 的 `[risk]` 段与全局 `[risk]` 段（`RiskDefaults`）取严合并后
得到生效限额；`RiskConfig` 仍是「单策略、已解析」的视图，合并逻辑在
`quant.risk.limits.resolve_limits`。
"""
from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from quant.foundation.config import RiskDefaults
from quant.foundation.logger import get_logger
from quant.risk.limits import LIMIT_FIELDS, resolve_limits

log = get_logger("risk.config")


class RiskConfig(BaseModel):
    """单策略风控参数（已合并的生效值）。"""

    strategy_id: str
    max_position_pct: Decimal = Decimal("0.3")
    max_total_pct: Decimal = Decimal("0.8")
    max_order_qty: Decimal = Decimal("10000")  # 单笔最大名义价值（USDT）
    daily_loss_limit: Decimal = Decimal("0.05")
    consecutive_loss_limit: int = 3


def load_strategy_risk(
    strategy_id: str,
    config_dir: Path = Path("config/strategies"),
) -> dict[str, object] | None:
    """读取策略 toml 的 `[risk]` 段原文。

    Returns:
        `[risk]` 段字典；缺段时返回 None（由调用方回落全局层/代码默认）。

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 文件声明的 strategy_id 与参数不一致
    """
    path = Path(config_dir) / f"{strategy_id}.toml"
    with path.open("rb") as f:
        data = tomllib.load(f)

    declared = data.get("strategy", {}).get("strategy_id", strategy_id)
    if declared != strategy_id:
        raise ValueError(
            f"strategy_id mismatch: file declares {declared!r}, "
            f"expected {strategy_id!r}"
        )
    return data.get("risk")


def load_risk_config(
    strategy_id: str,
    config_dir: Path = Path("config/strategies"),
    defaults: RiskDefaults | None = None,
) -> RiskConfig:
    """从 {config_dir}/{strategy_id}.toml 读取 `[risk]` 并与全局层取严。

    缺 `[risk]` 段时不再抛错，全部字段回落全局层/代码默认，并打一条
    WARNING——否则参数来源会变得不可见，排查时无从判断用的是哪一层。

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: strategy_id 声明不一致
    """
    strategy_risk = load_strategy_risk(strategy_id, config_dir=config_dir)
    if strategy_risk is None:
        log.warning(
            f"missing [risk] section for {strategy_id}, "
            "falling back to global/default limits"
        )
    else:
        unknown = set(strategy_risk) - set(LIMIT_FIELDS)
        if unknown:
            log.warning(
                f"unknown [risk] keys for {strategy_id}: {sorted(unknown)}"
            )

    limits = resolve_limits(
        strategy_id, defaults=defaults, strategy_risk=strategy_risk
    )
    return RiskConfig(
        strategy_id=strategy_id,
        max_position_pct=limits.max_position_pct,
        max_total_pct=limits.max_total_pct,
        max_order_qty=limits.max_order_qty,
        daily_loss_limit=limits.daily_loss_limit,
        consecutive_loss_limit=limits.consecutive_loss_limit,
    )


__all__ = ["RiskConfig", "load_risk_config", "load_strategy_risk"]
