"""M4-15 永续资金费率结算测试。

覆盖：
- 现货 inst -> 永续 inst 映射、本地费率读取
- 配置开关：未启用返回 None、启用但缺数据报错
- 引擎结算：多头付正费率、空头收正费率、空仓不计费、单根 bar 跨多个结算点
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from quant.backtest.context import Context
from quant.backtest.engine import Backtest
from quant.backtest.funding import (
    FundingSchedule,
    load_funding_rates,
    swap_inst_id,
)
from quant.backtest.matcher import Matcher
from quant.backtest.order import Fill
from quant.backtest.runner import BacktestSetupError, load_funding_schedule
from quant.backtest.signal import Signal
from quant.backtest.strategy import Strategy
from quant.foundation.config import (
    BacktestSettings,
    OKXSettings,
    Settings,
    StorageSettings,
)
from quant.models.bar import Bar
from quant.models.funding_rate import FundingRate

H = 3_600_000
INST = "BTC-USDT"


def _bar(ts: int, open_: str) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal(open_),
        high=Decimal(open_),
        low=Decimal(open_),
        close=Decimal(open_),
        vol=Decimal("1"),
        vol_quote=Decimal("100"),
        confirm=1,
        inst_id=INST,
        bar_size="1H",
    )


def _rate(ts: int, rate: str) -> FundingRate:
    return FundingRate(
        inst_id=f"{INST}-SWAP",
        ts=ts,
        rate=Decimal(rate),
        next_funding_ts=ts + 8 * H,
    )


class TestCoverage:
    """费率数据必须覆盖整个回测区间，否则缺失段成本静默按 0 计（P0）。"""

    @staticmethod
    def _settings(tmp_path: Path) -> Settings:
        return Settings(
            okx=OKXSettings(
                api_key="k", secret="s", passphrase="p", demo=True
            ),
            storage=StorageSettings(data_dir=tmp_path / "data"),
            backtest=BacktestSettings(funding_rate_enabled=True),
        )

    def test_partial_coverage_rejected(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "quant.backtest.runner.load_funding_rates",
            lambda *_a, **_k: [_rate(0, "0.0001"), _rate(8 * H, "0.0001")],
        )

        with pytest.raises(BacktestSetupError, match="covers"):
            load_funding_schedule(
                self._settings(tmp_path),
                INST,
                start_ts=0,
                end_ts=48 * H,
            )

    def test_full_coverage_accepted(self, tmp_path, monkeypatch) -> None:
        rates = [_rate(i * 8 * H, "0.0001") for i in range(0, 7)]
        monkeypatch.setattr(
            "quant.backtest.runner.load_funding_rates",
            lambda *_a, **_k: rates,
        )

        schedule = load_funding_schedule(
            self._settings(tmp_path), INST, start_ts=0, end_ts=48 * H
        )

        assert schedule is not None
        assert len(schedule) == len(rates)


class _HoldOnce(Strategy):
    """第一根 bar 发一次信号，之后不再交易。"""

    def __init__(self, side: str = "buy") -> None:
        self.side = side
        self.fired = False

    @property
    def strategy_id(self) -> str:
        return "test"

    def on_init(self, ctx: Context) -> None:
        pass

    def on_bar(self, bar: Bar, ctx: Context) -> Signal | None:
        if self.fired:
            return None
        self.fired = True
        return Signal(
            strategy_id="test",
            side=self.side,
            order_type="market",
            inst_id=bar.inst_id,
            qty=Decimal("1"),
            ts=bar.ts,
            price=None,
        )

    def on_trade(self, trade: Fill, ctx: Context) -> None:
        pass


def _run(rates: list[FundingRate] | None, side: str = "buy"):
    bars = [
        _bar(0, "100"),
        _bar(H, "110"),
        _bar(2 * H, "120"),
        _bar(3 * H, "130"),
    ]
    funding = FundingSchedule.from_rates(rates) if rates is not None else None
    return Backtest(
        _HoldOnce(side),
        Matcher(),
        initial_cash=Decimal("10000"),
        funding=funding,
    ).run(bars)


# ---------------------------------------------------------------- 数据来源


def test_swap_inst_id_maps_spot_to_perp() -> None:
    assert swap_inst_id("BTC-USDT") == "BTC-USDT-SWAP"
    assert swap_inst_id("BTC-USDT-SWAP") == "BTC-USDT-SWAP"


def test_load_funding_rates_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_funding_rates(tmp_path, INST) == []


def test_load_funding_rates_reads_swap_file(tmp_path: Path) -> None:
    root = tmp_path / "funding_rates"
    root.mkdir()
    (root / f"{INST}-SWAP.json").write_text(
        json.dumps(
            [
                {
                    "inst_id": f"{INST}-SWAP",
                    "ts": 2 * H,
                    "rate": "0.0001",
                    "next_funding_ts": 10 * H,
                }
            ]
        ),
        encoding="utf-8",
    )
    rates = load_funding_rates(tmp_path, INST)
    assert len(rates) == 1
    assert rates[0].ts == 2 * H
    assert rates[0].rate == Decimal("0.0001")


def test_schedule_is_sorted_and_indexable() -> None:
    schedule = FundingSchedule.from_rates([_rate(2 * H, "0.1"), _rate(H, "0.2")])
    assert schedule.timestamps == (H, 2 * H)
    assert schedule.index_until(0) == 0
    assert schedule.index_until(H) == 1
    assert schedule.index_until(2 * H) == 2
    assert len(schedule) == 2


# ---------------------------------------------------------------- 配置开关


def _settings(tmp_path: Path, enabled: bool) -> Settings:
    return Settings(
        okx=OKXSettings(
            api_key="test-key",
            secret="test-secret",
            passphrase="test-pass",
            demo=True,
        ),
        storage=StorageSettings(data_dir=tmp_path),
        backtest=BacktestSettings(funding_rate_enabled=enabled),
    )


def test_schedule_disabled_by_config(tmp_path: Path) -> None:
    assert load_funding_schedule(_settings(tmp_path, False), INST) is None


def test_schedule_missing_data_raises(tmp_path: Path) -> None:
    with pytest.raises(BacktestSetupError, match="no funding rate data"):
        load_funding_schedule(_settings(tmp_path, True), INST)


def test_schedule_loaded_when_enabled(tmp_path: Path) -> None:
    root = tmp_path / "funding_rates"
    root.mkdir()
    (root / f"{INST}-SWAP.json").write_text(
        json.dumps(
            [
                {
                    "inst_id": f"{INST}-SWAP",
                    "ts": H,
                    "rate": "0.0001",
                    "next_funding_ts": 9 * H,
                }
            ]
        ),
        encoding="utf-8",
    )
    schedule = load_funding_schedule(_settings(tmp_path, True), INST)
    assert schedule is not None
    assert len(schedule) == 1


# ---------------------------------------------------------------- 引擎结算


def test_long_pays_positive_rate_and_receives_negative() -> None:
    rates = [_rate(H, "0.0001"), _rate(2 * H, "0.0001"), _rate(3 * H, "-0.0002")]
    result = _run(rates)
    # t=1h 结算发生在开仓成交之前（当时空仓），只结算 t=2h 与 t=3h
    assert result.funding_cost == Decimal("-0.014")
    assert result.metrics["funding_cost"] == pytest.approx(-0.014)
    # 9889.89 - 0.012 + 0.026
    assert result.final_cash == Decimal("9889.904")


def test_short_receives_positive_rate() -> None:
    rates = [_rate(2 * H, "0.0001")]
    result = _run(rates, side="sell")
    assert result.funding_cost == Decimal("-0.012")
    # 开空按引擎口径入账 +110.11（含手续费），再收 0.012 资金费率
    assert result.final_cash == Decimal("10110.122")


def test_flat_position_pays_nothing() -> None:
    class _NeverTrade(_HoldOnce):
        def on_bar(self, bar: Bar, ctx: Context) -> Signal | None:
            return None

    rates = [_rate(H, "0.0001"), _rate(2 * H, "0.0001")]
    bars = [_bar(0, "100"), _bar(H, "110"), _bar(2 * H, "120")]
    result = Backtest(
        _NeverTrade(),
        Matcher(),
        initial_cash=Decimal("10000"),
        funding=FundingSchedule.from_rates(rates),
    ).run(bars)
    assert result.funding_cost == Decimal("0")
    assert result.final_cash == Decimal("10000")


def test_multiple_settlements_in_one_bar() -> None:
    bars = [_bar(0, "100"), _bar(H, "110"), _bar(4 * H, "120")]
    rates = [_rate(H + H // 2, "0.001"), _rate(2 * H, "0.002")]
    result = Backtest(
        _HoldOnce("buy"),
        Matcher(),
        initial_cash=Decimal("10000"),
        funding=FundingSchedule.from_rates(rates),
    ).run(bars)
    # 两次结算都用 t=4h 的开盘价 120：(0.001 + 0.002) * 1 * 120
    assert result.funding_cost == Decimal("0.36")


def test_no_funding_when_disabled() -> None:
    result = _run(None)
    assert result.funding_cost == Decimal("0")
    assert result.metrics["funding_cost"] == 0.0
