"""M5-6~11：横截面标准化、IC/RankIC/ICIR、分层、衰减、换手、相关性。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.factors.evaluate import (
    _forward_returns,
    _icir,
    _pearson,
    correlation_matrix,
    evaluate,
    percentile,
    zscore,
)

HOUR = 3_600_000
TS0 = 1_699_000_000_000


def _prices(insts: int = 10, count: int = 30) -> dict:
    result = {}
    for idx in range(insts):
        price = Decimal("100")
        trend = Decimal("0.005") * (idx + 1)
        series = {}
        for i in range(count):
            series[TS0 + i * HOUR] = price
            price = price * (1 + trend)
        result[f"I{idx}"] = series
    return result


def _factor_from_forward(prices: dict) -> dict:
    """构造「因子值 = 未来 1 根收益」的完美因子。"""
    fwd = _forward_returns(prices, 1)
    return {
        inst: {
            ts: Decimal(str(fwd[ts][inst]))
            for ts in fwd
            if inst in fwd[ts]
        }
        for inst in prices
    }


class TestBasicStats:
    def test_pearson_insufficient_points(self) -> None:
        assert _pearson([1.0, 2.0], [1.0, 2.0]) is None

    def test_pearson_perfect(self) -> None:
        assert _pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)

    def test_icir_none_when_constant(self) -> None:
        assert _icir([1.0, 1.0, 1.0]) is None


class TestCrossSection:
    def test_zscore(self) -> None:
        result = zscore(
            {"a": Decimal("1"), "b": Decimal("2"), "c": Decimal("3")}
        )

        assert result["b"] == Decimal("0")
        assert result["a"] < 0
        assert result["c"] > 0

    def test_zscore_skips_missing(self) -> None:
        result = zscore(
            {"a": Decimal("1"), "b": None, "c": Decimal("3")}
        )

        assert result["b"] is None
        assert result["a"] == Decimal("-1")

    def test_percentile(self) -> None:
        result = percentile(
            {"a": Decimal("1"), "b": Decimal("2"), "c": Decimal("3")}
        )

        assert result["a"] == Decimal("0")
        assert result["b"] == Decimal("0.5")
        assert result["c"] == Decimal("1")


class TestEvaluate:
    def test_perfect_factor_has_ic_one(self) -> None:
        prices = _prices()
        factor = _factor_from_forward(prices)

        result = evaluate(factor, prices)

        assert result.ic_mean == pytest.approx(1.0)
        assert result.rank_ic_mean == pytest.approx(1.0)

    def test_quantile_returns_are_monotonic(self) -> None:
        prices = _prices()
        factor = _factor_from_forward(prices)

        result = evaluate(factor, prices)

        # 每档取第一个完整 row 检查：低档收益 < 高档收益
        _, row = result.quantile_returns[0]
        assert all(value is not None for value in row)
        assert row[0] < row[-1]
        assert result.long_short[0][1] > 0

    def test_icir_none_for_constant_ic(self) -> None:
        prices = _prices()
        factor = _factor_from_forward(prices)

        result = evaluate(factor, prices)

        # 完美因子每期 IC 都是 1：标准差为 0，ICIR 无定义
        assert result.icir is None

    def test_decay_has_all_lags_and_decreases(self) -> None:
        prices = _prices()
        factor = _factor_from_forward(prices)

        result = evaluate(factor, prices, decay_lags=3)

        assert [lag for lag, _ in result.decay] == [1, 2, 3]
        assert result.decay[0][1] == pytest.approx(1.0)
        assert result.decay[-1][1] < result.decay[0][1]

    def test_turnover_in_range(self) -> None:
        prices = _prices()
        factor = _factor_from_forward(prices)

        result = evaluate(factor, prices)

        assert result.turnover is not None
        assert 0 <= result.turnover <= 1

    def test_horizon_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="horizon"):
            evaluate({}, {}, horizon=0)


class TestCorrelation:
    def test_duplicate_factors_are_redundant(self) -> None:
        left = [Decimal(i) for i in range(20)]
        right = [Decimal(i * 2) for i in range(20)]

        result = correlation_matrix({"a": left, "b": right})

        assert result.matrix[("a", "b")] == pytest.approx(1.0)
        assert ("a", "b") in result.redundant

    def test_diagonal_is_one(self) -> None:
        series = [Decimal(i) for i in range(20)]

        result = correlation_matrix({"a": series})

        assert result.matrix[("a", "a")] == pytest.approx(1.0)

    def test_missing_pairs_are_dropped(self) -> None:
        left = [Decimal(i) if i % 2 == 0 else None for i in range(20)]
        right = [Decimal(i * 2) for i in range(20)]

        result = correlation_matrix({"a": left, "b": right})

        # 只保留两边都有值的 10 个点，相关性仍应为 1
        assert result.matrix[("a", "b")] == pytest.approx(1.0)
