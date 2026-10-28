"""风控配置（spec §10）。

从 config/strategies/{strategy_id}.toml 的 [risk] 段加载。
"""
from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel


class RiskConfig(BaseModel):
    """单策略风控参数。"""

    strategy_id: str
    max_position_pct: Decimal = Decimal("0.3")
    max_total_pct: Decimal = Decimal("0.8")
    max_order_qty: Decimal = Decimal("10000")  # 单笔最大名义价值（USDT）
    daily_loss_limit: Decimal = Decimal("0.05")
    consecutive_loss_limit: int = 3


def load_risk_config(
    strategy_id: str,
    config_dir: Path = Path("config/strategies"),
) -> RiskConfig:
    """从 {config_dir}/{strategy_id}.toml 读取 [risk] 配置。

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 缺少 [risk] 段或 strategy_id 不一致
    """
    path = Path(config_dir) / f"{strategy_id}.toml"
    with path.open("rb") as f:
        data = tomllib.load(f)

    risk = data.get("risk")
    if risk is None:
        raise ValueError(f"missing [risk] section in {path}")

    declared = data.get("strategy", {}).get("strategy_id", strategy_id)
    if declared != strategy_id:
        raise ValueError(
            f"strategy_id mismatch: file declares {declared!r}, "
            f"expected {strategy_id!r}"
        )

    return RiskConfig(strategy_id=strategy_id, **risk)


__all__ = ["RiskConfig", "load_risk_config"]
