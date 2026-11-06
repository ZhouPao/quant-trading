"""组合回测（PRD §4.6 / M6-12）。

把各单元的历史权益曲线（可直接用 M4 ``BacktestResult.equity_curve``）按
组合权重合成组合权益曲线，支持周期性再平衡；绩效指标复用
``metrics.compute_metrics``，保证与单次回测**同口径**。

验收口径：``rebalance_every_bars=0``（买入持有）时，组合权益曲线等于
逐单元曲线的加权和，因此「组合回测与逐单元单独回测的加权结果」可对齐。

本模块只读历史数据，不产出调仓单、不调用 OKX。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from quant.backtest.metrics import compute_metrics
from quant.backtest.order import Fill
from quant.models.bar import Bar

from .allocator import Allocator
from .models import PortfolioConfig

_ZERO = Decimal("0")


class PortfolioBacktestError(ValueError):
    """曲线/时间轴不完备；显式报错，不对齐不上就静默跳过。"""


@dataclass(frozen=True, slots=True)
class PortfolioBacktestResult:
    equity_curve: list[tuple[int, Decimal]]
    unit_curves: dict[str, list[tuple[int, Decimal]]]
    metrics: dict[str, float | int | str | None]
    rebalance_count: int


class PortfolioBacktest:
    """多单元并行 + 按目标权重再平衡的组合回测。"""

    def __init__(
        self,
        config: PortfolioConfig,
        initial_cash: Decimal = Decimal("10000"),
        rebalance_every_bars: int = 0,
        volatilities: Mapping[str, Decimal] | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise PortfolioBacktestError("initial_cash must be positive")
        if rebalance_every_bars < 0:
            raise PortfolioBacktestError("rebalance_every_bars must be >= 0")
        self._config = config
        self._initial_cash = initial_cash
        self._rebalance_every = rebalance_every_bars
        self._weights = Allocator(config, volatilities).weights()

    def _validate(
        self,
        unit_curves: Mapping[str, Sequence[tuple[int, Decimal]]],
        bars: Sequence[Bar],
    ) -> list[str]:
        keys = [unit.unit_key for unit in self._config.enabled_units()]
        if not keys:
            raise PortfolioBacktestError("portfolio has no enabled units")
        for key in keys:
            curve = unit_curves.get(key)
            if curve is None or len(curve) < 2:
                raise PortfolioBacktestError(
                    f"unit {key} needs an equity curve with >= 2 points"
                )
            if curve[0][1] <= 0:
                raise PortfolioBacktestError(
                    f"unit {key} initial equity must be positive"
                )
            if any(value <= 0 for _, value in curve):
                raise PortfolioBacktestError(
                    f"unit {key} equity must stay positive"
                )
        reference = [ts for ts, _ in unit_curves[keys[0]]]
        for key in keys[1:]:
            if [ts for ts, _ in unit_curves[key]] != reference:
                raise PortfolioBacktestError(
                    f"unit {key} has a different timeline; "
                    "all units must share ts (same period and range)"
                )
        if not bars:
            raise PortfolioBacktestError("bars must not be empty")
        if len(bars) != len(reference) or [bar.ts for bar in bars] != reference:
            raise PortfolioBacktestError(
                "bars timeline must match the unit equity curves"
            )
        return keys

    def run(
        self,
        unit_curves: Mapping[str, Sequence[tuple[int, Decimal]]],
        bars: Sequence[Bar],
        trades: Sequence[Fill] | None = None,
    ) -> PortfolioBacktestResult:
        keys = self._validate(unit_curves, bars)
        timeline = [ts for ts, _ in unit_curves[keys[0]]]

        capital = {
            key: self._initial_cash * self._weights[key] for key in keys
        }
        equity_curve: list[tuple[int, Decimal]] = [
            (timeline[0], sum(capital.values(), _ZERO))
        ]
        out_curves: dict[str, list[tuple[int, Decimal]]] = {
            key: [(timeline[0], capital[key])] for key in keys
        }
        rebalance_count = 0

        for index in range(1, len(timeline)):
            for key in keys:
                curve = unit_curves[key]
                ratio = curve[index][1] / curve[index - 1][1]
                capital[key] *= ratio
            if self._rebalance_every and index % self._rebalance_every == 0:
                total = sum(capital.values(), _ZERO)
                for key in keys:
                    capital[key] = total * self._weights[key]
                rebalance_count += 1
            total = sum(capital.values(), _ZERO)
            equity_curve.append((timeline[index], total))
            for key in keys:
                out_curves[key].append((timeline[index], capital[key]))

        metrics = compute_metrics(
            list(trades or []), equity_curve, list(bars)
        )
        return PortfolioBacktestResult(
            equity_curve=equity_curve,
            unit_curves=out_curves,
            metrics=metrics,
            rebalance_count=rebalance_count,
        )
