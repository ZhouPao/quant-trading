"""前端样式契约（M5 排版事故复盘）。

`tsc` / `vitest` / `build` 都检测不到「className 写了但 styles.css 没定义」
和「12 列网格里漏写 grid-column」。这些用例用文本契约把两个根因锁死。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "web" / "src"
CSS = (SRC / "styles.css").read_text(encoding="utf-8")
APP = (SRC / "App.tsx").read_text(encoding="utf-8")
ECHART = (SRC / "chart" / "EChart.tsx").read_text(encoding="utf-8")

REQUIRED_CLASSES = (
    "factor-panel",
    "settings-panel",
    "proc-audit",
    "tone-warn",
    "config-table",
    "factor-chart",
    "factor-empty",
    "factor-loading",
    "factor-list",
)


@pytest.mark.parametrize("name", REQUIRED_CLASSES)
def test_class_has_css_definition(name: str) -> None:
    """防止再次出现 .proc-audit 这种「幽灵类名」。"""
    assert re.search(rf"(?m)^\s*\.{re.escape(name)}\b", CSS), (
        f".{name} 在 App.tsx/EChart.tsx 中使用，但 styles.css 没有定义"
    )


def test_pages_declare_full_row_span() -> None:
    assert 'className="card reveal factor-panel"' in APP
    assert 'className="card settings-panel"' in APP
    # 整行跨度必须真的存在，而不是只加了类名
    assert re.search(
        r"\.factor-panel,\s*\.settings-panel\s*\{[^}]*grid-column:\s*1 / -1",
        CSS,
        re.DOTALL,
    )


def test_config_table_is_wrapped_and_wrappable() -> None:
    assert 'className="config-table"' in APP
    assert ".config-table td" in CSS
    assert "white-space: normal" in CSS


def test_echart_applies_classname() -> None:
    assert "className={className}" in ECHART
    assert 'className = "factor-chart"' in ECHART


def test_factor_page_uses_meta_for_default_range() -> None:
    """默认区间必须由真实数据范围推导，不能回到 isoDay(-30)。"""
    assert "fetchFactorMeta" in APP
    assert "defaultRange(info, Date.now())" in APP
    assert "isoDay(-30)" not in APP


def test_no_hardcoded_cross_section_insts() -> None:
    """IC 品种不能硬编码成 BTC/ETH/SOL。"""
    for inst in ('"BTC-USDT", "ETH-USDT", "SOL-USDT"', '"SOL-USDT"'):
        assert inst not in APP.split("function FactorPanel()")[-1]


def test_implemented_tabs_are_not_marked_todo() -> None:
    """已交付的因子库/系统设置不能再挂着「未开始」标签。"""
    assert '{ key: "factors", label: "因子库", status: "live" }' in APP
    assert '{ key: "settings", label: "系统设置", status: "live" }' in APP


NINE_BARS = ("1m", "5m", "15m", "30m", "1H", "4H", "6H", "12H", "1D")


def test_kline_period_buttons_are_all_nine_and_always_visible() -> None:
    """M2-12：9 档常驻，不能再因 available_bars 只有一个周期而隐藏按钮。"""
    bars_ts = (SRC / "bars.ts").read_text(encoding="utf-8")
    for bar in NINE_BARS:
        assert f'"{bar}"' in bars_ts, f"缺少周期 {bar}"
    assert "if (available.length === 0) return BAR_OPTIONS;" not in APP
    assert "barStates.map" in APP
    assert 'state === "missing"' in APP


def test_missing_period_has_css_marker() -> None:
    assert ".seg-missing" in CSS
    assert "seg-missing" in APP


def test_candle_sync_config_covers_all_periods() -> None:
    """A：定时增量同步默认覆盖 9 档，否则新周期永远不会补数据。"""
    example = (
        Path(__file__).resolve().parents[1] / "config" / "config.toml.example"
    )
    text = example.read_text(encoding="utf-8")
    assert "bars = [" in text
    for bar in NINE_BARS:
        assert f'"{bar}"' in text, f"示例配置缺少周期 {bar}"
    # 组合管理确实还没做，保留 todo
    assert '{ key: "portfolio", label: "组合管理", status: "todo" }' in APP
