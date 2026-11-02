"""批量回测（M4-7）：多品种 × 多周期 × 多参数，结果汇总成表。

与 `GridOptimizer` 的分工：

- 寻优固定「单策略单品种单周期」，并行发生在参数维度（进程池）；
- 批量固定「策略」，在品种 / 周期 / 参数三个维度展开，按格子串行执行。

同一次批量里 K 线按 (品种, 周期) 缓存：一组参数再多也只读一次 parquet。
单个格子失败（品种没数据、参数非法）不中断整批，原因随行返回；全部失败
才报错——否则用户会拿到一张空表却不知道是参数写错还是数据缺失。
"""
from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from quant.backtest.engine import Backtest
from quant.backtest.funding import FundingSchedule
from quant.backtest.matcher import Matcher
from quant.backtest.strategy import Strategy
from quant.models.bar import Bar

#: 一个待跑格子：(品种, 周期, 该格独有的参数)
CellSpec = tuple[str, str, dict[str, Any]]
#: 按 (品种, 周期) 返回「预热段 + 回测段」
BarsLoader = Callable[[str, str], tuple[list[Bar], list[Bar]]]
ProgressFn = Callable[[int, int], None]
MetricValue = float | int | str | None


@dataclass(frozen=True, slots=True)
class BatchCell:
    """一个格子的结果；失败时 `metrics` 为空、`error` 给原因。"""

    inst: str
    bar: str
    params: dict[str, Any]
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    """批量汇总：成功格子在 `cells`（服务层按指标排序），失败在 `failed`。"""

    metric: str = ""
    cells: tuple[BatchCell, ...] = ()
    failed: tuple[BatchCell, ...] = ()


def expand_cells(
    insts: Iterable[str],
    bars: Iterable[str],
    grid: dict[str, list[Any]] | None = None,
) -> list[CellSpec]:
    """展开「品种 × 周期 × 参数」，顺序固定（品种最外层、参数最内层）。

    空字符串与重复项会被去掉；网格为空时每个 (品种, 周期) 只有一格。

    Raises:
        ValueError: 品种或周期为空
    """
    inst_list = _unique(insts)
    bar_list = _unique(bars)
    if not inst_list:
        raise ValueError("insts is empty")
    if not bar_list:
        raise ValueError("bars is empty")

    names = list(grid or {})
    combos: list[dict[str, Any]] = (
        [
            dict(zip(names, combo, strict=True))
            for combo in itertools.product(*(grid[name] for name in names))
        ]
        if names
        else [{}]
    )
    return [
        (inst, bar, dict(combo))
        for inst in inst_list
        for bar in bar_list
        for combo in combos
    ]


def sort_cells(
    cells: Iterable[BatchCell], metric: str, *, ascending: bool = False
) -> list[BatchCell]:
    """按指标排序；算不出该指标（None）的格子始终排在末尾。"""
    missing = float("inf") if ascending else float("-inf")
    return sorted(
        cells,
        key=lambda cell: _rank(cell.metrics.get(metric), missing),
        reverse=not ascending,
    )


class BatchSweep:
    """逐格回测；K 线与资金费率表跨格子复用。"""

    def __init__(
        self,
        strategy_factory: Callable[[dict[str, Any]], Strategy],
        *,
        initial_cash: Decimal = Decimal("10000"),
        matcher: Matcher | None = None,
        window_size: int = 200,
        base_params: dict[str, Any] | None = None,
        funding: dict[str, FundingSchedule | None] | None = None,
    ) -> None:
        if not callable(strategy_factory):
            raise ValueError("strategy_factory must be callable")
        self.strategy_factory = strategy_factory
        self.initial_cash = initial_cash
        self.matcher = matcher
        self.window_size = window_size
        self.base_params = dict(base_params or {})
        #: 品种 -> 结算表；None 表示该品种不计资金费率
        self.funding = dict(funding or {})

    def run(
        self,
        cells: list[CellSpec],
        load_bars: BarsLoader,
        *,
        should_stop: Callable[[], bool] | None = None,
        progress: ProgressFn | None = None,
    ) -> tuple[list[BatchCell], list[BatchCell]]:
        """跑完所有格子，返回 (成功, 失败)。

        Args:
            cells: `expand_cells` 的结果
            load_bars: 按 (品种, 周期) 加载数据；异常按该格子失败处理
            should_stop: 每格开始前检查一次，返回 True 即提前结束
            progress: 进度回调 `(已完成, 总数)`

        Raises:
            ValueError: 所有格子都失败
        """
        ok: list[BatchCell] = []
        failed: list[BatchCell] = []
        # 缓存异常本身：同一品种后续格子不必重复踩一遍数据缺失
        loaded: dict[tuple[str, str], tuple[list[Bar], list[Bar]] | Exception] = {}
        total = len(cells)

        for done, (inst, bar, combo) in enumerate(cells, 1):
            if should_stop is not None and should_stop():
                break
            params = {**self.base_params, **combo}
            key = (inst, bar)
            if key not in loaded:
                try:
                    loaded[key] = load_bars(inst, bar)
                except Exception as e:  # noqa: BLE001 - 单格失败不中断整批
                    loaded[key] = e
            data = loaded[key]

            if isinstance(data, Exception):
                failed.append(BatchCell(inst, bar, params, error=str(data)))
            else:
                warmup, bars = data
                try:
                    result = Backtest(
                        self.strategy_factory(dict(params)),
                        self.matcher,
                        initial_cash=self.initial_cash,
                        window_size=self.window_size,
                        params=params,
                        warmup_bars=warmup,
                        funding=self.funding.get(inst),
                    ).run(bars)
                except ValueError as e:
                    # 例如 fast_len >= slow_len，属该格参数非法
                    failed.append(BatchCell(inst, bar, params, error=str(e)))
                else:
                    ok.append(BatchCell(inst, bar, params, result.metrics))
            if progress is not None:
                progress(done, total)

        if not ok:
            detail = f"; first error: {failed[0].error}" if failed else ""
            raise ValueError(f"all {len(cells)} batch cells failed{detail}")
        return ok, failed


def _unique(values: Iterable[str]) -> list[str]:
    """去空白、去重，保持首次出现的顺序。"""
    out: list[str] = []
    for value in values:
        item = value.strip()
        if item and item not in out:
            out.append(item)
    return out


def _rank(value: Any, missing: float) -> float:
    """非数值指标（如 monthly_returns 的 JSON 文本）同样按「算不出」处理。"""
    if not isinstance(value, (int, float)):
        return missing
    return float(value)


__all__ = [
    "BatchCell",
    "BatchResult",
    "BatchSweep",
    "expand_cells",
    "sort_cells",
]
