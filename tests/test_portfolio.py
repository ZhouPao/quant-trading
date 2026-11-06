"""M6-1/2/3/4/13（8-1）：组合配置、四种分配、归一上限、单策略兼容。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quant.foundation.config import Settings
from quant.portfolio import (
    AllocationError,
    Allocator,
    PortfolioConfig,
    PortfolioConfigError,
    PortfolioUnit,
    allocate,
    annualized_volatility,
    load_portfolio,
    load_portfolios,
)

_TWO_UNITS = (
    PortfolioUnit(strategy_id="sma_cross", inst_id="BTC-USDT", weight=Decimal("0.6")),
    PortfolioUnit(strategy_id="rsi", inst_id="ETH-USDT", weight=Decimal("0.4")),
)


def _config(**kwargs: object) -> PortfolioConfig:
    return PortfolioConfig(portfolio_id="core", units=_TWO_UNITS, **kwargs)  # type: ignore[arg-type]


def _sum(weights: dict[str, Decimal]) -> Decimal:
    return sum(weights.values(), Decimal("0"))


class TestPortfolioConfig:
    def test_unit_key_and_enabled_units(self) -> None:
        units = (
            _TWO_UNITS[0],
            PortfolioUnit(strategy_id="rsi", inst_id="ETH-USDT", enabled=False),
        )
        cfg = PortfolioConfig(portfolio_id="core", units=units)
        assert cfg.units[0].unit_key == "sma_cross:BTC-USDT"
        assert len(cfg.enabled_units()) == 1
        # 停用单元仍留在配置里（保留持仓与历史，M6-2）
        assert len(cfg.unit_map()) == 2

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"portfolio_id": ""},
            {"allocator": "machine_learning"},
            {"units": ()},
            {"max_unit_weight": Decimal("1.5")},
            {"max_unit_weight": Decimal("0")},
            {"target_vol": Decimal("0")},
            {"vol_window_bars": 1},
        ],
    )
    def test_invalid_config_rejected(self, kwargs: dict[str, object]) -> None:
        base: dict[str, object] = {"portfolio_id": "core", "units": _TWO_UNITS}
        base.update(kwargs)
        with pytest.raises(ValueError):
            PortfolioConfig(**base)  # type: ignore[arg-type]

    def test_duplicate_unit_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate unit_key"):
            PortfolioConfig(
                portfolio_id="core",
                units=(_TWO_UNITS[0], _TWO_UNITS[0]),
            )

    def test_load_toml_keeps_decimal_precision(self, tmp_path: Path) -> None:
        path = tmp_path / "core.toml"
        path.write_text(
            """
