"""Pine 基线副本与 Python 生产基准的一致性登记（M3-6 / M3-13）。

基准口径（2026-11-04 决策）：**Python + `config/strategies/*.toml` 是唯一基准**，
`.pine` 只是验证最基础逻辑的基线策略副本，允许参数与逻辑差异。但差异必须
显式登记——任何新增漂移都会让测试失败，而不是靠 README 一句「严格对齐」
自欺欺人。

校验逻辑已抽到 `quant.strategies.consistency`，与 `quant check-strategies`
共用同一份登记表，避免测试与 CLI 各说各话。
"""
from __future__ import annotations

from quant.strategies.consistency import (
    CONFIG_DIR,
    KNOWN_PINE_DRIFT,
    KNOWN_SPEC_DRIFT,
    PINE_DIR,
    pine_drift,
    pine_params,
    spec_defaults,
    spec_drift,
    toml_params,
)


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
        pine = pine_params(PINE_DIR / f"{name}.pine")
        toml = toml_params(CONFIG_DIR / f"{name}.toml.example")
        assert pine_drift(pine, toml) == expected, f"{name} 出现未登记的 Pine 漂移"


def test_spec_defaults_drift_matches_registration() -> None:
    """PARAM_SPEC 默认值与 toml 默认值的差异必须逐项等于已登记清单。"""
    for name, expected in KNOWN_SPEC_DRIFT.items():
        spec = spec_defaults(name)
        toml = toml_params(CONFIG_DIR / f"{name}.toml.example")
        assert spec_drift(spec, toml) == expected, f"{name} 出现未登记的默认值漂移"
