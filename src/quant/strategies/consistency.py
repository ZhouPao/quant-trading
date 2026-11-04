"""双副本一致性校验（M3-13）。

基准口径：**Python + `config/strategies/*.toml` 是唯一生产基准**，`.pine` 只是
基线策略副本。差异可以存在，但必须**显式登记**——新增漂移应让 `--strict`
校验失败，而不是靠 README 一句「严格对齐」自欺欺人。

这里同时被 `tests/strategies/test_pine_python_consistency.py` 与
`quant check-strategies` 复用，保证测试与 CLI 不会各说各话。
"""
from __future__ import annotations

import importlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: 项目根（本文件位于 src/quant/strategies/）
ROOT = Path(__file__).resolve().parents[3]
PINE_DIR = ROOT / "src" / "quant" / "strategies" / "pine"
CONFIG_DIR = ROOT / "config" / "strategies"

#: 策略 ID -> Python 类（PARAM_SPEC 来源）
STRATEGY_CLASSES = {
    "sma_cross": "quant.strategies.python.sma_cross.SmaCrossStrategy",
    "rsi": "quant.strategies.python.rsi.RsiStrategy",
}

#: 已登记并接受的 Pine ↔ toml.example 差异
KNOWN_PINE_DRIFT: dict[str, dict] = {
    "sma_cross": {
        "pine_only": [],
        "toml_only": [
            "atr_len",
            "commission_rate",
            "ema_band_atr",
            "ema_filter",
            "ema_len",
        ],
        "value_mismatch": {"slow_len": ("50", "100")},
    },
    "rsi": {
        "pine_only": ["period"],
        "toml_only": ["commission_rate", "rsi_period"],
        "value_mismatch": {},
    },
}

#: 已登记并接受的 PARAM_SPEC 默认值 ↔ toml 默认值差异
KNOWN_SPEC_DRIFT: dict[str, dict] = {
    "sma_cross": {"ema_filter": ("false", "true"), "slow_len": ("50", "100")},
    "rsi": {},
}

#: Pine 文件头必须声明基线定位
BASELINE_MARK = "基线策略副本"


@dataclass(frozen=True, slots=True)
class DriftItem:
    """一项差异；`known=False` 表示未登记。"""

    strategy: str
    kind: str
    key: str
    detail: str
    known: bool

    def text(self) -> str:
        mark = "已登记" if self.known else "未登记"
        return f"[{mark}] {self.strategy}.{self.key} ({self.kind}) {self.detail}"


def pine_params(path: Path) -> dict[str, str]:
    """解析 Pine 的 `name = input(value, ...)` 声明。"""
    text = path.read_text(encoding="utf-8")
    return {
        match.group(1): match.group(2).strip()
        for match in re.finditer(
            r"^\s*(\w+)\s*=\s*input\(\s*([^,)]+)", text, re.MULTILINE
        )
    }


def toml_params(path: Path) -> dict[str, str]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return {
        key: str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in data.get("params", {}).items()
    }


def spec_defaults(strategy_id: str) -> dict[str, str]:
    module_name, _, class_name = STRATEGY_CLASSES[strategy_id].rpartition(".")
    cls = getattr(importlib.import_module(module_name), class_name)
    return {
        key: str(spec.default).lower()
        if isinstance(spec.default, bool)
        else str(spec.default)
        for key, spec in cls.PARAM_SPEC.items()
    }


def pine_drift(
    pine: dict[str, str], toml: dict[str, str]
) -> dict[str, object]:
    """Pine 与 toml 的差异（结构与登记表一致，便于直接比较）。"""
    common = set(pine) & set(toml)
    return {
        "pine_only": sorted(set(pine) - set(toml)),
        "toml_only": sorted(set(toml) - set(pine)),
        "value_mismatch": {
            key: (pine[key], toml[key])
            for key in sorted(common)
            if pine[key] != toml[key]
        },
    }


