"""M5-5/12/14：缓存命中、增量回退、缺失/极值、全量=增量。"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from quant.factors import (
    FactorProvider,
    FactorStore,
    factor,
    get_factor,
)
from quant.models import Bar
from quant.storage.bar_storage import BarStorage

HOUR = 3_600_000
START = 1_699_000_000_000
INST = "BTC-USDT"
BAR = "1H"


@factor(
    factor_id="gap_test",
    name="带空洞",
    category="trend",
    scope="time_series",
    lookback=1,
)
def _gap(bars, params):
    return [None if i == 5 else Decimal(i) for i in range(len(bars))]


@factor(
    factor_id="linear_test",
    name="线性",
    category="trend",
    scope="time_series",
    lookback=0,
)
def _linear(bars, params):
    return [Decimal(i) for i in range(len(bars))]


@factor(
    factor_id="scale_test",
    name="比例",
    category="trend",
    scope="time_series",
    lookback=0,
    params={"scale": 1},
)
def _scale(bars, params):
    scale = Decimal(str(params.get("scale", 1)))
    return [Decimal(i) * scale for i in range(len(bars))]


def _bars(count: int = 100) -> list[Bar]:
    result = []
    for i in range(count):
        price = Decimal("100") + i
        result.append(
            Bar(
                ts=START + i * HOUR,
                open=price,
                high=price,
                low=price,
                close=price,
                vol=Decimal("1"),
                vol_quote=price,
                confirm=1,
                inst_id=INST,
                bar_size=BAR,
            )
        )
    return result


def _provider(tmp_path: Path, bars: list[Bar] | None = None):
    bars = bars if bars is not None else _bars()
    storage = BarStorage(tmp_path / "data")
    storage.save_bars(bars)
    return FactorProvider(FactorStore(tmp_path / "data"), storage), bars


class TestCompute:
    def test_cold_compute_matches_direct(self, tmp_path) -> None:
        provider, bars = _provider(tmp_path)

        got = provider.compute(
            "momentum_20", INST, BAR, bars[0].ts, bars[-1].ts
        )
        direct = get_factor("momentum_20").compute(bars, {})

        assert [value for _, value in got] == direct

    def test_second_call_does_not_recompute(self, tmp_path, monkeypatch) -> None:
        provider, bars = _provider(tmp_path)
        spec = get_factor("momentum_20")
        calls: list[int] = []

        def wrapped(call_bars, params):
            calls.append(len(call_bars))
            return spec.compute(call_bars, params)

        monkeypatch.setattr(
            "quant.factors.provider.get_factor",
            lambda _id: replace(spec, compute=wrapped),
        )

        provider.compute("momentum_20", INST, BAR, bars[0].ts, bars[40].ts)
        provider.compute("momentum_20", INST, BAR, bars[0].ts, bars[40].ts)

        assert len(calls) == 1

    def test_incremental_matches_full(self, tmp_path) -> None:
        """M5-14：先算一半再增量，必须与一次全量逐点相等。"""
        bars = _bars()
        full_provider, _ = _provider(tmp_path / "full", bars)
        full = full_provider.compute(
            "momentum_20", INST, BAR, bars[0].ts, bars[-1].ts
        )

        inc_provider, _ = _provider(tmp_path / "inc", bars)
        inc_provider.compute("momentum_20", INST, BAR, bars[0].ts, bars[60].ts)
        incremental = inc_provider.compute(
            "momentum_20", INST, BAR, bars[0].ts, bars[-1].ts
        )

        assert incremental == full

    def test_incremental_only_computes_tail(
        self, tmp_path, monkeypatch
    ) -> None:
        provider, bars = _provider(tmp_path)
        spec = get_factor("momentum_20")
        calls: list[int] = []

        def wrapped(call_bars, params):
            calls.append(len(call_bars))
            return spec.compute(call_bars, params)

        monkeypatch.setattr(
            "quant.factors.provider.get_factor",
            lambda _id: replace(spec, compute=wrapped),
        )

        provider.compute("momentum_20", INST, BAR, bars[0].ts, bars[60].ts)
        provider.compute("momentum_20", INST, BAR, bars[0].ts, bars[-1].ts)

        # 第一次只有 61 根可算；第二次从 ts60 回退 20 根起算，不应重算 100 根
        assert calls[0] == 61
        assert calls[1] < 100

    def test_params_are_isolated(self, tmp_path) -> None:
        provider, bars = _provider(tmp_path)

        two = provider.compute(
            "scale_test", INST, BAR, bars[0].ts, bars[9].ts,
            params={"scale": 2},
        )
        three = provider.compute(
            "scale_test", INST, BAR, bars[0].ts, bars[9].ts,
            params={"scale": 3},
        )

        assert [v for _, v in two] != [v for _, v in three]


class TestFillAndClip:
    def test_fill_prev(self, tmp_path) -> None:
        provider, bars = _provider(tmp_path)

        filled = provider.compute(
            "gap_test", INST, BAR, bars[0].ts, bars[9].ts, fill="prev"
        )

        values = dict(filled)
        # i=5 的空洞被前值（i=4）填上
        assert values[bars[5].ts] == Decimal(4)

    def test_fill_skip(self, tmp_path) -> None:
        provider, bars = _provider(tmp_path)

        skipped = provider.compute(
            "gap_test", INST, BAR, bars[0].ts, bars[9].ts, fill="skip"
        )

        assert len(skipped) == 9
        assert bars[5].ts not in dict(skipped)

    def test_clip_quantile(self, tmp_path) -> None:
        provider, bars = _provider(tmp_path)

        clipped = provider.compute(
            "linear_test", INST, BAR, bars[0].ts, bars[-1].ts,
            clip_quantile=0.05,
        )

        values = [v for _, v in clipped]
        # 100 个值里 5%/95% 分位约为 5 与 94：两端被裁剪
        assert values[0] >= Decimal(4)
        assert values[-1] <= Decimal(95)

    def test_invalid_fill_rejected(self, tmp_path) -> None:
        provider, bars = _provider(tmp_path)

        with pytest.raises(ValueError, match="fill mode"):
            provider.compute(
                "gap_test", INST, BAR, bars[0].ts, bars[9].ts, fill="bad"
            )
