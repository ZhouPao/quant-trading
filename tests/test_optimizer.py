"""网格参数寻优单元测试（spec §4.7）。

覆盖：
- 组合穷举数量与参数透传
- 按指标降序排序
- 空网格（无参数）跑一次
- 未知指标 / 非法 n_jobs / 非标量参数校验
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.matcher import Matcher
from quant.backtest.optimizer import GridOptimizer
from quant.backtest.signal import Signal
from quant.backtest.strategy import Strategy
from quant.models.bar import Bar

_TS0 = 1_700_000_000_000
_HOUR = 3_600_000


def _bars() -> list[Bar]:
    """4 根 bar：100 → 110 → 120 → 120，开盘价用于下一根成交。"""
    ohlc = [
        ("100", "100", "100", "100"),
        ("100", "110", "100", "110"),
        ("110", "120", "110", "120"),
        ("120", "120", "120", "120"),
    ]
    return [
        Bar(
            ts=_TS0 + i * _HOUR,
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(low),
            close=Decimal(c),
            vol=Decimal("1"),
            vol_quote=Decimal("1"),
            confirm=1,
            inst_id="BTC-USDT",
            bar_size="1H",
        )
        for i, (o, h, low, c) in enumerate(ohlc)
    ]


class _QtyStrategy(Strategy):
    """首根 bar 买入固定数量，用于验证参数对收益的影响。"""

    def __init__(self) -> None:
        self.qty = Decimal("1")
        self.received_params: dict = {}

    @property
    def strategy_id(self) -> str:
        return "qty"

    def on_init(self, ctx) -> None:
        self.received_params = dict(ctx.params)
        self.qty = Decimal(str(ctx.params.get("qty", "1")))

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
        if len(ctx.bars_window) == 3 and ctx.position.is_long:
            return Signal(
                strategy_id=self.strategy_id,
                side="sell",
                order_type="market",
                inst_id=bar.inst_id,
                qty=ctx.position.qty,
                ts=bar.ts,
            )
        return None


def _factory(params: dict) -> _QtyStrategy:
    return _QtyStrategy()


def _optimizer(grid: dict) -> GridOptimizer:
    return GridOptimizer(
        _factory,
        grid,
        initial_cash=Decimal("10000"),
        matcher=Matcher(commission_rate=Decimal("0")),
    )


class TestGridOptimizer:
    def test_enumerates_all_combinations(self) -> None:
        optimizer = _optimizer({"qty": ["1", "2"], "unused": [1, 2]})
        results = optimizer.search(_bars(), metric="total_return")

        assert len(results) == 4
        assert {tuple(sorted(p.items())) for p, _ in results} == {
            (("qty", "1"), ("unused", 1)),
            (("qty", "1"), ("unused", 2)),
            (("qty", "2"), ("unused", 1)),
            (("qty", "2"), ("unused", 2)),
        }

    def test_sorted_by_metric_desc(self) -> None:
        """qty=2 的收益是 qty=1 的两倍，应排在前面。"""
        results = _optimizer({"qty": ["1", "2"]}).search(
            _bars(), metric="total_return"
        )

        assert results[0][0] == {"qty": "2"}
        assert results[0][1]["total_return"] > results[1][1]["total_return"]

    def test_params_passed_to_strategy_and_engine(self) -> None:
        optimizer = _optimizer({"qty": ["3"]})
        results = optimizer.search(_bars(), metric="total_return")

        assert results[0][0] == {"qty": "3"}
        assert results[0][1]["trade_count"] == 1.0

    def test_empty_grid_runs_once(self) -> None:
        results = _optimizer({}).search(_bars(), metric="total_return")
        assert len(results) == 1
        assert results[0][0] == {}

    def test_unknown_metric_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown metric"):
            _optimizer({"qty": ["1"]}).search(_bars(), metric="not_a_metric")

    def test_invalid_n_jobs_raises(self) -> None:
        with pytest.raises(ValueError, match="n_jobs"):
            _optimizer({"qty": ["1"]}).search(_bars(), n_jobs=0)

    def test_non_scalar_param_raises(self) -> None:
        with pytest.raises(ValueError, match="scalar"):
            _optimizer({"qty": [lambda: 1]})

    def test_factory_must_be_callable(self) -> None:
        with pytest.raises(ValueError, match="callable"):
            GridOptimizer("not-callable", {"qty": ["1"]})  # type: ignore[arg-type]

    def test_metrics_complete(self) -> None:
        results = _optimizer({"qty": ["1"]}).search(_bars())
        metrics = results[0][1]
        assert "sharpe" in metrics
        assert "total_return" in metrics