portfolio_id = "core"
allocator = "fixed"
max_unit_weight = "0.60"
[[units]]
strategy_id = "sma_cross"
inst_id = "BTC-USDT"
params_ref = "sma_cross"
weight = "0.60"
[[units]]
strategy_id = "rsi"
inst_id = "ETH-USDT"
weight = "0.40"
enabled = false
[rebalance]
mode = "both"
drift_pct = "0.05"
cooldown_hours = 6
""",
            encoding="utf-8",
        )
        cfg = load_portfolio(path)
        assert cfg.portfolio_id == "core"
        assert cfg.allocator == "fixed"
        assert cfg.max_unit_weight == Decimal("0.60")
        assert cfg.units[0].weight == Decimal("0.60")
        assert cfg.units[0].params_ref == "sma_cross"
        assert cfg.units[1].enabled is False
        assert cfg.rebalance.mode == "both"
        assert len(cfg.enabled_units()) == 1

    def test_load_portfolios_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_portfolios(tmp_path / "nope") == []

    def test_load_portfolios_ignores_example_and_rejects_bad_file(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "core.toml.example").write_text(
            'portfolio_id = "ex"\n[[units]]\nstrategy_id = "s"\ninst_id = "i"\n',
            encoding="utf-8",
        )
        (tmp_path / "core.toml").write_text(
            'portfolio_id = "core"\n[[units]]\nstrategy_id = "s"\ninst_id = "i"\n',
            encoding="utf-8",
        )
        assert [cfg.portfolio_id for cfg in load_portfolios(tmp_path)] == ["core"]

        (tmp_path / "bad.toml").write_text(
            'portfolio_id = "bad"\n', encoding="utf-8"
        )
        with pytest.raises(PortfolioConfigError, match="bad"):
            load_portfolios(tmp_path)

    def test_non_finite_number_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "core.toml"
        path.write_text(
            'portfolio_id = "core"\nmax_unit_weight = "NaN"\n'
            '[[units]]\nstrategy_id = "s"\ninst_id = "i"\n',
            encoding="utf-8",
        )
        with pytest.raises(PortfolioConfigError, match="finite"):
            load_portfolio(path)


class TestAllocator:
    def test_equal_splits_and_allocates_amount(self) -> None:
        cfg = _config(allocator="equal")
        weights = Allocator(cfg).weights()
        assert weights["sma_cross:BTC-USDT"] == Decimal("0.5")
        assert weights["rsi:ETH-USDT"] == Decimal("0.5")
        assert _sum(weights) == Decimal("1")
        amounts = allocate(Decimal("10000"), cfg)
        assert amounts["sma_cross:BTC-USDT"] == Decimal("5000")

    def test_fixed_uses_configured_weights(self) -> None:
        weights = Allocator(_config(allocator="fixed")).weights()
        assert weights["sma_cross:BTC-USDT"] == Decimal("0.6")
        assert weights["rsi:ETH-USDT"] == Decimal("0.4")
        assert _sum(weights) == Decimal("1")

    def test_fixed_without_weight_fails_explicitly(self) -> None:
        cfg = PortfolioConfig(
            portfolio_id="core",
            units=(PortfolioUnit(strategy_id="s", inst_id="i"),),
            allocator="fixed",
        )
        with pytest.raises(AllocationError, match="positive weight"):
            Allocator(cfg).weights()

    def test_inverse_vol_weights_opposite_to_risk(self) -> None:
        vols = {
            "sma_cross:BTC-USDT": Decimal("0.2"),
            "rsi:ETH-USDT": Decimal("0.4"),
        }
        weights = Allocator(_config(allocator="inverse_vol"), vols).weights()
        assert weights["sma_cross:BTC-USDT"] > weights["rsi:ETH-USDT"]
        assert abs(weights["sma_cross:BTC-USDT"] - Decimal("2") / 3) < Decimal("1e-20")
        assert _sum(weights) == Decimal("1")

    def test_vol_target_keeps_cash_when_portfolio_too_risky(self) -> None:
        vols = {
            "sma_cross:BTC-USDT": Decimal("0.5"),
            "rsi:ETH-USDT": Decimal("0.5"),
        }
        cfg = _config(allocator="vol_target", target_vol=Decimal("0.20"))
        weights = Allocator(cfg, vols).weights()
        total = _sum(weights)
        assert Decimal("0") < total < Decimal("1")
        assert weights["sma_cross:BTC-USDT"] == weights["rsi:ETH-USDT"]

    def test_volatility_methods_need_positive_vol(self) -> None:
        cfg = _config(allocator="inverse_vol")
        with pytest.raises(AllocationError, match="volatility"):
            Allocator(cfg).weights()

    def test_disabled_unit_gets_zero_and_rest_renormalized(self) -> None:
        units = (_TWO_UNITS[0], PortfolioUnit("rsi", "ETH-USDT", enabled=False))
        cfg = PortfolioConfig(portfolio_id="core", units=units)
        weights = Allocator(cfg).weights()
        assert weights["sma_cross:BTC-USDT"] == Decimal("1")
        assert weights["rsi:ETH-USDT"] == Decimal("0")

    def test_all_units_disabled_returns_all_zero(self) -> None:
        units = tuple(
            PortfolioUnit(u.strategy_id, u.inst_id, enabled=False) for u in _TWO_UNITS
        )
        cfg = PortfolioConfig(portfolio_id="core", units=units)
        assert _sum(Allocator(cfg).weights()) == Decimal("0")

    def test_max_unit_weight_caps_and_redistributes(self) -> None:
        units = (
            PortfolioUnit("a", "BTC-USDT", weight=Decimal("0.8")),
            PortfolioUnit("b", "ETH-USDT", weight=Decimal("0.1")),
            PortfolioUnit("c", "SOL-USDT", weight=Decimal("0.1")),
        )
        cfg = PortfolioConfig(
            portfolio_id="core",
            units=units,
            allocator="fixed",
            max_unit_weight=Decimal("0.4"),
        )
        weights = Allocator(cfg).weights()
        assert all(value <= Decimal("0.4") for value in weights.values())
        assert abs(_sum(weights) - Decimal("1")) < Decimal("1e-20")
        assert weights["a:BTC-USDT"] == Decimal("0.4")
        assert weights["b:ETH-USDT"] == weights["c:SOL-USDT"]

    def test_infeasible_cap_rejected(self) -> None:
        cfg = _config(max_unit_weight=Decimal("0.3"))
        with pytest.raises(AllocationError, match="infeasible"):
            Allocator(cfg).weights()

    def test_negative_equity_rejected(self) -> None:
        with pytest.raises(AllocationError, match="equity"):
            allocate(Decimal("-1"), _config())


class TestVolatility:
    def test_flat_prices_give_zero_vol(self) -> None:
        assert annualized_volatility([100, 100, 100, 100]) == 0

    def test_volatile_series_gives_positive_vol(self) -> None:
        vol = annualized_volatility([100, 110, 95, 120, 105], "1D")
        assert vol > 0

    @pytest.mark.parametrize(
        "closes,bar_size",
        [([100, 101], "1H"), ([100, 0, 101], "1H"), ([100, 101, 102], "3H")],
    )
    def test_invalid_inputs_rejected(
        self, closes: list[int], bar_size: str
    ) -> None:
        with pytest.raises(AllocationError):
            annualized_volatility(closes, bar_size)


class TestSingleStrategyCompatibility:
    """M6-13：不配组合时，Settings 默认值与旧行为完全一致。"""

    def test_settings_without_portfolio_section_keeps_defaults(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[okx]\napi_key = "k"\nsecret = "s"\npassphrase = "p"\n',
            encoding="utf-8",
        )
        settings = Settings.load(path)
        assert settings.portfolio.enabled is False
        assert settings.portfolio.config_dir == Path("./config/portfolios")
        # 既有引擎配置不受影响
        assert settings.engine.strategy == "sma_cross"
        assert settings.engine.multi_binding is True

    def test_settings_can_enable_portfolio(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[okx]\napi_key = "k"\nsecret = "s"\npassphrase = "p"\n'
            '[portfolio]\nenabled = true\nactive = "core"\n',
            encoding="utf-8",
        )
        settings = Settings.load(path)
        assert settings.portfolio.enabled is True
        assert settings.portfolio.active == "core"
