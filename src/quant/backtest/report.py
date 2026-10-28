"""回测报告生成（spec §4.6）。

- CSV：交易明细 + 权益/回撤曲线，统一表结构（record_type 区分）
- HTML：指标表 + 内联 SVG 权益曲线/回撤曲线 + 交易明细表

不引入 matplotlib/jinja2 依赖，HTML 用 f-string + 内联 SVG 生成。
"""
from __future__ import annotations

import csv
from decimal import Decimal
from html import escape
from pathlib import Path

from quant.backtest.engine import BacktestResult

_CSV_FIELDS = [
    "record_type",
    "ts",
    "inst_id",
    "side",
    "qty",
    "price",
    "fee",
    "equity",
    "drawdown",
]

_METRIC_LABELS = [
    ("total_return", "总收益率"),
    ("annual_return", "年化收益率"),
    ("sharpe", "夏普比率"),
    ("max_drawdown", "最大回撤"),
    ("win_rate", "胜率"),
    ("profit_factor", "盈亏比"),
    ("trade_count", "交易次数"),
    ("avg_holding_bars", "平均持仓K线数"),
]

_PCT_KEYS = {"total_return", "annual_return", "max_drawdown", "win_rate"}


class ReportGenerator:
    """CSV + HTML 报告生成器。"""

    def to_csv(self, result: BacktestResult, path: Path) -> None:
        """输出统一 CSV：交易明细 + 权益/回撤曲线。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        rows: list[dict] = []
        for i, (ts, equity) in enumerate(result.equity_curve):
            drawdown = (
                result.drawdown_curve[i][1]
                if i < len(result.drawdown_curve)
                else Decimal("0")
            )
            rows.append(
                {
                    "record_type": "equity",
                    "ts": ts,
                    "inst_id": "",
                    "side": "",
                    "qty": "",
                    "price": "",
                    "fee": "",
                    "equity": str(equity),
                    "drawdown": str(drawdown),
                }
            )

        for fill in result.trades:
            rows.append(
                {
                    "record_type": "trade",
                    "ts": fill.ts,
                    "inst_id": fill.inst_id,
                    "side": fill.side,
                    "qty": str(fill.qty),
                    "price": str(fill.price),
                    "fee": str(fill.fee),
                    "equity": "",
                    "drawdown": "",
                }
            )

        rows.sort(key=lambda r: (r["ts"], r["record_type"]))

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def to_html(self, result: BacktestResult, path: Path) -> None:
        """输出单文件 HTML 报告（内联 SVG，无外部依赖）。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        equity_values = [float(e) for _, e in result.equity_curve]
        drawdown_values = [float(d) for _, d in result.drawdown_curve]

        metrics_rows = "\n".join(
            f"<tr><td>{label}</td><td>{_fmt_metric(key, result.metrics.get(key, 0))}</td></tr>"
            for key, label in _METRIC_LABELS
        )

        trade_rows = "\n".join(
            "<tr>"
            f"<td>{fill.ts}</td>"
            f"<td>{escape(fill.inst_id)}</td>"
            f"<td>{escape(fill.side)}</td>"
            f"<td>{fill.qty}</td>"
            f"<td>{fill.price}</td>"
            f"<td>{fill.fee}</td>"
            "</tr>"
            for fill in result.trades
        ) or '<tr><td colspan="6">无成交</td></tr>'

        html = _HTML_TEMPLATE.format(
            metrics_rows=metrics_rows,
            equity_chart=_svg_line_chart(equity_values, "#2563eb", "权益曲线"),
            drawdown_chart=_svg_line_chart(
                [d * 100 for d in drawdown_values], "#dc2626", "回撤曲线 (%)"
            ),
            trade_rows=trade_rows,
            trade_count=len(result.trades),
            unfilled_count=len(result.unfilled_signals),
            final_equity=equity_values[-1] if equity_values else 0.0,
        )
        path.write_text(html, encoding="utf-8")


def _fmt_metric(key: str, value: float) -> str:
    if value == float("inf"):
        return "inf"
    if key in _PCT_KEYS:
        return f"{value * 100:.2f}%"
    if key == "trade_count":
        return f"{int(value)}"
    return f"{value:.4f}"


def _svg_line_chart(values: list[float], color: str, title: str) -> str:
    """把数值序列画成内联 SVG 折线图。"""
    width, height, pad = 800, 220, 30
    if not values:
        return f'<svg width="{width}" height="{height}"></svg>'

    vmin, vmax = min(values), max(values)
    span = vmax - vmin
    if span == 0:
        span = 1.0
    n = len(values)

    points = []
    for i, v in enumerate(values):
        x = pad if n == 1 else pad + i * (width - 2 * pad) / (n - 1)
        y = height - pad - (v - vmin) / span * (height - 2 * pad)
        points.append(f"{x:.1f},{y:.1f}")

    return (
        f'<svg width="{width}" height="{height}" role="img" aria-label="{escape(title)}">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fafafa"/>'
        f'<text x="{pad}" y="18" font-size="12" fill="#555">{escape(title)}</text>'
        f'<text x="{pad}" y="{height - 6}" font-size="10" fill="#888">min={vmin:.2f}</text>'
        f'<text x="{width - pad - 80}" y="{height - 6}" font-size="10" fill="#888">'
        f"max={vmax:.2f}</text>"
        f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
        f'points="{" ".join(points)}"/>'
        "</svg>"
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>回测报告</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", sans-serif; margin: 24px; color: #222; }}
  h1 {{ font-size: 20px; }}
  h2 {{ font-size: 16px; margin-top: 28px; }}
  table {{ border-collapse: collapse; margin: 8px 0; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 12px; font-size: 13px; text-align: right; }}
  th {{ background: #f3f4f6; }}
  td:first-child, th:first-child {{ text-align: left; }}
  .summary {{ color: #555; font-size: 13px; }}
  svg {{ display: block; margin: 8px 0 16px; }}
</style>
</head>
<body>
<h1>回测报告</h1>
<p class="summary">成交数 {trade_count}；未成交信号 {unfilled_count}；期末权益
{final_equity:.2f}</p>

<h2>绩效指标</h2>
<table>
<tr><th>指标</th><th>值</th></tr>
{metrics_rows}
</table>

<h2>权益曲线</h2>
{equity_chart}

<h2>回撤曲线</h2>
{drawdown_chart}

<h2>交易明细</h2>
<table>
<tr><th>时间戳(ms)</th><th>品种</th><th>方向</th><th>数量</th><th>价格</th><th>手续费</th></tr>
{trade_rows}
</table>
</body>
</html>
"""


__all__ = ["ReportGenerator"]
