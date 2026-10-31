"""网格参数寻优（spec §4.7 / MS4-C §6）。

对参数网格做穷举回测，按指定指标降序返回结果。
约束：
- 参数值必须是标量常量（int/float/str/bool/Decimal），禁止 callable/对象
- 禁止未来函数由引擎保证：策略只能访问 ctx.bars_window（当前及历史 bar）
- `n_jobs > 1` 用进程池真并行：worker 内按类路径重建策略实例，
  不 pickle 实例本身（策略将来可能持有锁/连接）
"""
from __future__ import annotations

import importlib
import itertools
import multiprocessing
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal
from typing import Any

from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.strategy import Strategy
from quant.models.bar import Bar

_SCALAR_TYPES = (int, float, str, bool, Decimal)

#: worker 进程的共享数据，由 initializer 一次性注入（避免每个组合重传 K 线）
_WORKER: dict[str, Any] = {}


class GridOptimizer:
    """网格搜索策略参数。

    Args:
        strategy_factory: 接受 params dict 返回 Strategy 实例（串行路径用）
        param_grid: {"fast_len": [5, 10, 20], "slow_len": [20, 50]}
        initial_cash / matcher / window_size: 透传给 Backtest
        warmup_bars: 预热段，透传给 Backtest（保证与单次回测口径一致）
        base_params: 固定参数（如策略配置里的 commission_rate），
            与每组网格参数合并后再传给策略
        strategy_cls: 策略类，`n_jobs > 1` 时必需——并行要按类路径在子进程
            重建实例，闭包工厂过不了进程边界
    """

    def __init__(
        self,
        strategy_factory: Callable[[dict[str, Any]], Strategy],
        param_grid: dict[str, Iterable[Any]],
        initial_cash: Decimal = Decimal("10000"),
        matcher: Matcher | None = None,
        window_size: int = 200,
        warmup_bars: list[Bar] | None = None,
        base_params: dict[str, Any] | None = None,
        strategy_cls: type[Strategy] | None = None,
    ) -> None:
        if not callable(strategy_factory):
            raise ValueError("strategy_factory must be callable")
        self.strategy_factory = strategy_factory
        self.param_grid = {k: list(v) for k, v in param_grid.items()}
        self.base_params = dict(base_params or {})
        self.initial_cash = initial_cash
        self.matcher = matcher
        self.window_size = window_size
        self.warmup_bars = list(warmup_bars) if warmup_bars else []
        self.strategy_cls = strategy_cls

        for name, values in self.param_grid.items():
            for value in values:
                self._check_future_leak({name: value})
        self._check_future_leak(self.base_params)

    def search(
        self,
        bars: list[Bar],
        metric: str = "sharpe",
        n_jobs: int = 1,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[tuple[dict[str, Any], dict[str, float]]]:
        """穷举所有参数组合并回测，按 metric 降序返回。

        Args:
            bars: 历史 K线（按 ts 升序、同品种）
            metric: 排序指标键（如 sharpe/total_return/max_drawdown）
            n_jobs: 并行度；>1 走进程池，需要 `strategy_cls`
            progress: 进度回调 `(已完成, 总数)`，每完成一组调用一次

        Returns:
            [(params, metrics), ...]，按 metric 降序

        Raises:
            ValueError: n_jobs < 1 / 并行缺少 strategy_cls / metric 不存在 /
                参数非标量 / 全部组合失败
        """
        if n_jobs < 1:
            raise ValueError(f"n_jobs must be >= 1, got {n_jobs}")

        names = list(self.param_grid)
        value_lists = [self.param_grid[name] for name in names]
        combinations = (
            list(itertools.product(*value_lists)) if names else [()]
        )
        # 组合级校验放在派发前：非法参数早失败，不必等进程池起停
        for combo in combinations:
            self._check_future_leak(
                {**self.base_params, **dict(zip(names, combo, strict=True))}
            )

        if n_jobs > 1 and len(combinations) > 1:
            results, skipped = self._run_parallel(
                bars, names, combinations, n_jobs, progress
            )
        else:
            results, skipped = self._run_serial(
                bars, names, combinations, progress
            )

        if not results:
            detail = f"; first error: {skipped[0]}" if skipped else ""
            raise ValueError(
                f"all {len(combinations)} parameter combinations failed{detail}"
            )

        if metric not in results[0][1]:
            raise ValueError(
                f"unknown metric {metric!r}; available: {sorted(results[0][1])}"
            )
        if not isinstance(results[0][1][metric], (int, float)):
            # monthly_returns / modal_params 这类非数值列不可用于排序
            raise ValueError(f"metric {metric!r} is not sortable (non-numeric)")

        # 扩展指标可能为 None（无法计算）：排序时视为最差，而不是抛 TypeError
        results.sort(key=lambda item: _rank(item[1][metric]), reverse=True)
        return results

    def _run_serial(
        self,
        bars: list[Bar],
        names: list[str],
        combinations: list[tuple],
        progress: Callable[[int, int], None] | None,
    ) -> tuple[list[tuple[dict[str, Any], dict[str, float]]], list[str]]:
        results: list[tuple[dict[str, Any], dict[str, float]]] = []
        skipped: list[str] = []
        total = len(combinations)
        for done, combo in enumerate(combinations, 1):
            params = {**self.base_params, **dict(zip(names, combo, strict=True))}
            try:
                strategy = self.strategy_factory(dict(params))
                backtest = Backtest(
                    strategy,
                    self.matcher,
                    initial_cash=self.initial_cash,
                    window_size=self.window_size,
                    params=params,
                    warmup_bars=self.warmup_bars,
                )
                result = backtest.run(bars)
            except ValueError as e:
                # 例如 fast_len >= slow_len，属参数组合本身非法，跳过继续
                skipped.append(f"{params}: {e}")
            else:
                results.append((params, result.metrics))
            if progress is not None:
                progress(done, total)
        return results, skipped

    def _run_parallel(
        self,
        bars: list[Bar],
        names: list[str],
        combinations: list[tuple],
        n_jobs: int,
        progress: Callable[[int, int], None] | None,
    ) -> tuple[list[tuple[dict[str, Any], dict[str, float]]], list[str]]:
        if self.strategy_cls is None:
            raise ValueError(
                "n_jobs > 1 requires strategy_cls: "
                "callable factories cannot be rebuilt in worker processes"
            )
        path = f"{self.strategy_cls.__module__}.{self.strategy_cls.__qualname__}"
        total = len(combinations)
        indexed: list[tuple[int, dict[str, Any], dict[str, float]]] = []
        skipped: list[str] = []

        with ProcessPoolExecutor(
            max_workers=min(n_jobs, total),
            mp_context=_mp_context(),
            initializer=_init_worker,
            initargs=(
                path,
                self.matcher,
                self.initial_cash,
                self.window_size,
                self.warmup_bars,
                bars,
                self.base_params,
            ),
        ) as pool:
            futures = {
                pool.submit(_run_combo, names, combo): index
                for index, combo in enumerate(combinations)
            }
            for done, future in enumerate(as_completed(futures), 1):
                status, payload, detail = future.result()
                if status == "ok":
                    indexed.append((futures[future], payload, detail))
                else:
                    skipped.append(f"{payload}: {detail}")
                if progress is not None:
                    progress(done, total)

        # 恢复网格声明顺序，保证同分组合的排序与串行一致
        indexed.sort(key=lambda item: item[0])
        return [(params, metrics) for _index, params, metrics in indexed], skipped

    @staticmethod
    def _check_future_leak(params: dict[str, Any]) -> None:
        """参数只允许标量常量，防止传入函数/对象引用未来数据。"""
        for name, value in params.items():
            if not isinstance(value, _SCALAR_TYPES):
                raise ValueError(
                    f"param {name!r} must be a scalar constant, "
                    f"got {type(value).__name__}"
                )


def _rank(value: float | int | None) -> float:
    """排序键：None（指标无法计算）视为最差，保证降序时排在末尾。"""
    return float("-inf") if value is None else float(value)


def _mp_context() -> multiprocessing.context.BaseContext:
    """优先 forkserver：worker 不必重新导入主模块，启动也比 spawn 快。"""
    methods = multiprocessing.get_all_start_methods()
    return multiprocessing.get_context(
        "forkserver" if "forkserver" in methods else "spawn"
    )


def _init_worker(
    strategy_path: str,
    matcher: Matcher | None,
    initial_cash: Decimal,
    window_size: int,
    warmup_bars: list[Bar],
    bars: list[Bar],
    base_params: dict[str, Any],
) -> None:
    """把每进程都一样的重数据（K 线、成交模拟器）注入一次。"""
    _WORKER.clear()
    _WORKER.update(
        strategy_cls=_load_strategy(strategy_path),
        matcher=matcher,
        initial_cash=initial_cash,
        window_size=window_size,
        warmup_bars=warmup_bars,
        bars=bars,
        base_params=base_params,
    )


def _load_strategy(path: str) -> type[Strategy]:
    module_name, _, class_name = path.rpartition(".")
    if not module_name:
        raise ValueError(f"invalid strategy path {path!r}")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def _run_combo(
    names: list[str], combo: tuple
) -> tuple[str, Any, Any]:
    """执行一组参数；返回 ("ok", params, metrics) 或 ("skip", params, 原因)。"""
    params = {**_WORKER["base_params"], **dict(zip(names, combo, strict=True))}
    try:
        strategy = _WORKER["strategy_cls"]()
        result = Backtest(
            strategy,
            _WORKER["matcher"],
            initial_cash=_WORKER["initial_cash"],
            window_size=_WORKER["window_size"],
            params=params,
            warmup_bars=_WORKER["warmup_bars"],
        ).run(_WORKER["bars"])
    except ValueError as e:
        return "skip", params, str(e)
    return "ok", params, result.metrics


__all__ = ["GridOptimizer"]
