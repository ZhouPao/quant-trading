"""walk-forward 命令：滚动 IS/OOS 验证参数稳健性。

用法：
  quant walk-forward --strategy sma_cross --inst BTC-USDT-SWAP --bar 4H \
      --start 2024-03-01 --end 2026-10-29 \
      --grid "slow_len=50,100,150;ema_filter=false,true"

流程：按 `--is-bars` / `--oos-bars` 滚动切片；每段在 IS 上按 `--metric`
选最优参数，再用该参数在紧随的 OOS 上回测；最后汇总 OOS 复合收益、
盈利段数与参数稳定性。OOS 只用于评估，不参与选参。
"""
from __future__ import annotations

from datetime import UTC, datetime

from quant.backtest.runner import (
    BacktestSetupError,
    build_matcher,
    format_metric,
    load_bars,
    load_funding_schedule,
    parse_grid,
    parse_range,
)
from quant.backtest.walkforward import WalkForward
from quant.cli._params import resolve_cli_params
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.strategies.loader import StrategyConfigError
from quant.strategies.params import ParamError

_RANGE_FMT = "%Y-%m-%d %H:%M"


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime(_RANGE_FMT)


def main(args) -> int:
    """执行 walk-forward，返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.walk_forward")

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
        strategy_cls, base_params, _sources = resolve_cli_params(settings, args)
        grid = parse_grid(args.grid)
        # 看板路径一直传 funding，CLI 此前漏传 → 同一条 walk-forward 在
        # 「是否计入资金费率」上口径不同（AGENTS #4 四条路径必须透传）。
        funding = load_funding_schedule(
            settings,
            args.inst,
            enabled=args.funding or None,
            start_ts=start_ts,
            end_ts=end_ts,
        )
    except BacktestSetupError as e:
        log.error(str(e))
        return 1
    except (StrategyConfigError, ParamError) as e:
        log.error(str(e))
        return 1

    def factory(params: dict):
        return strategy_cls()

    walker = WalkForward(
        factory,
        grid,
        initial_cash=settings.backtest.initial_cash,
        matcher=build_matcher(settings),
        window_size=settings.backtest.window_size,
        warmup_bars=args.warmup_bars,
        base_params=base_params,
        metric=args.metric,
        funding=funding,
    )

    step_bars = args.step_bars or args.oos_bars
    log.info(
        f"walk-forward started strategy={args.strategy} inst={args.inst} "
        f"bar={args.bar} range={args.start}..{args.end} "
        f"is={args.is_bars} oos={args.oos_bars} step={step_bars} "
        f"metric={args.metric}"
    )

    try:
        result = walker.run(
            warmup + bars,
            is_bars=args.is_bars,
            oos_bars=args.oos_bars,
            step_bars=step_bars,
            start_index=len(warmup),
        )
    except ValueError as e:
        log.error(str(e))
        return 1

    grid_names = list(grid)
    header = "  ".join(f"{name:>12}" for name in grid_names)
    print(
        f"\nWalk-forward  {len(result.segments)} 段"
        f"  (IS={args.is_bars} OOS={args.oos_bars} "
        f"step={step_bars} metric={args.metric})\n"
    )
    print(
        f"{'#':>2}  {'IS 区间':<32}{'OOS 区间':<32}"
        f"{header}  {'IS '+args.metric:>12}  {'OOS 收益':>10}  {'OOS 回撤':>10}"
    )
    print("-" * 130)
    for item in result.segments:
        cols = "  ".join(f"{str(item.params.get(n)):>12}" for n in grid_names)
        print(
            f"{item.segment.index:>2}  "
            f"{_fmt_ts(item.is_range[0])}~{_fmt_ts(item.is_range[1])[-5:]}  "
            f"{_fmt_ts(item.oos_range[0])}~{_fmt_ts(item.oos_range[1])[-5:]}  "
            f"{cols}  "
            f"{format_metric(args.metric, item.is_metrics[args.metric]):>12}  "
            f"{format_metric('total_return', item.oos_metrics['total_return']):>10}  "
            f"{format_metric('max_drawdown', item.oos_metrics['max_drawdown']):>10}"
        )

    total = len(result.segments)
    compounded = result.compounded_return
    compounded_text = (
        "—（OOS 段重叠，不可复合）"
        if compounded is None
        else f"{compounded:>10.2%}"
    )
    print("\n汇总")
    print(f"  OOS 复合收益        {compounded_text}")
    print(f"  盈利段数            {result.profitable_segments}/{total}")
    print(f"  OOS 平均夏普        {result.avg_oos_sharpe:>10.2f}")
    print(f"  OOS 累计交易数      {result.total_oos_trades:>10d}")
    modal = "  ".join(f"{k}={result.modal_params.get(k)}" for k in grid_names)
    print(f"  参数众数            {modal}")
    return 0
