"""M5-13：回测/实盘共用因子提供器——Context.factors 与开关。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.strategy import Strategy
from quant.factors.provider import factor_series_for
from quant.foundation.config import OKXSettings, Settings, StorageSettings
from quant.models import Bar
from quant.storage.bar_storage import BarStorage

HOUR = 3_600_000
START = 1_699_000_000_000
INST = "BTC-USDT"


def _bar(ts: int, price: str = "100") -> Bar:
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


def _bars(count: int = 5) -> list[Bar]:
    return [_bar(START + i * HOUR) for i in range(count)]


class _Spy(Strategy):
    """记录每根 bar 看到的 factors 快照。"""

    def __init__(self) -> None:
        self.seen: list[dict[str, list[Decimal | None]]] = []

    @property
    def strategy_id(self) -> str:
        return "spy"

    def on_init(self, ctx) -> None:
        pass

    def on_bar(self, bar, ctx):
        self.seen.append({key: list(value) for key, value in ctx.factors.items()})
        return None


def _series(count: int = 5) -> dict[int, Decimal]:
    return {
        START + i * HOUR: Decimal(i) / Decimal("10") for i in range(count)
    }


class TestBacktestFactors:
    def test_factor_series_exposed_and_window_aligned(self) -> None:
        strategy = _Spy()

        Backtest(
            strategy,
            Matcher(),
            window_size=3,
            factor_series={"momentum": _series()},
        ).run(_bars())

        assert all(set(snapshot) == {"momentum"} for snapshot in strategy.seen)
        # 窗口 3：长度依次 1,2,3,3,3；最后值对应当前 bar
        assert [len(snapshot["momentum"]) for snapshot in strategy.seen] == [
            1, 2, 3, 3, 3,
        ]
        assert strategy.seen[-1]["momentum"][-1] == Decimal("0.4")

    def test_without_factor_series_keeps_empty(self) -> None:
        strategy = _Spy()

        Backtest(strategy, Matcher()).run(_bars())

        assert all(snapshot == {} for snapshot in strategy.seen)

    def test_metrics_identical_when_strategy_ignores_factors(self) -> None:
        plain = Backtest(_Spy(), Matcher(), window_size=3).run(_bars())
        with_factors = Backtest(
            _Spy(),
            Matcher(),
            window_size=3,
            factor_series={"momentum": _series()},
        ).run(_bars())

        assert plain.metrics == with_factors.metrics


class TestFactorSeriesFor:
    def _settings(self, tmp_path: Path, **factors) -> Settings:
        return Settings(
            okx=OKXSettings(
                api_key="k", secret="s", passphrase="p", demo=True
            ),
            storage=StorageSettings(data_dir=tmp_path / "data"),
            factors=factors,
        )

    def test_disabled_returns_empty(self, tmp_path) -> None:
        settings = self._settings(tmp_path)
        BarStorage(settings.storage.data_dir).save_bars(
            [_bar(START + i * HOUR) for i in range(40)]
        )

        assert factor_series_for(
            settings, INST, "1H", START, START + 39 * HOUR
        ) == {}

    def test_enabled_returns_values(self, tmp_path) -> None:
        settings = self._settings(
            tmp_path, enabled=True, ids=["momentum_20"]
        )
        BarStorage(settings.storage.data_dir).save_bars(
            [_bar(START + i * HOUR, str(100 + i)) for i in range(40)]
        )

        series = factor_series_for(
            settings, INST, "1H", START, START + 39 * HOUR
        )

        assert set(series) == {"momentum_20"}
        # 前 20 根预热无值，之后有点值
        assert series["momentum_20"]
        assert len(series["momentum_20"]) == 20
