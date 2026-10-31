"""回测/寻优命令行共用的准备逻辑。

- 解析 `YYYY-MM-DD` 区间为毫秒
- 从本地 parquet 加载「预热段 + 回测段」
- 按配置构造 Matcher
- 解析 `key=v1,v2` 形式的参数覆盖与网格
- 指标格式化
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from quant.backtest.matcher import Matcher
from quant.foundation.config import Settings
from quant.models.bar import Bar
from quant.storage.bar_storage import BarStorage

DAY_MS = 86_400_000

BAR_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
    "6H": 21_600_000,
    "12H": 43_200_000,
    "1D": 86_400_000,
}


class BacktestSetupError(Exception):
    """回测前置条件不满足（日期非法/无数据等），消息可直接记日志。"""


def parse_date_to_ms(date_str: str) -> int:
    """`YYYY-MM-DD` -> UTC 毫秒时间戳（当日 00:00）。"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as e:
        raise BacktestSetupError(f"invalid date {date_str!r} (expect YYYY-MM-DD)") from e
    return int(dt.timestamp() * 1000)


def parse_range(start: str, end: str) -> tuple[int, int]:
    """返回 [start 当天 00:00, end 次日 00:00)，即含 end 整天。"""
    start_ts = parse_date_to_ms(start)
    end_ts = parse_date_to_ms(end) + DAY_MS
    if start_ts >= end_ts:
        raise BacktestSetupError(f"start {start} must not be after end {end}")
    return start_ts, end_ts


def load_bars(
    settings: Settings,
    inst: str,
    bar: str,
    start_ts: int,
    end_ts: int,
    warmup_bars: int,
) -> tuple[list[Bar], list[Bar]]:
    """返回 (预热段, 回测段)。

    预热段长度 = warmup_bars 根，严格早于 start_ts；
    回测段 = [start_ts, end_ts)。
    """
    if bar not in BAR_MS:
        raise BacktestSetupError(
            f"unsupported bar {bar!r}; expect one of {sorted(BAR_MS)}"
        )

    storage = BarStorage(settings.storage.data_dir)
    # 多取一根用于判断预热段边界
    warmup_start = start_ts - (warmup_bars + 1) * BAR_MS[bar]
    raw = storage.load_bars(inst, bar, warmup_start, end_ts)
    if not raw:
        raise BacktestSetupError(
            f"no local bars for {inst} {bar} "
            f"({_fmt(start_ts)}..{_fmt(end_ts)}); run fetch-candles first"
        )

    warmup = [b for b in raw if b.ts < start_ts][-warmup_bars:] if warmup_bars else []
    bars = [b for b in raw if start_ts <= b.ts < end_ts]
    if not bars:
        raise BacktestSetupError(
            f"no bars in range {_fmt(start_ts)}..{_fmt(end_ts)} for {inst} {bar}"
        )
    if warmup_bars and len(warmup) < warmup_bars:
        raise BacktestSetupError(
            f"not enough warmup bars: need {warmup_bars}, got {len(warmup)}; "
            f"start earlier or fetch more history"
        )
    return warmup, bars


def build_matcher(settings: Settings) -> Matcher:
    """按配置构造成交模拟器。"""
    return Matcher(
        slippage_rate=settings.backtest.slippage_bps / Decimal("10000"),
        commission_rate=settings.backtest.commission_rate,
    )


def parse_params(text: str | None) -> dict:
    """`fast_len=10;slow_len=50` -> {"fast_len": 10, "slow_len": 50}。"""
    if not text:
        return {}
    params: dict = {}
    for item in text.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise BacktestSetupError(f"invalid param {item!r} (expect key=value)")
        key, _, raw = item.partition("=")
        params[key.strip()] = _to_scalar(raw.strip())
    return params


def parse_grid(text: str) -> dict[str, list]:
    """`fast_len=5,10,20;slow_len=20,50` -> {"fast_len": [5,10,20], ...}。"""
    grid: dict[str, list] = {}
    for item in text.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise BacktestSetupError(f"invalid grid item {item!r} (expect key=v1,v2)")
        key, _, raw = item.partition("=")
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if not values:
            raise BacktestSetupError(f"empty value list for {key.strip()!r}")
        grid[key.strip()] = [_to_scalar(v) for v in values]
    if not grid:
        raise BacktestSetupError("empty param grid")
    return grid


_METRIC_LABELS = {
    "total_return": "总收益率",
    "annual_return": "年化收益率",
    "sharpe": "夏普比率",
    "max_drawdown": "最大回撤",
    "win_rate": "胜率",
    "profit_factor": "盈亏比",
    "trade_count": "交易次数",
    "avg_holding_bars": "平均持仓K线数",
}

_PCT_METRICS = {"total_return", "annual_return", "max_drawdown", "win_rate"}


def format_metric(key: str, value: float | None) -> str:
    """按指标语义格式化（比率转百分比）；None 显示为占位符。"""
    if value is None:
        return "—"
    if key in _PCT_METRICS:
        return f"{value:.2%}"
    if key == "profit_factor" and value == float("inf"):
        return "inf"
    if key in ("trade_count",):
        return f"{value:.0f}"
    return f"{value:.2f}"


def format_metrics(metrics: dict[str, float]) -> str:
    """指标 dict -> 多行文本（用于终端输出）。"""
    lines = []
    for key, label in _METRIC_LABELS.items():
        if key not in metrics:
            continue
        lines.append(f"  {label:<14}{format_metric(key, metrics[key]):>14}")
    return "\n".join(lines)


def _to_scalar(text: str):
    """按 int -> bool -> float -> str 顺序推断类型。"""
    try:
        return int(text)
    except ValueError:
        pass
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def report_paths(
    out_dir: Path,
    strategy: str,
    inst: str,
    bar: str,
    start: str,
    end: str,
) -> tuple[Path, Path]:
    """回测报告输出路径 (csv, html)。"""
    stem = f"{strategy}-{inst}-{bar}-{start}_{end}"
    return Path(out_dir) / f"{stem}.csv", Path(out_dir) / f"{stem}.html"


__all__ = [
    "BacktestSetupError",
    "BAR_MS",
    "build_matcher",
    "format_metric",
    "format_metrics",
    "load_bars",
    "parse_date_to_ms",
    "parse_grid",
    "parse_params",
    "parse_range",
    "report_paths",
]