def spec_drift(spec: dict[str, str], toml: dict[str, str]) -> dict:
    """PARAM_SPEC 默认值与 toml 默认值的差异。"""
    return {
        key: (spec[key], toml[key])
        for key in sorted(set(spec) & set(toml))
        if spec[key] != toml[key]
    }


def _pine_known(
    strategy: str, kind: str, key: str, pair: tuple | None = None
) -> bool:
    registered = KNOWN_PINE_DRIFT.get(strategy, {})
    if kind in ("pine_only", "toml_only"):
        return key in registered.get(kind, [])
    if kind == "value_mismatch":
        return registered.get("value_mismatch", {}).get(key) == pair
    return False


def _spec_known(strategy: str, key: str, pair: tuple) -> bool:
    return KNOWN_SPEC_DRIFT.get(strategy, {}).get(key) == pair


def discover(pine_dir: Path, config_dir: Path) -> list[str]:
    """有 `.pine` 且有 `*.toml.example` 的策略 ID（按字母序）。"""
    return sorted(
        path.stem
        for path in pine_dir.glob("*.pine")
        if (config_dir / f"{path.stem}.toml.example").is_file()
    )


def check(
    pine_dir: Path | None = None, config_dir: Path | None = None
) -> list[DriftItem]:
    """逐策略比对，返回全部差异（含已登记）；`known` 区分是否登记过。"""
    pine_dir = pine_dir or PINE_DIR
    config_dir = config_dir or CONFIG_DIR
    items: list[DriftItem] = []

    for path in sorted(pine_dir.glob("*.pine")):
        strategy = path.stem
        head = path.read_text(encoding="utf-8")[:500]
        if BASELINE_MARK not in head:
            items.append(
                DriftItem(
                    strategy, "missing_baseline_decl", path.name,
                    "文件头未声明「基线策略副本」", False,
                )
            )

        toml_path = config_dir / f"{strategy}.toml.example"
        if not toml_path.is_file():
            items.append(
                DriftItem(
                    strategy, "missing_toml", path.name,
                    f"缺少 {toml_path.name}，无法校验", False,
                )
            )
            continue

        pine = pine_params(path)
        toml = toml_params(toml_path)
        drift = pine_drift(pine, toml)
        for key in drift["pine_only"]:
            items.append(
                DriftItem(strategy, "pine_only", key, "仅 Pine 有该参数",
                          _pine_known(strategy, "pine_only", key))
            )
        for key in drift["toml_only"]:
            items.append(
                DriftItem(strategy, "toml_only", key, "仅生产 toml 有该参数",
                          _pine_known(strategy, "toml_only", key))
            )
        for key, pair in drift["value_mismatch"].items():
            items.append(
                DriftItem(
                    strategy, "value_mismatch", key,
                    f"Pine={pair[0]} toml={pair[1]}",
                    _pine_known(strategy, "value_mismatch", key, pair),
                )
            )

        if strategy in STRATEGY_CLASSES:
            spec = spec_defaults(strategy)
            for key, pair in spec_drift(spec, toml).items():
                items.append(
                    DriftItem(
                        strategy, "spec_mismatch", key,
                        f"spec={pair[0]} toml={pair[1]}",
                        _spec_known(strategy, key, pair),
                    )
                )
    return items


def unregistered(items: list[DriftItem]) -> list[DriftItem]:
    """过滤出未登记的差异（`--strict` 的失败条件）。"""
    return [item for item in items if not item.known]


__all__ = [
    "BASELINE_MARK",
    "CONFIG_DIR",
    "DriftItem",
    "KNOWN_PINE_DRIFT",
    "KNOWN_SPEC_DRIFT",
    "PINE_DIR",
    "STRATEGY_CLASSES",
    "check",
    "discover",
    "pine_drift",
    "pine_params",
    "spec_defaults",
    "spec_drift",
    "toml_params",
    "unregistered",
]
