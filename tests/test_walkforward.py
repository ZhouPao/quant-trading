"""Walk-forward 滚动验证单元测试。

覆盖：
- 切分：段数、IS/OOS 首尾相接、step < oos 时允许重叠、放不下时报错
- 选参：IS 上的最优参数被原样用于 OOS，且 OOS 结果不回流
- 聚合：复合收益、盈利段数、参数众数
- 预热：只取段起点之前的 bar，不足时报错
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.matcher import Matcher
from quant.backtest.signal import Signal
from quant.backtest.strategy import Strategy
from quant.backtest.walkforward import (
    Segment,
    SegmentResult,
    WalkForward,
    WalkForwardResult,
    build_segments,
)
from quant.models.bar import Bar

_TS0 = 1_700_000_000_000
_HOUR = 3_600_000


def _rising_bars(count: int) -> list[Bar]:
    """单边上涨：100, 101, 102 ...，用于让仓位越大收益越高。"""
    bars = []
    for i in range(count):
        price = Decimal(100 + i)
        bars.append(
            Bar(
                ts=_TS0 + i * _HOUR,
                open=price,
                high=price,
                low=price,
                close=price,
                vol=Decimal("1"),
                vol_quote=Decimal("1"),
                confirm=1,
                inst_id="BTC-USDT",
                bar_size="4H",
            )
        )
    return bars


class _BuyAndHold(Strategy):
    """窗口首根全仓买入并持有，收益随 qty 线性放大。"""

    def __init__(self) -> None:
        self.qty = Decimal("1")

    @property
    def strategy_id(self) -> str:
        return "buy_hold"

    def on_init(self, ctx) -> None:
        self.qty = Decimal(str(ctx.params.get("qty", "1")))
        if self.qty <= 0:
            raise ValueError("qty must be positive")

    def on_bar(self, bar: Bar, ctx) -> Signal | None:
        if len(ctx.bars_window) == 1 and ctx.position.is_flat:
            return Signal(
                strategy_id=self.strategy_id,
                side="buy",
                order_type="market",
                inst_id=bar.inst_id,
                qty=self.qty,
                ts=bar.ts,
            )
        return None


def _factory(params: dict) -> _BuyAndHold:
    return _BuyAndHold()


def _walker(grid: dict, **kwargs) -> WalkForward:
    return WalkForward(
        _factory,
        grid,
        initial_cash=Decimal("1000000"),
        matcher=Matcher(commission_rate=Decimal("0")),
        **kwargs,
    )


class TestBuildSegments:
    def test_contiguous_segments(self) -> None:
        """step = is + oos 时各段首尾相接，不重叠。"""
        segments = build_segments(30, is_bars=10, oos_bars=5, step_bars=15)

        assert [s.index for s in segments] == [0, 1]
        assert segments[0].is_start == 0
        assert segments[0].is_end == 10
        assert segments[0].oos_start == 10
        assert segments[0].oos_end == 15
        assert segments[1].is_start == 15

    def test_step_equals_oos_keeps_oos_adjacent(self) -> None:
        """默认 step = oos：OOS 段首尾相接，IS 窗口相互重叠（滚动式）。"""
        segments = build_segments(30, is_bars=10, oos_bars=5, step_bars=5)

        assert len(segments) == 4
        for prev, cur in zip(segments, segments[1:], strict=False):
            assert prev.oos_end == cur.oos_start

    def test_step_smaller_than_oos_overlaps(self) -> None:
        segments = build_segments(30, is_bars=10, oos_bars=5, step_bars=2)

        assert len(segments) == 8
        assert segments[1].is_start == 2
        assert segments[1].oos_start < segments[0].oos_end

    def test_start_index_skips_warmup(self) -> None:
        """前置预热不参与切分，首段 IS 从 start_index 起。"""
        segments = build_segments(
            40, is_bars=10, oos_bars=5, step_bars=5, start_index=5
        )

        assert segments[0].is_start == 5
        assert segments[0].oos_end == 20
        assert segments[-1].oos_end <= 40

    def test_not_enough_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="not enough bars"):
            build_segments(10, is_bars=10, oos_bars=5, step_bars=5)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"is_bars": 0, "oos_bars": 5, "step_bars": 5},
            {"is_bars": 10, "oos_bars": -1, "step_bars": 5},
            {"is_bars": 10, "oos_bars": 5, "step_bars": 0},
        ],
    )
    def test_non_positive_raises(self, kwargs) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            build_segments(100, **kwargs)

    def test_start_index_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            build_segments(10, is_bars=4, oos_bars=2, step_bars=2, start_index=10)


class TestWalkForwardRun:
    def test_picks_best_is_params_and_scores_oos(self) -> None:
        """qty 越大收益越高，各段都应选 qty=2，且 OOS 收益为正。"""
        result = _walker({"qty": ["1", "2"]}, metric="total_return").run(
            _rising_bars(60), is_bars=20, oos_bars=10
        )

        assert len(result.segments) == 4
        for item in result.segments:
            assert item.params["qty"] == "2"
            assert item.is_metrics["total_return"] > 0
            assert item.oos_metrics["total_return"] > 0

    def test_oos_range_follows_is_range(self) -> None:
        result = _walker({"qty": ["1"]}).run(
            _rising_bars(60), is_bars=20, oos_bars=10
        )

        for item in result.segments:
            assert item.is_range[1] < item.oos_range[0]
            assert item.oos_range[0] < item.oos_range[1]

    def test_start_index_excludes_warmup_from_segments(self) -> None:
        """start_index 之前的 bar 只作预热，不参与 IS/OOS 成交。"""
        bars = _rising_bars(60)
        result = _walker({"qty": ["1"]}).run(
            bars, is_bars=20, oos_bars=10, start_index=10
        )

        assert result.segments[0].is_range[0] == bars[10].ts
        assert result.segments[-1].oos_range[1] == bars[-1].ts

    def test_insufficient_warmup_raises(self) -> None:
        walker = _walker({"qty": ["1"]}, warmup_bars=5)
        with pytest.raises(ValueError, match="not enough warmup"):
            walker.run(_rising_bars(60), is_bars=20, oos_bars=10, start_index=3)

    def test_all_invalid_params_raises(self) -> None:
        with pytest.raises(ValueError, match="all .* failed"):
            _walker({"qty": ["0"]}).run(_rising_bars(60), is_bars=20, oos_bars=10)

    def test_compounded_return_matches_product(self) -> None:
        result = _walker({"qty": ["1"]}).run(
            _rising_bars(60), is_bars=20, oos_bars=10
        )

        expected = 1.0
        for item in result.segments:
            expected *= 1 + item.oos_metrics["total_return"]
        assert result.compounded_return == pytest.approx(expected - 1)
        assert result.compounded_return > 0
        assert result.profitable_segments == len(result.segments)


class TestAggregates:
    def _item(self, params: dict, oos_return: float) -> SegmentResult:
        seg = Segment(0, 0, 1, 1, 2)
        return SegmentResult(
            segment=seg,
            params=params,
            is_metrics={"total_return": 0.0},
            oos_metrics={"total_return": oos_return, "sharpe": oos_return * 10},
            is_range=(_TS0, _TS0),
            oos_range=(_TS0, _TS0),
        )

    def test_modal_params_takes_majority(self) -> None:
        result = WalkForwardResult(
            [
                self._item({"qty": "1"}, 0.1),
                self._item({"qty": "2"}, -0.1),
                self._item({"qty": "2"}, 0.2),
            ]
        )

        assert result.modal_params == {"qty": "2"}
        assert result.profitable_segments == 2
        assert result.avg_oos_sharpe == pytest.approx((1.0 - 1.0 + 2.0) / 3)

    def test_empty_result_is_safe(self) -> None:
        result = WalkForwardResult([])

        assert result.compounded_return == 0.0
        assert result.avg_oos_sharpe == 0.0
        assert result.modal_params == {}


class TestCliWiring:
    def test_parser_accepts_walk_forward(self) -> None:
        from quant.cli import build_parser

        args = build_parser().parse_args(
            [
                "walk-forward",
                "--strategy", "sma_cross",
                "--inst", "BTC-USDT-SWAP",
                "--bar", "4H",
                "--start", "2024-03-01",
                "--end", "2026-10-29",
                "--grid", "slow_len=50,100",
            ]
        )

        assert args.command == "walk-forward"
        assert args.is_bars == 1500
        assert args.oos_bars == 500
        assert args.step_bars is None
        assert args.metric == "sharpe"
