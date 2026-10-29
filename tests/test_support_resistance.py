"""支撑/压力位识别测试。

与前端 `web/src/chart/indicators.ts` 的 pivotLevels 用例对齐：
同一组输入必须产出同一组价位与触碰次数，避免前后端算法漂移。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.indicators.support_resistance import Level, nearest_levels, pivot_levels
from quant.models.bar import Bar


def make_bar(close: int, index: int, spread: int = 1) -> Bar:
    """构造 high=close+spread、low=close-spread 的 K 线，与前端用例一致。"""
    return Bar(
        ts=1_700_000_000_000 + index * 3_600_000,
        open=Decimal(close),
        high=Decimal(close + spread),
        low=Decimal(close - spread),
        close=Decimal(close),
        vol=Decimal("100"),
        vol_quote=Decimal("10000"),
        confirm=1,
        inst_id="BTC-USDT",
        bar_size="1H",
    )


# 与 web/src/chart/indicators.test.ts 的 pivotLevels 用例同一组数据
TS_PATH = [100, 105, 110, 105, 100, 105, 110, 105, 100, 105, 95, 90]


class TestPivotLevels:
    def test_matches_frontend_case(self) -> None:
        bars = [make_bar(close, index) for index, close in enumerate(TS_PATH)]
        levels = pivot_levels(bars, left_right=2, tolerance_pct="0.5", max_levels=5)

        assert [(level.price, level.touches) for level in levels] == [
            (Decimal("99"), 1),
            (Decimal("111"), 2),
        ]

    def test_result_is_ascending(self) -> None:
        bars = [make_bar(close, index) for index, close in enumerate(TS_PATH)]
        levels = pivot_levels(bars, left_right=2, tolerance_pct="0.5")
        prices = [level.price for level in levels]
        assert prices == sorted(prices)

    def test_empty_when_not_enough_bars(self) -> None:
        bars = [make_bar(100, index) for index in range(4)]
        assert pivot_levels(bars, left_right=3) == []

    def test_no_pivot_in_monotonic_series(self) -> None:
        bars = [make_bar(100 + index, index) for index in range(20)]
        assert pivot_levels(bars, left_right=2) == []

    def test_max_levels_truncated(self) -> None:
        bars = [make_bar(close, index) for index, close in enumerate(TS_PATH)]
        assert len(pivot_levels(bars, left_right=2, tolerance_pct="0.5", max_levels=1)) == 1

    def test_invalid_params_rejected(self) -> None:
        bars = [make_bar(100, index) for index in range(10)]
        with pytest.raises(ValueError):
            pivot_levels(bars, left_right=0)
        with pytest.raises(ValueError):
            pivot_levels(bars, max_levels=0)
        with pytest.raises(ValueError):
            pivot_levels(bars, tolerance_pct="-1")


class TestNearestLevels:
    def test_picks_adjacent_sides(self) -> None:
        levels = [Level(price=Decimal("90"), touches=1), Level(price=Decimal("110"), touches=2)]
        support, resistance = nearest_levels(levels, Decimal("100"))
        assert support is not None and support.price == Decimal("90")
        assert resistance is not None and resistance.price == Decimal("110")

    def test_level_above_and_below(self) -> None:
        levels = [Level(price=Decimal("90"), touches=1), Level(price=Decimal("110"), touches=2)]
        support, resistance = nearest_levels(levels, Decimal("120"))
        assert support is not None and support.price == Decimal("110")
        assert resistance is None

    def test_no_levels(self) -> None:
        assert nearest_levels([], Decimal("100")) == (None, None)

    def test_price_equal_counts_as_resistance(self) -> None:
        levels = [Level(price=Decimal("100"), touches=1)]
        support, resistance = nearest_levels(levels, Decimal("100"))
        assert support is None
        assert resistance is not None


class TestLevelValidation:
    def test_rejects_non_positive_price(self) -> None:
        with pytest.raises(ValueError):
            Level(price=Decimal("0"), touches=1)

    def test_rejects_non_positive_touches(self) -> None:
        with pytest.raises(ValueError):
            Level(price=Decimal("1"), touches=0)
