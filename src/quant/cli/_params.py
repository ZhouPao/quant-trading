"""CLI 回测参数解析：与看板 / 实盘共用同一真相源（MS4-C §3.2）。

口径（2026-11-04 拍板）：
- 「共用」指共用 `resolve_strategy` 这个**函数与校验**（类型 / 范围 / 未知键 /
  NaN 防护），不是共用**数据源**。
- CLI 是离线研究入口，默认走纯 toml 基线：同一条命令的结果只由 toml +
  `--params` 决定，可复现、与 git 版本对应。`strategy_params` 是看板在线
  调参写入的运维态热参数，若隐式生效，历史回测会随调参静默变化，寻优 /
  walk-forward 的网格还会被混入在线值而失去可解释性。
- 需要「验证当前实盘参数」时用显式 `--use-db-params`；看板 M3-16 一键回测
  本来就读 DB 覆盖，能力并未缺失。
"""
from __future__ import annotations

from typing import Any

from quant.backtest.runner import parse_params
from quant.backtest.strategy import Strategy
from quant.foundation.config import Settings
from quant.state.sqlite_store import SQLiteStore
from quant.strategies.params import resolve_strategy


def resolve_cli_params(
    settings: Settings, args: Any
) -> tuple[type[Strategy], dict[str, Any], dict[str, str]]:
    """解析 CLI 参数，返回 (策略类, 合并后参数, 每字段来源)。

    Raises:
        StrategyConfigError: 策略配置缺失 / 类导入失败
        ParamError: `--params` 类型、范围、未知键或非有限值
    """
    read_db = (
        bool(getattr(args, "use_db_params", False))
        and settings.params.resolve_from_db
    )
    store = SQLiteStore(settings.state.db_path) if read_db else None
    try:
        return resolve_strategy(
            args.strategy,
            args.config_dir,
            store,
            overrides=parse_params(args.params),
            resolve_from_db=read_db,
        )
    finally:
        if store is not None:
            store.close()


__all__ = ["resolve_cli_params"]
