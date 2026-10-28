"""网格参数寻优（spec §4.7）。

对参数网格做穷举回测，按指定指标降序返回结果。
约束：
- 参数值必须是标量常量（int/float/str/bool/Decimal），禁止 callable/对象
- 禁止未来函数由引擎保证：策略只能访问 ctx.bars_window（当前及历史 bar）
- M2 串行执行；n_jobs 保留接口，>1 时仍串行
"""
from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.strategy import Strategy
from quant.models.bar import Bar

_SCALAR_TYPES = (int, float, str, bool, Decimal)


class GridOptimizer:
    """网格搜索策略参数。

    Args:
        strategy_factory: 接受 params dict 返回 Strategy 实例
        param_grid: {"fast_len": [5, 10, 20], "slow_len": [20, 50]}
        initial_cash / matcher / window_size: 透传给 Backtest
        warmup_bars: 预热段，透传给 Backtest（保证与单次回测口径一致）
        base_params: 固定参数（如策略配置里的 commission_rate），
            与每组网格参数合并后再传给策略
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

        for name, values in self.param_grid.items():
            for value in values:
                self._check_future_leak({name: value})
        self._check_future_leak(self.base_params)

    def search(
        self,
        bars: list[Bar],
        metric: str = "sharpe",
        n_jobs: int = 1,
    ) -> list[tuple[dict[str, Any], dict[str, float]]]:
        """穷举所有参数组合并回测，按 metric 降序返回。

        Args:
            bars: 历史 K线（按 ts 升序、同品种）
            metric: 排序指标键（如 sharpe/total_return/max_drawdown）
            n_jobs: 并行度；M2 仅支持串行（传 >1 也串行执行）

        Returns:
            [(params, metrics), ...]，按 metric 降序

        Raises:
            ValueError: n_jobs < 1 / metric 不存在 / 参数非标量
        """
        if n_jobs < 1:
            raise ValueError(f"n_jobs must be >= 1, got {n_jobs}")

        names = list(self.param_grid)
        value_lists = [self.param_grid[name] for name in names]
        combinations = (
            list(itertools.product(*value_lists)) if names else [()]
        )

        results: list[tuple[dict[str, Any], dict[str, float]]] = []
        skipped: list[str] = []
        for combo in combinations:
            params = {**self.base_params, **dict(zip(names, combo, strict=True))}
            self._check_future_leak(params)

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
                continue
            results.append((params, result.metrics))

        if not results:
            detail = f"; first error: {skipped[0]}" if skipped else ""
            raise ValueError(
                f"all {len(combinations)} parameter combinations failed{detail}"
            )

        if metric not in results[0][1]:
            raise ValueError(
                f"unknown metric {metric!r}; available: {sorted(results[0][1])}"
            )

        results.sort(key=lambda item: item[1][metric], reverse=True)
        return results

    @staticmethod
    def _check_future_leak(params: dict[str, Any]) -> None:
        """参数只允许标量常量，防止传入函数/对象引用未来数据。"""
        for name, value in params.items():
            if not isinstance(value, _SCALAR_TYPES):
                raise ValueError(
                    f"param {name!r} must be a scalar constant, "
                    f"got {type(value).__name__}"
                )


__all__ = ["GridOptimizer"]
