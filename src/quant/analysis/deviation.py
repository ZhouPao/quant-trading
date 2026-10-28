"""模拟盘 vs 回测偏离检测（spec §5.2，M3）。

同一策略在同一时间窗内：
- 回测：本地 K线 + Matcher 模拟成交
- 模拟盘：OKX 模拟盘真实成交（SQLite fills 表）

对比成交笔数、数量、名义金额、手续费与买卖方向序列，
任一指标偏离超阈值（默认 5%）即判定不通过，禁止进入实盘。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant.backtest.order import Fill

DEFAULT_THRESHOLD = 0.05


@dataclass(frozen=True, slots=True)
class FillSummary:
    """成交汇总。"""

    trade_count: int
    buy_qty: Decimal
    sell_qty: Decimal
    notional: Decimal
    fee: Decimal


@dataclass(frozen=True, slots=True)
class DeviationReport:
    """偏离报告。"""

    paper: FillSummary
    backtest: FillSummary
    trade_count_dev: float
    qty_dev: float
    notional_dev: float
    fee_dev: float
    side_sequence_match: bool
    max_deviation: float
    threshold: float
    within_threshold: bool


def summarize(fills: list[Fill]) -> FillSummary:
    """汇总成交：笔数、买卖数量、名义金额、手续费。"""
    buy_qty = Decimal("0")
    sell_qty = Decimal("0")
    notional = Decimal("0")
    fee = Decimal("0")

    for fill in fills:
        if fill.side == "buy":
            buy_qty += fill.qty
        else:
            sell_qty += fill.qty
        notional += fill.qty * fill.price
        fee += fill.fee

    return FillSummary(
        trade_count=len(fills),
        buy_qty=buy_qty,
        sell_qty=sell_qty,
        notional=notional,
        fee=fee,
    )


def relative_deviation(actual: Decimal | float, base: Decimal | float) -> float:
    """相对偏离 |actual - base| / |base|。

    基准为 0 时：actual 也为 0 返回 0，否则返回 1.0（100% 偏离）。
    """
    a = float(actual)
    b = float(base)
    if b == 0:
        return 0.0 if a == 0 else 1.0
    return abs(a - b) / abs(b)


def compare_fills(
    paper: list[Fill],
    backtest: list[Fill],
    threshold: float = DEFAULT_THRESHOLD,
) -> DeviationReport:
    """对比模拟盘成交与回测成交。

    Args:
        paper: 模拟盘成交（按 ts 升序）
        backtest: 回测成交（按 ts 升序，同一时间窗）
        threshold: 允许的最大偏离（默认 5%）

    Raises:
        ValueError: threshold 不在 [0, 1]
    """
    if not 0 <= threshold <= 1:
        raise ValueError(f"threshold must be in [0, 1], got {threshold}")

    p = summarize(paper)
    b = summarize(backtest)

    trade_count_dev = relative_deviation(p.trade_count, b.trade_count)
    qty_dev = relative_deviation(p.buy_qty + p.sell_qty, b.buy_qty + b.sell_qty)
    notional_dev = relative_deviation(p.notional, b.notional)
    fee_dev = relative_deviation(p.fee, b.fee)

    side_sequence_match = [f.side for f in paper] == [f.side for f in backtest]

    max_deviation = max(trade_count_dev, qty_dev, notional_dev, fee_dev)
    within = side_sequence_match and max_deviation <= threshold

    return DeviationReport(
        paper=p,
        backtest=b,
        trade_count_dev=trade_count_dev,
        qty_dev=qty_dev,
        notional_dev=notional_dev,
        fee_dev=fee_dev,
        side_sequence_match=side_sequence_match,
        max_deviation=max_deviation,
        threshold=threshold,
        within_threshold=within,
    )


def format_report(report: DeviationReport) -> str:
    """渲染为纯文本表（CLI 输出）。"""
    status = "PASS" if report.within_threshold else "FAIL"
    rows = [
        ("成交笔数", report.paper.trade_count, report.backtest.trade_count,
         report.trade_count_dev),
        ("成交数量", report.paper.buy_qty + report.paper.sell_qty,
         report.backtest.buy_qty + report.backtest.sell_qty, report.qty_dev),
        ("名义金额", report.paper.notional, report.backtest.notional,
         report.notional_dev),
        ("手续费", report.paper.fee, report.backtest.fee, report.fee_dev),
    ]

    lines = [
        f"[{status}] 模拟盘 vs 回测偏离（阈值 {report.threshold:.1%}）",
        f"{'指标':<10}{'模拟盘':>24}{'回测':>24}{'偏离':>10}",
    ]
    for name, paper_val, bt_val, dev in rows:
        lines.append(f"{name:<10}{_fmt(paper_val):>24}{_fmt(bt_val):>24}{dev:>9.2%}")
    lines.append(
        f"{'方向序列':<10}"
        f"{'一致' if report.side_sequence_match else '不一致':>48}"
    )
    lines.append(f"最大偏离: {report.max_deviation:.2%}")
    return "\n".join(lines)


def _fmt(value) -> str:
    if isinstance(value, Decimal):
        return f"{value:.4f}"
    return str(value)


__all__ = [
    "DEFAULT_THRESHOLD",
    "DeviationReport",
    "FillSummary",
    "compare_fills",
    "format_report",
    "relative_deviation",
    "summarize",
]
