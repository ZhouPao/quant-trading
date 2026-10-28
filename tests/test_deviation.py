"""模拟盘 vs 回测偏离检测测试（M3）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.analysis.deviation import (
    compare_fills,
    format_report,
    relative_deviation,
    summarize,
)
from quant.backtest.order import Fill


def _fill(side="buy", qty="1", price="100", fee="0.1", ts=1, inst="BTC-USDT"):
    return Fill(
        order_id=f"o-{ts}",
        inst_id=inst,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        ts=ts,
    )


class TestSummarize:
    def test_empty(self):
        s = summarize([])
        assert s.trade_count == 0
        assert s.buy_qty == 0
        assert s.sell_qty == 0
        assert s.notional == 0
        assert s.fee == 0

    def test_mixed(self):
        s = summarize([
            _fill("buy", "1", "100", "0.1"),
            _fill("sell", "2", "110", "0.22", ts=2),
        ])
        assert s.trade_count == 2
        assert s.buy_qty == Decimal("1")
        assert s.sell_qty == Decimal("2")
        assert s.notional == Decimal("320")
        assert s.fee == Decimal("0.32")


class TestRelativeDeviation:
    def test_equal(self):
        assert relative_deviation(Decimal("100"), Decimal("100")) == 0.0

    def test_greater(self):
        assert relative_deviation(Decimal("105"), Decimal("100")) == pytest.approx(0.05)

    def test_smaller_uses_abs(self):
        assert relative_deviation(Decimal("95"), Decimal("100")) == pytest.approx(0.05)

    def test_zero_base_zero_actual(self):
        assert relative_deviation(Decimal("0"), Decimal("0")) == 0.0

    def test_zero_base_nonzero_actual(self):
        assert relative_deviation(Decimal("1"), Decimal("0")) == 1.0


class TestCompareFills:
    def test_identical_passes(self):
        fills = [_fill("buy", ts=1), _fill("sell", ts=2)]
        report = compare_fills(fills, fills)
        assert report.within_threshold
        assert report.side_sequence_match
        assert report.max_deviation == 0.0

    def test_trade_count_mismatch_fails(self):
        paper = [_fill("buy", ts=1), _fill("sell", ts=2), _fill("buy", ts=3)]
        backtest = [_fill("buy", ts=1), _fill("sell", ts=2)]
        report = compare_fills(paper, backtest)
        assert report.trade_count_dev == pytest.approx(0.5)
        assert not report.within_threshold

    def test_price_deviation_fails(self):
        paper = [_fill("buy", price="110", ts=1)]
        backtest = [_fill("buy", price="100", ts=1)]
        report = compare_fills(paper, backtest)
        assert report.notional_dev == pytest.approx(0.10)
        assert not report.within_threshold

    def test_side_sequence_mismatch_fails_even_if_sizes_match(self):
        paper = [_fill("buy", ts=1), _fill("sell", ts=2)]
        backtest = [_fill("sell", ts=1), _fill("buy", ts=2)]
        report = compare_fills(paper, backtest)
        assert report.max_deviation == 0.0
        assert not report.side_sequence_match
        assert not report.within_threshold

    def test_threshold_boundary_inclusive(self):
        paper = [_fill("buy", price="105", ts=1)]
        backtest = [_fill("buy", price="100", ts=1)]
        report = compare_fills(paper, backtest, threshold=0.05)
        assert report.max_deviation == pytest.approx(0.05)
        assert report.within_threshold

    def test_invalid_threshold(self):
        with pytest.raises(ValueError):
            compare_fills([], [], threshold=-0.1)
        with pytest.raises(ValueError):
            compare_fills([], [], threshold=1.5)


class TestFormatReport:
    def test_contains_status_and_metrics(self):
        fills = [_fill("buy", ts=1)]
        text = format_report(compare_fills(fills, fills))
        assert "[PASS]" in text
        assert "成交笔数" in text
        assert "方向序列" in text

    def test_fail_status(self):
        text = format_report(compare_fills([_fill(ts=1)], []))
        assert "[FAIL]" in text
