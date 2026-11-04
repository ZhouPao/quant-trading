"""Pine 基线副本与 Python 生产基准的一致性登记（M3-6 / M3-13）。

基准口径（2026-11-04 决策）：**Python + `config/strategies/*.toml` 是唯一基准**，
`.pine` 只是验证最基础逻辑的基线策略副本，允许参数与逻辑差异。但差异必须
显式登记在这里——任何新增漂移都会让测试失败，而不是靠 README 一句
「严格对齐」自欺欺人。

同时登记 `PARAM_SPEC` 声明默认值与 toml 默认值的差异：页面展示的默认值
与「实际生效默认值」本就不同，这个漂移也需要被管住。
"""
from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINE_DIR = ROOT / "src" / "quant" / "strategies" / "pine"
CONFIG_DIR = ROOT / "config" / "strategies"

STRATEGY_CLASSES = {
    "sma_cross": "quant.strategies.python.sma_cross.SmaCrossStrategy",
    "rsi": "quant.strategies.python.rsi.RsiStrategy",
}

#: 已登记并接受的 Pine ↔ toml.example 差异（新增漂移会让测试失败）
KNOWN_PINE_DRIFT = {
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
KNOWN_SPEC_DRIFT = {
    "sma_cross": {"ema_filter": ("false", "true"), "slow_len": ("50", "100")},
    "rsi": {},
}


def _pine_params(path: Path) -> dict[str, str]:
    """解析 Pine 的 `name = input(value, ...)` 声明。"""
    text = path.read_text(encoding="utf-8")
    return {
        match.group(1): match.group(2).strip()
        for match in re.finditer(
            r"^\s*(\w+)\s*=\s*input\(\s*([^,)]+)", text, re.MULTILINE
        )
    }


def _toml_params(path: Path) -> dict[str, str]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return {
        key: str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in data.get("params", {}).items()
    }


def _spec_defaults(name: str) -> dict[str, str]:
    module_name, _, class_name = STRATEGY_CLASSES[name].rpartition(".")
    cls = getattr(importlib.import_module(module_name), class_name)
    return {
        key: str(spec.default).lower() if isinstance(spec.default, bool)
        else str(spec.default)
        for key, spec in cls.PARAM_SPEC.items()
    }


def _drift(pine: dict[str, str], toml: dict[str, str]) -> dict:
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


def test_pine_files_declare_baseline_role() -> None:
    """每个 Pine 文件头必须声明「基线策略副本」，避免被误当生产镜像。"""
    files = sorted(PINE_DIR.glob("*.pine"))
    assert files, "no pine strategy files found"
    for path in files:
        head = path.read_text(encoding="utf-8")[:500]
        assert "基线策略副本" in head, f"{path.name} 未声明基线定位"


def test_every_pine_is_registered_and_has_toml_example() -> None:
    names = {path.stem for path in PINE_DIR.glob("*.pine")}
    assert names == set(KNOWN_PINE_DRIFT), "新增/删除 Pine 策略需同步登记漂移"
    assert names == set(KNOWN_SPEC_DRIFT), "新增/删除 Pine 策略需同步登记 spec 漂移"
    for name in names:
        assert (CONFIG_DIR / f"{name}.toml.example").exists(), (
            f"{name} 缺少入 git 的 toml.example，无法做一致性校验"
        )


def test_pine_drift_matches_registration() -> None:
    """Pine 与生产 toml 的差异必须逐项等于已登记清单。"""
    for name, expected in KNOWN_PINE_DRIFT.items():
        pine = _pine_params(PINE_DIR / f"{name}.pine")
        toml = _toml_params(CONFIG_DIR / f"{name}.toml.example")
        assert _drift(pine, toml) == expected, f"{name} 出现未登记的 Pine 漂移"


def test_spec_defaults_drift_matches_registration() -> None:
    """PARAM_SPEC 默认值与 toml 默认值的差异必须逐项等于已登记清单。"""
    for name, expected in KNOWN_SPEC_DRIFT.items():
        spec = _spec_defaults(name)
        toml = _toml_params(CONFIG_DIR / f"{name}.toml.example")
        mismatch = {
            key: (spec[key], toml[key])
            for key in sorted(set(spec) & set(toml))
            if spec[key] != toml[key]
        }
        assert mismatch == expected, f"{name} 出现未登记的默认值漂移"
