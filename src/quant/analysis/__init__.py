"""分析层：回测/模拟盘结果对比（M3）。"""
from quant.analysis.deviation import (
    DEFAULT_THRESHOLD,
    DeviationReport,
    FillSummary,
    compare_fills,
    format_report,
    relative_deviation,
    summarize,
)

__all__ = [
    "DEFAULT_THRESHOLD",
    "DeviationReport",
    "FillSummary",
    "compare_fills",
    "format_report",
    "relative_deviation",
    "summarize",
]
