"""backtest 命令：在本地历史数据上跑策略回测。

用法：
  quant backtest --strategy sma_cross --inst BTC-USDT --bar 1H \
                 --start 2025-01-01 --end 2025-12-31
  quant backtest --strategy rsi --params "rsi_period=14;oversold=25" --no-report

输出：终端打印绩效指标；默认同时写 CSV + HTML 到 `reports/backtest/`。
只统计 [start, end] 区间，区间前的 bar 仅用于预热（不成交、不计入权益）。
"""
from __future__ import annotations

from pathlib import Path

from quant.backtest.engine import Backtest
from quant.backtest.report import ReportGenerator
from quant.backtest.runner import (
    BacktestSetupError,
    build_matcher,
    format_metrics,
    load_bars,
    load_funding_schedule,
    parse_range,
    report_paths,
)
from quant.cli._params import resolve_cli_params
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.strategies.loader import StrategyConfigError
from quant.strategies.params import ParamError


def main(args) -> int:
    """执行回测，返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.backtest")

    try:
        start_ts, end_ts = parse_range(args.start, args.end)
        warmup, bars = load_bars(
            settings,
            args.inst,
            args.bar,
            start_ts,
            end_ts,
            args.warmup_bars,
        )
        strategy_cls, params, _sources = resolve_cli_params(settings, args)
        funding = load_funding_schedule(
            settings, args.inst, enabled=args.funding or None
        )
    except BacktestSetupError as e:
        log.error(str(e))
        return 1
    except (StrategyConfigError, ParamError) as e:
        log.error(str(e))
        return 1

    result = Backtest(
        strategy_cls(),
        build_matcher(settings),
        initial_cash=settings.backtest.initial_cash,
        window_size=settings.backtest.window_size,
        params=params,
        warmup_bars=warmup,
        funding=funding,
    ).run(bars)

    log.info(
        f"backtest done strategy={args.strategy} inst={args.inst} bar={args.bar} "
        f"range={args.start}..{args.end} bars={len(bars)} "
        f"warmup={len(warmup)} trades={len(result.trades)}"
    )
    print(format_metrics(result.metrics))

    if args.no_report:
        return 0

    csv_path, html_path = report_paths(
        Path(args.out_dir),
        args.strategy,
        args.inst,
        args.bar,
        args.start,
        args.end,
    )
    generator = ReportGenerator()
    generator.to_csv(result, csv_path)
    generator.to_html(result, html_path)
    print(f"\nCSV : {csv_path}\nHTML: {html_path}")
    return 0
