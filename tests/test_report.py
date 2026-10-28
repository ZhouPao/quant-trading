"""报告生成单元测试（spec §4.6）。

覆盖：
- CSV：表头、交易/权益行、时间排序、父目录自动创建
- HTML：指标表、内联 SVG、交易明细、空交易/无穷盈亏比处理
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quant.backtest.engine import BacktestResult
from quant.backtest.order import Fill
from quant.backtest.position import Position
from quant.backtest.report import ReportGenerator

_TS0 = 1_700_000_000_000
_HOUR = 3_600_000


def _fill(
    side: str = "buy",
    price: str = "100",
    ts: int = _TS0 + _HOUR,
    fee: str = "0.1",
) -> Fill:
    return Fill(
        order_id=f"o-{ts}",
        inst_id="BTC-USDT",
        side=side,
        qty=Decimal("1"),
        price=Decimal(price),
        fee=Decimal(fee),
        ts=ts,
    )


def _result(
    trades: list[Fill] | None = None,
    metrics: dict[str, float] | None = None,
) -> BacktestResult:
    equity_curve = [
        (_TS0, Decimal("10000")),
        (_TS0 + _HOUR, Decimal("10010")),
    ]
    return BacktestResult(
        trades=trades if trades is not None else [_fill()],
        equity_curve=equity_curve,
        unfilled_signals=[],
        final_position=Position(inst_id="BTC-USDT"),
        initial_cash=Decimal("10000"),
        final_cash=Decimal("10010"),
        drawdown_curve=[(_TS0, Decimal("0")), (_TS0 + _HOUR, Decimal("0.001"))],
        metrics=metrics
        if metrics is not None
        else {
            "total_return": 0.001,
            "annual_return": 0.1,
            "sharpe": 1.5,
            "max_drawdown": 0.05,
            "win_rate": 0.6,
            "profit_factor": 1.8,
            "trade_count": 5,
            "avg_holding_bars": 3.2,
        },
    )


# =====================================================================
# CSV
# =====================================================================


class TestCsvReport:
    def test_creates_file_and_header(self, tmp_path: Path) -> None:
        out = tmp_path / "report.csv"
        ReportGenerator().to_csv(_result(), out)

        assert out.exists()
        header = out.read_text(encoding="utf-8").splitlines()[0]
        assert header == (
            "record_type,ts,inst_id,side,qty,price,fee,equity,drawdown"
        )

    def test_contains_equity_and_trade_rows(self, tmp_path: Path) -> None:
        out = tmp_path / "report.csv"
        ReportGenerator().to_csv(_result(), out)
        text = out.read_text(encoding="utf-8")

        assert "equity" in text
        assert "trade" in text
        assert "10010" in text
        assert "BTC-USDT" in text

    def test_rows_sorted_by_ts(self, tmp_path: Path) -> None:
        out = tmp_path / "report.csv"
        ReportGenerator().to_csv(_result(), out)
        lines = out.read_text(encoding="utf-8").splitlines()[1:]

        timestamps = [int(line.split(",")[1]) for line in lines]
        assert timestamps == sorted(timestamps)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "dir" / "report.csv"
        ReportGenerator().to_csv(_result(), out)
        assert out.exists()

    def test_empty_result(self, tmp_path: Path) -> None:
        result = BacktestResult(
            trades=[],
            equity_curve=[],
            unfilled_signals=[],
            final_position=Position(inst_id="BTC-USDT"),
            initial_cash=Decimal("10000"),
            final_cash=Decimal("10000"),
        )
        out = tmp_path / "empty.csv"
        ReportGenerator().to_csv(result, out)

        assert out.exists()
        assert len(out.read_text(encoding="utf-8").splitlines()) == 1  # 仅表头


# =====================================================================
# HTML
# =====================================================================


class TestHtmlReport:
    def test_creates_file(self, tmp_path: Path) -> None:
        out = tmp_path / "report.html"
        ReportGenerator().to_html(_result(), out)
        assert out.exists()

    def test_contains_metrics_and_charts(self, tmp_path: Path) -> None:
        out = tmp_path / "report.html"
        ReportGenerator().to_html(_result(), out)
        text = out.read_text(encoding="utf-8")

        assert "总收益率" in text
        assert "0.10%" in text  # 0.001 格式化为百分比
        assert "权益曲线" in text
        assert "回撤曲线" in text
        assert "<svg" in text
        assert "<polyline" in text

    def test_contains_trade_details(self, tmp_path: Path) -> None:
        out = tmp_path / "report.html"
        ReportGenerator().to_html(_result(), out)
        text = out.read_text(encoding="utf-8")

        assert "BTC-USDT" in text
        assert "buy" in text

    def test_empty_trades_shows_placeholder(self, tmp_path: Path) -> None:
        out = tmp_path / "report.html"
        ReportGenerator().to_html(_result(trades=[]), out)
        assert "无成交" in out.read_text(encoding="utf-8")

    def test_infinite_profit_factor_formatted(self, tmp_path: Path) -> None:
        metrics = {
            "total_return": 0.1,
            "annual_return": 0.2,
            "sharpe": 1.0,
            "max_drawdown": 0.0,
            "win_rate": 1.0,
            "profit_factor": float("inf"),
            "trade_count": 1,
            "avg_holding_bars": 1.0,
        }
        out = tmp_path / "report.html"
        ReportGenerator().to_html(_result(metrics=metrics), out)
        assert "inf" in out.read_text(encoding="utf-8")

    def test_empty_equity_no_crash(self, tmp_path: Path) -> None:
        result = BacktestResult(
            trades=[],
            equity_curve=[],
            unfilled_signals=[],
            final_position=Position(inst_id="BTC-USDT"),
            initial_cash=Decimal("10000"),
            final_cash=Decimal("10000"),
        )
        out = tmp_path / "empty.html"
        ReportGenerator().to_html(result, out)
        assert out.exists()
