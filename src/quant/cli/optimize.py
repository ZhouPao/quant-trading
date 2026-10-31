"""optimize 命令：网格搜索策略参数。

用法：
  quant optimize --strategy sma_cross --inst BTC-USDT --bar 1H \
                 --start 2025-01-01 --end 2025-12-31 \
                 --grid "fast_len=5,10,20;slow_len=20,50,100"

输出：按 `--metric` 排序的 Top N 参数组合。`max_drawdown` 越小越好，
其余指标（sharpe/total_return 等）越大越好。
回测口径与 `quant backtest` 一致：只统计 [start, end]，区间前仅预热。
"""
from __future__ import annotations

from quant.backtest.optimizer import GridOptimizer
from quant.backtest.runner import (
    BacktestSetupError,
    build_matcher,
    format_metric,
    load_bars,
    parse_grid,
    parse_params,
    parse_range,
)
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.strategies.loader import StrategyConfigError, load_strategy_class

# 越小越好的指标，需要反向排序
_ASCENDING = {"max_drawdown", "avg_holding_bars"}


def main(args) -> int:
    """执行网格寻优，返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.optimize")

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
        strategy_cls, config_params = load_strategy_class(
            args.strategy, args.config_dir
        )
        fixed_params = parse_params(args.params)
        grid = parse_grid(args.grid)
    except BacktestSetupError as e:
        log.error(str(e))
        return 1
    except StrategyConfigError as e:
        log.error(str(e))
        return 1

    def factory(params: dict):
        return strategy_cls()

    optimizer = GridOptimizer(
        factory,
        grid,
        initial_cash=settings.backtest.initial_cash,
        matcher=build_matcher(settings),
        window_size=settings.backtest.window_size,
        warmup_bars=warmup,
        base_params={**config_params, **fixed_params},
        strategy_cls=strategy_cls,
    )

    combos = 1
    for values in grid.values():
        combos *= len(values)

    limit = args.max_combos or settings.backtest.max_combos
    if combos > limit:
        log.error(
            f"参数组合过多：{combos} 组 > 上限 {limit}，"
            f"缩小网格或用 --max-combos 显式放开"
        )
        return 1

    log.info(
        f"optimize started strategy={args.strategy} inst={args.inst} "
        f"bar={args.bar} range={args.start}..{args.end} "
        f"combos={combos} metric={args.metric} n_jobs={args.n_jobs}"
    )

    def on_progress(done: int, total: int) -> None:
        # 只在整十与收尾时打日志，避免几千行刷屏
        if done == total or done % 10 == 0:
            log.info(f"optimize progress {done}/{total}")

    try:
        results = optimizer.search(
            bars, metric=args.metric, n_jobs=args.n_jobs, progress=on_progress
        )
    except ValueError as e:
        log.error(str(e))
        return 1

    reverse = args.metric not in _ASCENDING
    results.sort(key=lambda item: item[1][args.metric], reverse=reverse)

    skipped = combos - len(results)
    note = f"，跳过 {skipped} 组非法参数" if skipped else ""
    print(
        f"\nTop {min(args.top, len(results))} / {len(results)} combos"
        f"  (metric={args.metric}{note})\n"
    )
    header = "  ".join(f"{name:>12}" for name in grid)
    print(f"{header}  {args.metric:>14}")
    print("-" * (len(header) + len(args.metric) + 4))
    for params, metrics in results[: args.top]:
        cols = "  ".join(f"{str(params[name]):>12}" for name in grid)
        print(f"{cols}  {format_metric(args.metric, metrics[args.metric]):>14}")
    return 0
