"""Walk-forward 验证（滚动 IS/OOS）。

把历史按「样本内(IS) + 样本外(OOS)」滚动切片：每段在 IS 上按 metric
选最优参数，再用该参数在紧随其后的 OOS 上回测。OOS 结果只用于评估，
绝不回流影响选参，因此能反映参数在未知数据上的真实表现。

约束与单次回测/寻优一致：
- 参数只允许标量常量（由 GridOptimizer 校验）
- 每段预热只取该段起点之前的历史 bar，不触碰段内数据
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.optimizer import GridOptimizer
from quant.backtest.strategy import Strategy
from quant.models.bar import Bar


@dataclass(frozen=True, slots=True)
class Segment:
    """一段 IS + 紧随其后的 OOS，下标均为 bars 数组下标（左闭右开）。"""

    index: int
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int


@dataclass(slots=True)
class SegmentResult:
    segment: Segment
    params: dict[str, Any]
    is_metrics: dict[str, float]
    oos_metrics: dict[str, float]
    is_range: tuple[int, int]
    oos_range: tuple[int, int]


@dataclass(slots=True)
class WalkForwardResult:
    """各段结果与汇总。汇总属性按需计算，避免无成交时除零。"""

    segments: list[SegmentResult] = field(default_factory=list)

    @property
    def compounded_return(self) -> float:
        """各段 OOS 收益的复利复合：Π(1+r) - 1。"""
        total = 1.0
        for item in self.segments:
            total *= 1 + item.oos_metrics.get("total_return", 0.0)
        return total - 1

    @property
    def profitable_segments(self) -> int:
        return sum(
            1 for item in self.segments if item.oos_metrics.get("total_return", 0.0) > 0
        )

    @property
    def avg_oos_sharpe(self) -> float:
        if not self.segments:
            return 0.0
        return sum(s.oos_metrics.get("sharpe", 0.0) for s in self.segments) / len(
            self.segments
        )

    @property
    def total_oos_trades(self) -> int:
        return sum(int(s.oos_metrics.get("trade_count", 0)) for s in self.segments)

    @property
    def modal_params(self) -> dict[str, Any]:
        """各段选中参数的众数；并列时取最早出现的。"""
        if not self.segments:
            return {}
        keys = sorted(self.segments[0].params)

        def signature(item: SegmentResult) -> tuple[str, ...]:
            return tuple(repr(item.params.get(k)) for k in keys)

        best, _ = Counter(signature(s) for s in self.segments).most_common(1)[0]
        for item in self.segments:
            if signature(item) == best:
                return {k: item.params.get(k) for k in keys}
        return {}  # pragma: no cover - 逻辑上前面的循环必然命中


def build_segments(
    total: int,
    *,
    is_bars: int,
    oos_bars: int,
    step_bars: int,
    start_index: int = 0,
) -> list[Segment]:
    """按 bar 根数滚动切分。

    Args:
        total: bars 总根数
        is_bars / oos_bars: 单段的样本内、样本外长度
        step_bars: 相邻段 IS 起点的间隔（< oos_bars 时 OOS 会重叠）
        start_index: 首段 IS 起点（通常等于前置预热根数）

    Raises:
        ValueError: 参数非正、起点越界或放不下一段
    """
    if is_bars <= 0 or oos_bars <= 0 or step_bars <= 0:
        raise ValueError("is_bars, oos_bars and step_bars must be positive")
    if not 0 <= start_index < total:
        raise ValueError(f"start_index {start_index} out of range (total={total})")

    segments: list[Segment] = []
    start = start_index
    while start + is_bars + oos_bars <= total:
        is_end = start + is_bars
        segments.append(
            Segment(
                index=len(segments),
                is_start=start,
                is_end=is_end,
                oos_start=is_end,
                oos_end=is_end + oos_bars,
            )
        )
        start += step_bars

    if not segments:
        raise ValueError(
            f"not enough bars for one segment: need {is_bars + oos_bars} "
            f"from index {start_index}, got {total - start_index}"
        )
    return segments


class WalkForward:
    """滚动 IS/OOS 验证器。

    Args:
        strategy_factory: 接受 params dict 返回 Strategy 实例
        param_grid: IS 段的搜索网格，如 {"slow_len": [50, 100]}
        initial_cash / matcher / window_size: 透传给 Backtest
        warmup_bars: 每段预热根数（只取段起点之前的 bar）
        base_params: 固定参数，与网格参数合并后传给策略
        metric: IS 段选参依据（最大化该指标）
    """

    def __init__(
        self,
        strategy_factory: Callable[[dict[str, Any]], Strategy],
        param_grid: dict[str, Iterable[Any]],
        initial_cash: Decimal = Decimal("10000"),
        matcher: Matcher | None = None,
        window_size: int = 200,
        warmup_bars: int = 0,
        base_params: dict[str, Any] | None = None,
        metric: str = "sharpe",
    ) -> None:
        if not callable(strategy_factory):
            raise ValueError("strategy_factory must be callable")
        if warmup_bars < 0:
            raise ValueError(f"warmup_bars must be >= 0, got {warmup_bars}")
        self.strategy_factory = strategy_factory
        self.param_grid = {k: list(v) for k, v in param_grid.items()}
        self.initial_cash = initial_cash
        self.matcher = matcher
        self.window_size = window_size
        self.warmup_bars = warmup_bars
        self.base_params = dict(base_params or {})
        self.metric = metric

    def run(
        self,
        bars: list[Bar],
        *,
        is_bars: int,
        oos_bars: int,
        step_bars: int | None = None,
        start_index: int = 0,
        should_stop: Callable[[], bool] | None = None,
    ) -> WalkForwardResult:
        """执行全部滚动段，返回逐段结果与汇总。

        Args:
            bars: 完整历史（含首段之前的前置预热）
            is_bars / oos_bars: 见 build_segments
            step_bars: 默认等于 oos_bars（OOS 首尾相接、不重叠）
            start_index: 首段 IS 起点，通常为前置预热根数
            should_stop: 取消检查，粒度是「一段」；已完成的段保留

        Raises:
            ValueError: 切分非法、预热不足或所有参数组合在某段均失败
        """
        if not bars:
            raise ValueError("bars must be non-empty")
        segments = build_segments(
            len(bars),
            is_bars=is_bars,
            oos_bars=oos_bars,
            step_bars=step_bars if step_bars is not None else oos_bars,
            start_index=start_index,
        )

        results: list[SegmentResult] = []
        for seg in segments:
            if should_stop is not None and should_stop():
                break
            is_warmup = self._warmup_before(bars, seg.is_start)
            optimizer = GridOptimizer(
                self.strategy_factory,
                self.param_grid,
                initial_cash=self.initial_cash,
                matcher=self.matcher,
                window_size=self.window_size,
                warmup_bars=is_warmup,
                base_params=self.base_params,
            )
            ranked = optimizer.search(bars[seg.is_start : seg.is_end], self.metric)
            params, is_metrics = ranked[0]

            oos_warmup = self._warmup_before(bars, seg.oos_start)
            oos_bars_slice = bars[seg.oos_start : seg.oos_end]
            oos_result = Backtest(
                self.strategy_factory(dict(params)),
                self.matcher,
                initial_cash=self.initial_cash,
                window_size=self.window_size,
                params=params,
                warmup_bars=oos_warmup,
            ).run(oos_bars_slice)

            results.append(
                SegmentResult(
                    segment=seg,
                    params=params,
                    is_metrics=is_metrics,
                    oos_metrics=oos_result.metrics,
                    is_range=(
                        bars[seg.is_start].ts,
                        bars[seg.is_end - 1].ts,
                    ),
                    oos_range=(
                        oos_bars_slice[0].ts,
                        oos_bars_slice[-1].ts,
                    ),
                )
            )
        return WalkForwardResult(results)

    def _warmup_before(self, bars: list[Bar], index: int) -> list[Bar]:
        """段起点之前的预热 bar；不足时报错而不是静默缩短。"""
        warmup = bars[max(0, index - self.warmup_bars) : index]
        if self.warmup_bars and len(warmup) < self.warmup_bars:
            raise ValueError(
                f"not enough warmup bars before index {index}: "
                f"need {self.warmup_bars}, got {len(warmup)}"
            )
        return warmup


__all__ = ["Segment", "SegmentResult", "WalkForward", "WalkForwardResult", "build_segments"]
