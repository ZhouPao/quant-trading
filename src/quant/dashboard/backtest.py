"""看板内的回测 / 参数寻优（只计算，不落盘）。

与命令行同口径：区间解析、预热加载、成交模拟、网格搜索全部复用
`quant.backtest` 下的现有实现，这里只负责：

- 把 HTTP 入参转成回测参数
- 在 `asyncio.to_thread` 里跑计算，避免阻塞事件循环
- 用信号量串行化，避免多个请求同时打满 CPU
- 把结果裁成适合页面渲染的 JSON（权益曲线降采样、成交条数上限）

不写 CSV/HTML、不写 SQLite、不改 Redis，保持 dashboard 只读。
"""
from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

from pydantic import BaseModel, Field

from quant.backtest.engine import Backtest
from quant.backtest.optimizer import GridOptimizer
from quant.backtest.runner import (
    BacktestSetupError,
    build_matcher,
    load_bars,
    parse_grid,
    parse_params,
    parse_range,
)
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger
from quant.models.bar import Bar
from quant.strategies.loader import StrategyConfigError, load_strategy_class

log = get_logger("dashboard.backtest")

MAX_COMBOS = 500          # 单次寻优的网格组合上限
MAX_EQUITY_POINTS = 2000  # 权益曲线降采样后最多点数
MAX_TRADES = 300          # 单次回测返回的成交明细上限

# 越小越好的指标，排序时反向
ASCENDING_METRICS = frozenset({"max_drawdown", "avg_holding_bars"})

METRIC_KEYS = (
    "total_return",
    "annual_return",
    "sharpe",
    "max_drawdown",
    "win_rate",
    "profit_factor",
    "trade_count",
    "avg_holding_bars",
)


class BacktestRequestError(Exception):
    """入参或数据不满足回测条件，消息可直接展示给页面。"""


class BacktestRequest(BaseModel):
    """单次回测入参。"""

    strategy: str
    start: str
    end: str
    inst: str | None = None
    bar: str = "1H"
    params: str = ""
    warmup_bars: int = Field(200, ge=0, le=5000)


class OptimizeRequest(BacktestRequest):
    """参数寻优入参，比回测多一个网格与排序指标。"""

    grid: str
    metric: str = "sharpe"
    top: int = Field(10, ge=1, le=50)


