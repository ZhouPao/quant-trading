"""Pine 回测结果导入对比（M4-16）单元测试。

覆盖：
- CSV 解析：表头/无表头、百分号、空行注释、列数错误、数值非法、空输入
- 偏离度：正常比例、分母为 0 的两种退化、未匹配指标、全未匹配报错
- 落库列：只写有 `pine_*` 列的三个指标 + 最大偏离度
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.pine_compare import (
    PineCompareError,
    compare,
    parse_pine_csv,
    to_metric_columns,
)

PYTHON_METRICS = {
    "total_return": "-0.0617",
    "sharpe": "-4.8",
    "max_drawdown": "0.0921",
    "trade_count": 4,
    "monthly_returns": '[{"month": "2026-10", "ret": -0.06}]',
}


class TestParse:
    def test_with_header(self) -> None:
        pine = parse_pine_csv("metric,value\ntotal_return,-0.0617\nsharpe,-4.8")

        assert pine == {
            "total_return": Decimal("-0.0617"),
            "sharpe": Decimal("-4.8"),
        }

    def test_without_header(self) -> None:
        pine = parse_pine_csv("total_return,-0.0617\n")

        assert pine == {"total_return": Decimal("-0.0617")}

    def test_percent_value_becomes_fraction(self) -> None:
        pine = parse_pine_csv("max_drawdown,9.21%")

        assert pine == {"max_drawdown": Decimal("0.0921")}

    def test_skips_blank_and_comment(self) -> None:
        pine = parse_pine_csv("\n# 从 TV 抄的\nsharpe,1.1\n\n")

        assert pine == {"sharpe": Decimal("1.1")}

    def test_last_duplicate_wins(self) -> None:
        pine = parse_pine_csv("sharpe,1.1\nsharpe,1.2\n")

        assert pine == {"sharpe": Decimal("1.2")}

    def test_rejects_wrong_column_count(self) -> None:
        with pytest.raises(PineCompareError, match="两列"):
            parse_pine_csv("metric,value,note\nsharpe,1.1,x")

    def test_rejects_bad_number_after_header(self) -> None:
        with pytest.raises(PineCompareError, match="数值非法"):
            parse_pine_csv("metric,value\nsharpe,abc")

    def test_rejects_empty_input(self) -> None:
        with pytest.raises(PineCompareError, match="没有解析到"):
            parse_pine_csv("   \n# 只有注释\n")


class TestCompare:
    def test_computes_deviation_and_flags_over_limit(self) -> None:
        pine = {
            "total_return": Decimal("-0.0617"),  # 与 Python 一致
            "sharpe": Decimal("-4.0"),           # -4.0 vs -4.8 -> 16.67%
        }

        result = compare(pine, PYTHON_METRICS)

        rows = {row.metric: row for row in result["rows"]}
        assert rows["total_return"].deviation == Decimal("0")
        assert rows["total_return"].over_limit is False
        assert rows["sharpe"].over_limit is True
        assert rows["sharpe"].deviation == pytest.approx(
            Decimal("0.1666666666666666666666666667"), rel=Decimal("1e-9")
        )
        assert result["over_limit"] is True
        assert result["unmatched"] == []

    def test_exactly_five_percent_is_not_over(self) -> None:
        pine = {"sharpe": Decimal("-5.04")}  # 5.0% 整

        result = compare(pine, {"sharpe": "-4.8"})

        assert result["rows"][0].deviation == Decimal("0.05")
        assert result["rows"][0].over_limit is False
        assert result["over_limit"] is False

    def test_zero_on_both_sides_is_match(self) -> None:
        result = compare({"sharpe": Decimal("0")}, {"sharpe": "0"})

        assert result["rows"][0].deviation == Decimal("0")
        assert result["over_limit"] is False

    def test_zero_denominator_with_nonzero_pine_is_over_limit(self) -> None:
        result = compare({"sharpe": Decimal("0.5")}, {"sharpe": "0"})

        assert result["rows"][0].deviation is None
        assert result["rows"][0].over_limit is True
        assert result["max_deviation"] is None
        assert result["over_limit"] is True

    def test_unmatched_metric_is_listed_not_fatal(self) -> None:
        pine = {"sharpe": Decimal("-4.8"), "net_profit": Decimal("123")}

        result = compare(pine, PYTHON_METRICS)

        assert result["unmatched"] == ["net_profit"]
        assert [row.metric for row in result["rows"]] == ["sharpe"]

    def test_non_numeric_python_metric_is_unmatched(self) -> None:
        """monthly_returns 是 JSON 文本，不能当数值比。"""
        result = compare(
            {"sharpe": Decimal("-4.8"), "monthly_returns": Decimal("1")},
            PYTHON_METRICS,
        )

        assert [row.metric for row in result["rows"]] == ["sharpe"]
        assert result["unmatched"] == ["monthly_returns"]

    def test_all_unmatched_raises(self) -> None:
        with pytest.raises(PineCompareError, match="没有同名项"):
            compare({"net_profit": Decimal("1")}, PYTHON_METRICS)


class TestMetricColumns:
    def test_writes_only_persisted_metrics_and_deviation(self) -> None:
        pine = {
            "total_return": Decimal("-0.06"),
            "sharpe": Decimal("-4.8"),
            "max_drawdown": Decimal("0.09"),
            "trade_count": Decimal("4"),
        }
        result = compare(pine, {"sharpe": "-4.8", "trade_count": "4"})

        columns = to_metric_columns(pine, result)

        assert columns == {
            "pine_total_return": "-0.06",
            "pine_sharpe": "-4.8",
            "pine_max_drawdown": "0.09",
            "pine_deviation": "0",
        }

    def test_zero_denominator_over_limit_stores_explicit_value(self) -> None:
        pine = {"sharpe": Decimal("0.5")}
        result = compare(pine, {"sharpe": "0"})

        columns = to_metric_columns(pine, result)

        # 算不出比例时也要留痕，否则「超限」在库里看不出来
        assert columns["pine_deviation"] == "1"
