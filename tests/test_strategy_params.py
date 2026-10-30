"""策略参数单一真相源测试（MS4-C §3.2）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.state.sqlite_store import SQLiteStore
from quant.strategies.params import (
    DB,
    REQUEST,
    TOML,
    ParamError,
    coerce_value,
    format_value,
    param_spec_payload,
    resolve_params,
    resolve_strategy,
)
from quant.strategies.python.rsi import RsiStrategy
from quant.strategies.python.sma_cross import SmaCrossStrategy

SMA_TOML = (
    "[strategy]\n"
    'strategy_id = "sma_cross"\n'
    'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n\n'
    "[params]\nfast_len = 5\nslow_len = 20\ncommission_rate = 0.0005\n"
)


@pytest.fixture
def config_dir(tmp_path):
    directory = tmp_path / "strategies"
    directory.mkdir()
    (directory / "sma_cross.toml").write_text(SMA_TOML)
    return directory


@pytest.fixture
def store(tmp_path):
    item = SQLiteStore(tmp_path / "quant.db")
    yield item
    item.close()


class TestResolveOrder:
    def test_toml_only(self, config_dir, store) -> None:
        params, sources = resolve_params("sma_cross", config_dir, store)

        assert params["fast_len"] == 5
        assert params["slow_len"] == 20
        assert sources == {
            "fast_len": TOML,
            "slow_len": TOML,
            "commission_rate": TOML,
        }

    def test_db_overrides_toml(self, config_dir, store) -> None:
        store.set_strategy_param("sma_cross", "fast_len", "8", 1000)

        params, sources = resolve_params("sma_cross", config_dir, store)

        assert params["fast_len"] == 8
        assert sources["fast_len"] == DB
        assert sources["slow_len"] == TOML

    def test_request_overrides_db(self, config_dir, store) -> None:
        store.set_strategy_param("sma_cross", "fast_len", "8", 1000)

        params, sources = resolve_params(
            "sma_cross", config_dir, store, overrides={"fast_len": 12}
        )

        assert params["fast_len"] == 12
        assert sources["fast_len"] == REQUEST

    def test_request_does_not_write_back(self, config_dir, store) -> None:
        resolve_params("sma_cross", config_dir, store, overrides={"fast_len": 12})

        assert store.get_strategy_params("sma_cross") == []

    def test_rollback_switch_ignores_db(self, config_dir, store) -> None:
        """params.resolve_from_db=false 是回滚开关：完全按 toml。"""
        store.set_strategy_param("sma_cross", "fast_len", "8", 1000)

        params, sources = resolve_params(
            "sma_cross", config_dir, store, resolve_from_db=False
        )

        assert params["fast_len"] == 5
        assert sources["fast_len"] == TOML

    def test_unknown_db_param_is_ignored(self, config_dir, store) -> None:
        """未在 PARAM_SPEC 声明的参数不允许在线覆盖（如 commission_rate）。"""
        store.set_strategy_param("sma_cross", "commission_rate", "0", 1000)

        params, _sources = resolve_params("sma_cross", config_dir, store)

        assert params["commission_rate"] == 0.0005

    def test_invalid_db_value_falls_back_to_toml(self, config_dir, store) -> None:
        """手工改库写脏值：忽略该条并回落 toml，而不是让引擎起不来。"""
        store.set_strategy_param("sma_cross", "fast_len", "abc", 1000)

        params, sources = resolve_params("sma_cross", config_dir, store)

        assert params["fast_len"] == 5
        assert sources["fast_len"] == TOML

    def test_out_of_range_db_value_ignored(self, config_dir, store) -> None:
        store.set_strategy_param("sma_cross", "fast_len", "9999", 1000)

        params, _sources = resolve_params("sma_cross", config_dir, store)

        assert params["fast_len"] == 5

    def test_invalid_request_override_raises(self, config_dir, store) -> None:
        with pytest.raises(ParamError):
            resolve_params(
                "sma_cross", config_dir, store, overrides={"fast_len": "abc"}
            )

    def test_returns_strategy_class(self, config_dir, store) -> None:
        cls, params, _sources = resolve_strategy("sma_cross", config_dir, store)

        assert cls is SmaCrossStrategy
        assert params["slow_len"] == 20


class TestCoerce:
    def test_int_and_range(self) -> None:
        spec = SmaCrossStrategy.PARAM_SPEC["fast_len"]

        assert coerce_value(spec, "7") == 7
        with pytest.raises(ParamError):
            coerce_value(spec, "1")
        with pytest.raises(ParamError):
            coerce_value(spec, "1.5")

    def test_bool_variants(self) -> None:
        spec = SmaCrossStrategy.PARAM_SPEC["ema_filter"]

        assert coerce_value(spec, "true") is True
        assert coerce_value(spec, "FALSE") is False
        assert coerce_value(spec, "1") is True
        assert coerce_value(spec, True) is True
        with pytest.raises(ParamError):
            coerce_value(spec, "maybe")

    def test_int_rejects_bool(self) -> None:
        spec = SmaCrossStrategy.PARAM_SPEC["fast_len"]

        with pytest.raises(ParamError):
            coerce_value(spec, True)

    def test_decimal(self) -> None:
        spec = RsiStrategy.PARAM_SPEC["oversold"]

        assert coerce_value(spec, "25") == Decimal("25")
        with pytest.raises(ParamError):
            coerce_value(spec, "101")

    def test_float(self) -> None:
        spec = SmaCrossStrategy.PARAM_SPEC["ema_band_atr"]

        assert coerce_value(spec, "0.5") == 0.5
        with pytest.raises(ParamError):
            coerce_value(spec, "-1")


class TestMeta:
    def test_format_value(self) -> None:
        bool_spec = SmaCrossStrategy.PARAM_SPEC["ema_filter"]

        assert format_value(bool_spec, True) == "true"
        assert format_value(bool_spec, False) == "false"
        assert format_value(SmaCrossStrategy.PARAM_SPEC["fast_len"], 10) == "10"

    def test_param_spec_payload_shape(self) -> None:
        payload = param_spec_payload(SmaCrossStrategy)
        by_name = {item["name"]: item for item in payload}

        assert list(by_name) == list(SmaCrossStrategy.PARAM_SPEC)
        assert by_name["fast_len"]["min"] == "2"
        assert by_name["ema_filter"]["type"] == "bool"
        assert by_name["ema_filter"]["default"] == "false"
        assert by_name["fast_len"]["description"]

    def test_rsi_spec_matches_on_init_defaults(self) -> None:
        """PARAM_SPEC 的默认值必须与 on_init 的 ctx.params.get 一致。"""
        spec = RsiStrategy.PARAM_SPEC

        assert spec["rsi_period"].default == 14
        assert spec["oversold"].default == 30
        assert spec["overbought"].default == 70
