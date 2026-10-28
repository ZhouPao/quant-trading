"""从 config/strategies/{id}.toml 加载策略类与参数。

日报（偏离比对）与实时信号引擎共用，避免两处各写一份动态导入。
"""
from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

from quant.backtest.strategy import Strategy


class StrategyConfigError(Exception):
    """策略配置缺失或非法，消息可直接记日志。"""


def load_strategy_class(
    strategy_id: str, config_dir: Path | str
) -> tuple[type[Strategy], dict]:
    """读取 toml，返回 (策略类, 参数 dict)。

    返回类而不是实例，供网格寻优按不同参数反复构造。

    Raises:
        StrategyConfigError: 文件缺失、缺 strategy.class、类导入失败
    """
    path = Path(config_dir) / f"{strategy_id}.toml"
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError as e:
        raise StrategyConfigError(f"strategy config not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise StrategyConfigError(f"invalid toml {path}: {e}") from e

    class_path = data.get("strategy", {}).get("class")
    if not class_path:
        raise StrategyConfigError(f"missing strategy.class in {path}")

    module_name, _, class_name = class_path.rpartition(".")
    if not module_name:
        raise StrategyConfigError(f"invalid strategy.class {class_path!r} in {path}")
    try:
        module = importlib.import_module(module_name)
        strategy_cls = getattr(module, class_name)
    except (ImportError, AttributeError) as e:
        raise StrategyConfigError(f"cannot load {class_path}: {e}") from e

    return strategy_cls, dict(data.get("params", {}))


def load_strategy(
    strategy_id: str, config_dir: Path | str
) -> tuple[Strategy, dict]:
    """读取 toml，返回 (策略实例, 参数 dict)。

    Raises:
        StrategyConfigError: 文件缺失、缺 strategy.class、类导入失败
    """
    strategy_cls, params = load_strategy_class(strategy_id, config_dir)
    return strategy_cls(), params


__all__ = ["StrategyConfigError", "load_strategy", "load_strategy_class"]
