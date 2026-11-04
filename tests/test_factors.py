"""M5-1~4：注册、param_hash、示例因子、缓存往返、Context.factors。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.context import Context
from quant.backtest.position import Position
from quant.factors import (
    FactorStore,
    factor,
    get_factor,
    list_factors,
    param_hash,
)
from quant.factors.registry import clear_registry
from quant.models import Bar

HOUR = 3_600_000
INST = "BTC-USDT"


def _bar(ts: int, price: str) -> Bar:
    value = Decimal(price)
    return Bar(
        ts=ts,
        open=value,
        high=value,
        low=value,
        close=value,
        vol=Decimal("1"),
        vol_quote=value,
        confirm=1,
        inst_id=INST,
        bar_size="1H",
    )


class TestRegistry:
    def test_builtin_factors_registered(self) -> None:
        ids = {item.factor_id for item in list_factors()}

        assert {"momentum_20", "volatility_20"} <= ids
        assert get_factor("momentum_20").category == "momentum"
        assert get_factor("momentum_20").lookback == 20

    def test_duplicate_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="already registered"):

            @factor(
                factor_id="momentum_20",
                name="dup",
                category="momentum",
                scope="time_series",
                lookback=1,
            )
            def _dup(bars, params):
                return [None] * len(bars)

    def test_alias_lookup(self) -> None:
        @factor(
            factor_id="alias_momentum",
            name="别名动量",
            category="momentum",
            scope="time_series",
            lookback=1,
            aliases=("alias_old",),
        )
        def _impl(bars, params):
            return [None] * len(bars)

        assert get_factor("alias_old").factor_id == "alias_momentum"

    def test_unknown_raises(self) -> None:
        with pytest.raises(KeyError):
            get_factor("nope")

    def test_bad_category_rejected(self) -> None:
        with pytest.raises(ValueError, match="category"):

            @factor(
                factor_id="bad_category",
                name="bad",
                category="not_a_category",
                scope="time_series",
                lookback=1,
            )
            def _bad(bars, params):
                return [None] * len(bars)

    def test_clear_registry_removes_factors(self) -> None:
        """注册表可清空（测试用）；清空后取内置因子应失败。"""
        try:
            clear_registry()
            with pytest.raises(KeyError):
                get_factor("momentum_20")
        finally:
            # 重新导入 builtin 恢复注册（避免污染后续测试）
            import importlib

            import quant.factors.builtin as builtin

            importlib.reload(builtin)


class TestParamHash:
    def test_stable_regardless_of_order(self) -> None:
        assert param_hash({"a": 1, "b": "x"}) == param_hash({"b": "x", "a": 1})

    def test_different_params_differ(self) -> None:
        assert param_hash({"a": 1}) != param_hash({"a": 2})

    def test_decimal_and_text_are_distinct_from_float(self) -> None:
        # Decimal 统一转文本，避免 float 精度改变哈希
        assert param_hash({"x": Decimal("0.1")}) == param_hash({"x": "0.1"})


class TestBuiltinFactors:
    def test_momentum_last_value(self) -> None:
        prices = [Decimal("100") + i for i in range(21)]
        bars = [_bar(i * HOUR, str(price)) for i, price in enumerate(prices)]

        values = get_factor("momentum_20").compute(bars, {})

        assert len(values) == len(bars)
        assert values[:20] == [None] * 20
        assert values[20] == bars[20].close / bars[0].close - 1

    def test_volatility_zero_for_flat_prices(self) -> None:
        bars = [_bar(i * HOUR, "100") for i in range(21)]

        values = get_factor("volatility_20").compute(bars, {})

        assert values[:20] == [None] * 20
        assert values[20] == Decimal("0")


class TestFactorStore:
    def test_roundtrip_keeps_none_and_order(self, tmp_path) -> None:
        store = FactorStore(tmp_path / "data")
        rows = [
            (1_699_000_000_000, Decimal("0.1")),
            (1_699_003_600_000, None),
        ]

        store.save("momentum_20", {}, INST, "1H", rows)
        loaded = store.load(
            "momentum_20", {}, INST, "1H", 0, 1_700_000_000_000
        )

        assert loaded == rows

    def test_params_are_separated(self, tmp_path) -> None:
        store = FactorStore(tmp_path / "data")
        store.save("demo", {"a": 1}, INST, "1H", [(1_699_000_000_000, Decimal("1"))])

        assert store.load("demo", {"a": 2}, INST, "1H", 0, 1_700_000_000_000) == []

    def test_merge_keeps_history(self, tmp_path) -> None:
        store = FactorStore(tmp_path / "data")
        store.save("demo", {}, INST, "1H", [(1_699_000_000_000, Decimal("1"))])
        store.save("demo", {}, INST, "1H", [(1_699_003_600_000, Decimal("2"))])

        loaded = store.load("demo", {}, INST, "1H", 0, 1_700_000_000_000)

        assert [value for _, value in loaded] == [Decimal("1"), Decimal("2")]


class TestContextFactors:
    def test_default_empty(self) -> None:
        ctx = Context(position=Position(inst_id=INST))

        assert ctx.factors == {}

    def test_can_hold_factor_series(self) -> None:
        ctx = Context(
            position=Position(inst_id=INST),
            factors={"momentum_20": [None, Decimal("0.1")]},
        )

        assert ctx.factors["momentum_20"][1] == Decimal("0.1")
