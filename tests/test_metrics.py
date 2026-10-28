"""绩效指标单元测试（spec §4.5）。

覆盖：
- 空输入 / 收益率 / 年化 / 夏普 / 最大回撤
- 回合统计：胜率、盈亏比、交易次数、平均持仓 K线数
- FIFO 配对、部分平仓、反手、手续费计入
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.metrics import (
    MS_PER_DAY,
    MS_PER_YEAR,
    compute_drawdown_curve,
    compute_metrics,
)
from quant.backtest.order import Fill
from quant.models.bar import Bar

_TS0 = 1_700_000_000_000
_HOUR = 3_600_000


def _bar(ts: int, close: str = "100") -> Bar:
    price = Decimal(close)
    return Bar(
        ts=ts,
        open=price,
        high=price,
        low=price,
        close=price,
        vol=Decimal("1"),
        vol_quote=Decimal("1"),
        confirm=1,
        inst_id="BTC-USDT",
        bar_size="1H",
    )


def _fill(
    side: str,
    price: str,
    ts: int,
    qty: str = "1",
    fee: str = "0",
) -> Fill:
    return Fill(
        order_id=f"o-{ts}-{side}-{price}",
        inst_id="BTC-USDT",
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        ts=ts,
    )


def _curve(points: list[tuple[int, str]]) -> list[tuple[int, Decimal]]:
    return [(ts, Decimal(v)) for ts, v in points]


# =====================================================================
# 空输入与基础指标
# =====================================================================


class TestBasicMetrics:
    def test_empty_equity_returns_zeros(self) -> None:
        m = compute_metrics([], [], [])
        assert m["total_return"] == 0.0
        assert m["sharpe"] == 0.0
        assert m["max_drawdown"] == 0.0
        assert m["trade_count"] == 0.0

    def test_total_return(self) -> None:
        curve = _curve([(_TS0, "100"), (_TS0 + _HOUR, "110")])
        m = compute_metrics([], curve, [_bar(_TS0), _bar(_TS0 + _HOUR)])
        assert m["total_return"] == pytest.approx(0.1)

    def test_total_return_negative(self) -> None:
        curve = _curve([(_TS0, "100"), (_TS0 + _HOUR, "80")])
        m = compute_metrics([], curve, [_bar(_TS0), _bar(_TS0 + _HOUR)])
        assert m["total_return"] == pytest.approx(-0.2)

    def test_max_drawdown(self) -> None:
        """100 → 120 → 90 → 100：最大回撤 = 1 - 90/120 = 0.25。"""
        curve = _curve(
            [
                (_TS0, "100"),
                (_TS0 + _HOUR, "120"),
                (_TS0 + 2 * _HOUR, "90"),
                (_TS0 + 3 * _HOUR, "100"),
            ]
        )
        m = compute_metrics([], curve, [_bar(_TS0)])
        assert m["max_drawdown"] == pytest.approx(0.25)

    def test_drawdown_curve_values(self) -> None:
        curve = _curve(
            [
                (_TS0, "100"),
                (_TS0 + _HOUR, "120"),
                (_TS0 + 2 * _HOUR, "90"),
            ]
        )
        dd = compute_drawdown_curve(curve)
        assert [float(v) for _, v in dd] == pytest.approx([0.0, 0.0, 0.25])

    def test_flat_sharpe_zero(self) -> None:
        """权益不变 → 波动为 0 → 夏普 0。"""
        curve = _curve(
            [
                (_TS0, "100"),
                (_TS0 + MS_PER_DAY, "100"),
                (_TS0 + 2 * MS_PER_DAY, "100"),
                (_TS0 + 3 * MS_PER_DAY, "100"),
            ]
        )
        m = compute_metrics([], curve, [_bar(_TS0)])
        assert m["sharpe"] == 0.0

    def test_sharpe_positive_for_uptrend(self) -> None:
        curve = _curve(
            [
                (_TS0, "100"),
                (_TS0 + MS_PER_DAY, "101"),
                (_TS0 + 2 * MS_PER_DAY, "102"),
                (_TS0 + 3 * MS_PER_DAY, "103"),
            ]
        )
        m = compute_metrics([], curve, [_bar(_TS0)])
        assert m["sharpe"] > 0

    def test_annual_return_one_year(self) -> None:
        """恰好 1 年、总收益 10% → 年化≈10%。"""
        start = _TS0
        end = start + MS_PER_YEAR
        curve = _curve([(start, "100"), (end, "110")])
        m = compute_metrics([], curve, [_bar(start), _bar(end)])
        assert m["annual_return"] == pytest.approx(0.1, rel=1e-6)

    def test_annual_return_short_period_zero(self) -> None:
        """不足 1 天 → 年化返回 0，避免指数爆炸。"""
        curve = _curve([(_TS0, "100"), (_TS0 + _HOUR, "110")])
        m = compute_metrics([], curve, [_bar(_TS0), _bar(_TS0 + _HOUR)])
        assert m["annual_return"] == 0.0


# =====================================================================
# 回合统计
# =====================================================================


class TestTradeStats:
    def test_win_rate_and_profit_factor(self) -> None:
        """一个盈利回合 +10，一个亏损回合 -10 → 胜率 0.5，盈亏比 1.0。"""
        trades = [
            _fill("buy", "100", _TS0),
            _fill("sell", "110", _TS0 + _HOUR),
            _fill("buy", "100", _TS0 + 2 * _HOUR),
            _fill("sell", "90", _TS0 + 3 * _HOUR),
        ]
        bars = [_bar(_TS0 + i * _HOUR) for i in range(4)]
        curve = _curve([(_TS0, "100"), (_TS0 + 3 * _HOUR, "100")])

        m = compute_metrics(trades, curve, bars)

        assert m["trade_count"] == 2.0
        assert m["win_rate"] == pytest.approx(0.5)
        assert m["profit_factor"] == pytest.approx(1.0)

    def test_all_wins_profit_factor_inf(self) -> None:
        trades = [
            _fill("buy", "100", _TS0),
            _fill("sell", "110", _TS0 + _HOUR),
        ]
        m = compute_metrics(trades, _curve([(_TS0, "100")]), [_bar(_TS0)])

        assert m["win_rate"] == pytest.approx(1.0)
        assert m["profit_factor"] == float("inf")

    def test_fees_included_in_pnl(self) -> None:
        """平价买卖各付 0.1 手续费 → 回合亏损 0.2，胜率 0。"""
        trades = [
            _fill("buy", "100", _TS0, fee="0.1"),
            _fill("sell", "100", _TS0 + _HOUR, fee="0.1"),
        ]
        m = compute_metrics(trades, _curve([(_TS0, "100")]), [_bar(_TS0)])

        assert m["trade_count"] == 1.0
        assert m["win_rate"] == 0.0
        assert m["profit_factor"] == 0.0

    def test_avg_holding_bars(self) -> None:
        """买 1 持 1 根卖出；再买 1 持 2 根卖出 → 平均 1.5 根。"""
        trades = [
            _fill("buy", "100", _TS0),
            _fill("sell", "110", _TS0 + _HOUR),
            _fill("buy", "100", _TS0 + 2 * _HOUR),
            _fill("sell", "90", _TS0 + 4 * _HOUR),
        ]
        bars = [_bar(_TS0 + i * _HOUR) for i in range(5)]
        m = compute_metrics(trades, _curve([(_TS0, "100")]), bars)

        assert m["avg_holding_bars"] == pytest.approx(1.5)

    def test_partial_close_fifo(self) -> None:
        """买 2，先卖 1（盈利 +10），再卖 1（亏损 -10）→ 两个回合。"""
        trades = [
            _fill("buy", "100", _TS0, qty="2"),
            _fill("sell", "110", _TS0 + _HOUR, qty="1"),
            _fill("sell", "90", _TS0 + 2 * _HOUR, qty="1"),
        ]
        bars = [_bar(_TS0 + i * _HOUR) for i in range(3)]
        m = compute_metrics(trades, _curve([(_TS0, "100")]), bars)

        assert m["trade_count"] == 2.0
        assert m["win_rate"] == pytest.approx(0.5)
        # 持仓 K线数：第一条 1 根、第二条 2 根 → 平均 1.5
        assert m["avg_holding_bars"] == pytest.approx(1.5)

    def test_reversal_counts_two_round_trips(self) -> None:
        """买 1 @100，卖 2 @110（平多+开空），买 1 @100（平空）→ 两回合均盈利。"""
        trades = [
            _fill("buy", "100", _TS0),
            _fill("sell", "110", _TS0 + _HOUR, qty="2"),
            _fill("buy", "100", _TS0 + 2 * _HOUR),
        ]
        m = compute_metrics(trades, _curve([(_TS0, "100")]), [_bar(_TS0)])

        assert m["trade_count"] == 2.0
        assert m["win_rate"] == pytest.approx(1.0)

    def test_no_closed_trades(self) -> None:
        """只有开仓、未平仓 → 回合数为 0。"""
        trades = [_fill("buy", "100", _TS0)]
        m = compute_metrics(trades, _curve([(_TS0, "100")]), [_bar(_TS0)])
        assert m["trade_count"] == 0.0
        assert m["win_rate"] == 0.0