class BacktestService:
    """看板回测服务：策略清单 + 单次回测 + 网格寻优。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._config_dir = Path(settings.engine.config_dir)
        self._lock = asyncio.Semaphore(1)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def strategies(self) -> list[dict[str, object]]:
        """扫描策略配置目录，返回 [{id, params}]。非法配置跳过并记日志。"""
        return await asyncio.to_thread(self._scan_strategies)

    async def run(self, req: BacktestRequest) -> dict[str, object]:
        async with self._lock:
            started = time.monotonic()
            payload = await asyncio.to_thread(self._run_sync, req)
        payload["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return payload

    async def optimize(self, req: OptimizeRequest) -> dict[str, object]:
        async with self._lock:
            started = time.monotonic()
            payload = await asyncio.to_thread(self._optimize_sync, req)
        payload["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return payload

    # ------------------------------------------------------------------
    # 同步实现（在线程里跑）
    # ------------------------------------------------------------------

    def _scan_strategies(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for path in sorted(self._config_dir.glob("*.toml")):
            try:
                _cls, params = load_strategy_class(path.stem, self._config_dir)
            except StrategyConfigError as e:
                log.warning(f"skip strategy config {path}: {e}")
                continue
            out.append({"id": path.stem, "params": params})
        return out

    def _run_sync(self, req: BacktestRequest) -> dict[str, object]:
        inst = req.inst or self._settings.engine.inst
        warmup, bars, strategy_cls, params = self._prepare(
            req, inst
        )
        try:
            result = Backtest(
                strategy_cls(),
                build_matcher(self._settings),
                initial_cash=self._settings.backtest.initial_cash,
                window_size=self._settings.backtest.window_size,
                params=params,
                warmup_bars=warmup,
            ).run(bars)
        except ValueError as e:
            raise BacktestRequestError(str(e)) from e

        trades = result.trades[-MAX_TRADES:]
        return {
            "kind": "backtest",
            "strategy": req.strategy,
            "inst": inst,
            "bar": req.bar,
            "start": req.start,
            "end": req.end,
            "params": params,
            "bar_count": len(bars),
            "warmup_count": len(warmup),
            "trade_count": len(result.trades),
            "trades_truncated": len(result.trades) > len(trades),
            "unfilled_signals": len(result.unfilled_signals),
            "final_equity": str(result.equity_curve[-1][1]),
            "metrics": _clean_metrics(result.metrics),
            "equity": _equity_points(result.equity_curve),
            "trades": [
                {
                    "ts": fill.ts,
                    "side": fill.side,
                    "qty": str(fill.qty),
                    "price": str(fill.price),
                    "fee": str(fill.fee),
                }
                for fill in trades
            ],
        }

    def _optimize_sync(self, req: OptimizeRequest) -> dict[str, object]:
        if req.metric not in METRIC_KEYS:
            raise BacktestRequestError(
                f"unknown metric {req.metric!r}; available: {list(METRIC_KEYS)}"
            )
        inst = req.inst or self._settings.engine.inst
        try:
            grid = parse_grid(req.grid)
        except BacktestSetupError as e:
            raise BacktestRequestError(str(e)) from e

        combos = math.prod(len(values) for values in grid.values())
        if combos > MAX_COMBOS:
            raise BacktestRequestError(
                f"参数组合过多：{combos} 组 > 上限 {MAX_COMBOS}，请缩小网格"
            )

        warmup, bars, strategy_cls, params = self._prepare(req, inst)
        optimizer = GridOptimizer(
            lambda _params: strategy_cls(),
            grid,
            initial_cash=self._settings.backtest.initial_cash,
            matcher=build_matcher(self._settings),
            window_size=self._settings.backtest.window_size,
            warmup_bars=warmup,
            base_params=params,
        )
        try:
            results = optimizer.search(bars, metric=req.metric)
        except ValueError as e:
            raise BacktestRequestError(str(e)) from e

        results.sort(
            key=lambda item: item[1][req.metric],
            reverse=req.metric not in ASCENDING_METRICS,
        )
        top = results[: req.top]
        return {
            "kind": "optimize",
            "strategy": req.strategy,
            "inst": inst,
            "bar": req.bar,
            "start": req.start,
            "end": req.end,
            "grid": grid,
            "metric": req.metric,
            "bar_count": len(bars),
            "warmup_count": len(warmup),
            "combo_count": combos,
            "skipped_count": combos - len(results),
            "results": [
                {
                    "rank": index + 1,
                    "params": {
                        name: str(params_[name]) for name in grid
                    },
                    "metrics": _clean_metrics(metrics),
                }
                for index, (params_, metrics) in enumerate(top)
            ],
        }

    def _prepare(
        self, req: BacktestRequest, inst: str
    ) -> tuple[list[Bar], list[Bar], type, dict]:
        """解析区间、加载预热与回测段、读取策略类与参数。"""
        try:
            # 先校验策略配置（廉价），避免数据不足的错误盖住配置写错
            strategy_cls, config_params = load_strategy_class(
                req.strategy, self._config_dir
            )
            params = {**config_params, **parse_params(req.params)}
            start_ts, end_ts = parse_range(req.start, req.end)
            warmup, bars = load_bars(
                self._settings,
                inst,
                req.bar,
                start_ts,
                end_ts,
                req.warmup_bars,
            )
        except (BacktestSetupError, StrategyConfigError) as e:
            raise BacktestRequestError(str(e)) from e
        return warmup, bars, strategy_cls, params


def _clean_metrics(metrics: dict[str, float]) -> dict[str, float | None]:
    """inf/nan 不是合法 JSON，统一转 null 让页面显示为「—」。"""
    return {
        key: (value if math.isfinite(value) else None)
        for key, value in metrics.items()
    }


def _equity_points(
    curve: list[tuple[int, object]], limit: int = MAX_EQUITY_POINTS
) -> list[dict[str, object]]:
    """权益曲线降采样：超出上限时等步取点，并保证保留最后一根。"""
    if not curve:
        return []
    if len(curve) <= limit:
        selected = curve
    else:
        step = math.ceil(len(curve) / limit)
        selected = curve[::step]
        if selected[-1][0] != curve[-1][0]:
            selected = [*selected, curve[-1]]
    return [{"ts": ts, "equity": str(equity)} for ts, equity in selected]


__all__ = [
    "ASCENDING_METRICS",
    "BacktestRequest",
    "BacktestRequestError",
    "BacktestService",
    "METRIC_KEYS",
    "OptimizeRequest",
]
