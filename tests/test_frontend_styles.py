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
    assert "first_sync_bars = 300" in text


def test_m6_portfolio_page_wiring() -> None:
    """M6-14 组合页：前后端契约同步、占位卡移除、四个视图齐备。"""
    api_ts = (SRC / "api.ts").read_text(encoding="utf-8")
    panel_ts = (SRC / "portfolio" / "PortfolioPanel.tsx").read_text(
        encoding="utf-8"
    )
    assert '"/api/portfolio"' in api_ts
    assert "export type PortfolioResponse" in api_ts
    assert "fetchPortfolio" in api_ts
    assert "PortfolioPanel" in APP
    assert "ModulePlaceholder" not in APP
    for keyword in ("目标权重", "组合权益与回撤", "各单元贡献", "再平衡记录"):
        assert keyword in panel_ts
    assert "PieChart" in (SRC / "chart" / "EChart.tsx").read_text(
        encoding="utf-8"
    )
    assert ".pf-grid" in CSS


def test_p02_readiness_checklist() -> None:
    """P0-2：上线检查清单接口与 M3 页卡片齐备。"""
    api_ts = (SRC / "api.ts").read_text(encoding="utf-8")
    assert "ReadinessResponse" in api_ts
    assert "fetchReadiness" in api_ts
    assert "上线检查清单" in APP
    assert "ready-item ready-${item.status}" in APP
    assert ".ready-card" in CSS
    assert ".ready-pass .ready-dot" in CSS


def test_m812_order_source_filter() -> None:
    """M8-12：订单支持按来源筛选并展示来源列。"""
    api_ts = (SRC / "api.ts").read_text(encoding="utf-8")
    assert "source?: string" in api_ts
    assert "source: string | null" in api_ts
    assert '<option value="portfolio">组合调仓' in APP
    assert '<option value="manual">手工单' in APP
    assert "SOURCE_LABELS" in APP
    assert '"来源"' in APP


def test_candles_page_labels_market_regime() -> None:
    """K 线页必须标注行情口径，避免把模拟盘行情误判成数据错误。"""
    api_ts = (SRC / "api.ts").read_text(encoding="utf-8")
    assert "demo: boolean" in api_ts
    assert "行情口径" in APP
    assert 'data?.demo === false' in APP


def test_m3_engine_sections_have_real_headings_and_collapse() -> None:
    """M3 归位：6 个区块要有真 h3 标题、可折叠、偏好持久化。"""
    section_ts = (SRC / "engine" / "Section.tsx").read_text(encoding="utf-8")
    for sid in ("params", "history", "bindings", "evaluations", "runs", "compare"):
        assert f"{sid}:" in section_ts, f"缺少分节 {sid}"
        assert f'id="{sid}"' in APP, f"页面未接入分节 {sid}"
    assert "<h3" in section_ts
    assert "aria-expanded" in section_ts
    assert "localStorage" in section_ts
    assert APP.count("data-collapsed={!sectionOpen.") == 6
    assert ".engine-section-head" in CSS
    assert '[data-collapsed="true"] > :not(.engine-section-head)' in CSS
    # card-note 的右对齐不能全局生效（会污染分区标题）
    assert ".card-head .card-note" in CSS
    assert ".param-head .card-note" in CSS


def test_m3_section_detail_cleanup() -> None:
    """阶段2：告警合并、说明收敛、绑定表单网格化、列表行对齐。"""
    assert "bindingAlerts" in APP
    assert "engine-status-bar" in APP
    assert "param-head binding-form" in APP
    assert ".engine-status-bar" in CSS
    assert "-webkit-line-clamp: 2" in CSS
    assert ".param-head.binding-form" in CSS
    assert ".param-history li" in CSS


def test_m3_strategy_switcher() -> None:
    """阶段3：策略清单可切换查看，且不改引擎运行策略。"""
    assert "engine-strategy-bar" in APP
    assert "setStrategyList(res.strategies)" in APP
    assert "selectedStrategy ?? runningStrategy" in APP
    assert "engine-strategy-running" in APP
    assert ".engine-strategy-chip" in CSS


def test_m3_links_to_m4_research_modes() -> None:
    """阶段4：M3 参数区串联 M4 三种稳健性检验，且不能自动提交任务。"""
    assert 'mode?: "optimize" | "batch" | "walkforward"' in APP
    assert 'setMode(preset.mode ?? "backtest")' in APP
    assert "else if (preset.mode) {" in APP
    for mode in ("optimize", "walkforward", "batch"):
        assert f'mode: "{mode}"' in APP


def test_sidebar_collapse_control() -> None:
    """左侧模块导航可收缩：DOM 状态、持久化与 CSS 都齐备。"""
    assert "NAV_COLLAPSED_KEY" in APP
    assert "loadNavCollapsed" in APP
    assert "nav-toggle" in APP
    assert "layout-nav-collapsed" in APP
    assert "sidebar-collapsed" in APP
    assert "aria-expanded={!navCollapsed}" in APP
    assert "aria-controls=\"module-nav-list\"" in APP
    assert ".nav-toggle" in CSS
    assert ".layout-nav-collapsed" in CSS
    assert ".sidebar-collapsed .nav-label" in CSS
    assert ".sidebar-collapsed .nav-item" in CSS


def test_subplot_height_and_min_bar_height() -> None:
    """小成交量柱不能被极端峰值压成不可见：柱高保底 + VOL 副图加权。"""
    options_ts = (SRC / "chart" / "options.ts").read_text(encoding="utf-8")
    chart_ts = (SRC / "chart" / "CandleChart.tsx").read_text(encoding="utf-8")
    assert "barMinHeight: MIN_BAR_HEIGHT" in options_ts
    assert "layoutGrids(subKinds)" in options_ts
    assert 'kind === "vol" ? 1.5 : 1' in options_ts
    assert "340 + subCount * 160" in chart_ts


def test_chart_bar_width_cap_and_sparse_hint() -> None:
    """本地数据少时柱宽有上限，并显式提示补历史（M2-12）。"""
    options_ts = (SRC / "chart" / "options.ts").read_text(encoding="utf-8")
    assert "MAX_BAR_WIDTH" in options_ts
    assert options_ts.count("barMaxWidth: MAX_BAR_WIDTH") == 3
    assert "SPARSE_BARS" in APP
    assert "该周期本地仅" in APP
    # M6-14 组合页已上线（8-4），导航不再是 todo
    assert '{ key: "portfolio", label: "组合管理", status: "live" }' in APP
