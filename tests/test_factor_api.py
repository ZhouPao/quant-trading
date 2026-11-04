"""M5-15 后端：因子列表 / 值 / 评价 / 相关性。"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quant.dashboard.factors import (
    FactorError,
    FactorNotFound,
    FactorService,
    parse_params,
)
from quant.foundation.config import OKXSettings, Settings, StorageSettings
from quant.models import Bar
from quant.storage.bar_storage import BarStorage

HOUR = 3_600_000
START = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
INSTS = ("BTC-USDT", "ETH-USDT", "SOL-USDT")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage=StorageSettings(data_dir=tmp_path / "data"),
        state={"db_path": tmp_path / "data" / "state" / "quant.db"},
        engine={"inst": "BTC-USDT", "bar": "1H"},
    )


def _bars(inst: str, count: int = 40) -> list[Bar]:
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
                inst_id=inst,
                bar_size="1H",
            )
        )
    return result


def _service(tmp_path: Path) -> FactorService:
    settings = _settings(tmp_path)
    storage = BarStorage(settings.storage.data_dir)
    for inst in INSTS:
        storage.save_bars(_bars(inst))
    return FactorService(settings)


class TestFactorMeta:
    """M5-15 首屏：默认区间必须来自真实数据覆盖范围。"""

    def _seed(self, tmp_path, insts=INSTS) -> FactorService:
        settings = _settings(tmp_path)
        storage = BarStorage(settings.storage.data_dir)
        for inst in insts:
            storage.save_bars(_bars(inst, 40))
        return FactorService(settings)

    def test_reports_insts_bars_and_range(self, tmp_path) -> None:
        data = self._seed(tmp_path).meta()

        assert data["insts"] == sorted(INSTS)
        assert data["bars"] == ["1H"]
        assert data["count"] == 40
        assert data["first_ts"] == START
        assert data["last_ts"] == START + 39 * HOUR
        assert data["reason"] is None

    def test_empty_dir_degrades(self, tmp_path) -> None:
        data = FactorService(_settings(tmp_path)).meta()

        assert data["insts"] == []
        assert data["count"] == 0
        assert data["last_ts"] is None
        assert data["reason"]

    def test_selected_inst_and_bar(self, tmp_path) -> None:
        service = self._seed(tmp_path)

        data = service.meta(inst="ETH-USDT", bar="1H")
        assert data["inst"] == "ETH-USDT"
        assert data["count"] == 40

        missing = service.meta(inst="ETH-USDT", bar="4H")
        assert missing["count"] == 0
        assert missing["reason"]


class TestList:
    def test_lists_builtin_factors(self, tmp_path) -> None:
        rows = _service(tmp_path).list()

        ids = {row["factor_id"] for row in rows}
        assert {"momentum_20", "volatility_20"} <= ids
        momentum = next(row for row in rows if row["factor_id"] == "momentum_20")
        assert momentum["category"] == "momentum"
        assert momentum["lookback"] == 20


class TestValues:
    def test_returns_series_with_warmup_gap(self, tmp_path) -> None:
        data = _service(tmp_path).values(
            "momentum_20", "BTC-USDT", "1H", "2026-09-01", "2026-09-01"
        )

        values = data["values"]
        # parse_range 的结束是次日 00:00，load_bars 闭区间 → 24 根 + 端点 1 根
        assert len(values) == 25
        # 前 20 根预热，值应为 None
        assert all(item["value"] is None for item in values[:20])
        assert values[-1]["value"] is not None

    def test_unknown_factor_is_not_found(self, tmp_path) -> None:
        with pytest.raises(FactorNotFound):
            _service(tmp_path).values(
                "nope", "BTC-USDT", "1H", "2026-09-01", "2026-09-02"
            )

    def test_bad_date_is_error(self, tmp_path) -> None:
        with pytest.raises(FactorError):
            _service(tmp_path).values(
                "momentum_20", "BTC-USDT", "1H", "2026/09/01", "2026-09-02"
            )


class TestEvaluate:
    def test_returns_metrics_and_decay(self, tmp_path) -> None:
        data = _service(tmp_path).evaluate(
            "momentum_20", ",".join(INSTS), "1H", "2026-09-01", "2026-09-03"
        )

        assert data["factor_id"] == "momentum_20"
        assert data["insts"] == list(INSTS)
        assert len(data["decay"]) == 5
        assert data["ic_mean"] is not None
        assert data["turnover"] is not None
        assert len(data["quantile_returns"][0]["values"]) == 5

    def test_too_many_insts_rejected(self, tmp_path) -> None:
        many = ",".join(f"I{i}" for i in range(11))

        with pytest.raises(FactorError, match="品种"):
            _service(tmp_path).evaluate(
                "momentum_20", many, "1H", "2026-09-01", "2026-09-02"
            )


class TestCorrelations:
    def test_diagonal_correlation_is_one(self, tmp_path) -> None:
        data = _service(tmp_path).correlations(
            "momentum_20", "BTC-USDT", "1H", "2026-09-01", "2026-09-02"
        )

        assert data["matrix"][0]["corr"] == pytest.approx(1.0)
        assert data["redundant"] == []

    def test_unknown_factor_is_not_found(self, tmp_path) -> None:
        with pytest.raises(FactorNotFound):
            _service(tmp_path).correlations(
                "nope", "BTC-USDT", "1H", "2026-09-01", "2026-09-02"
            )


class TestCorrelationResponse:
    """看板响应必须是完整对称矩阵，热力图不能只画半张。"""

    def test_full_symmetric_matrix(self, tmp_path) -> None:
        data = _service(tmp_path).correlations(
            "momentum_20,volatility_20",
            "BTC-USDT",
            "1H",
            "2026-09-01",
            "2026-09-02",
        )

        by_pair = {
            (item["a"], item["b"]): item["corr"] for item in data["matrix"]
        }
        assert set(by_pair) == {
            ("momentum_20", "momentum_20"),
            ("momentum_20", "volatility_20"),
            ("volatility_20", "momentum_20"),
            ("volatility_20", "volatility_20"),
        }
        assert (
            by_pair[("momentum_20", "volatility_20")]
            == by_pair[("volatility_20", "momentum_20")]
        )


class TestParseParams:
    def test_empty_returns_none(self) -> None:
        assert parse_params(None) is None
        assert parse_params("  ") is None

    def test_bad_json_rejected(self) -> None:
        with pytest.raises(FactorError, match="JSON"):
            parse_params("{bad")

    def test_non_object_rejected(self) -> None:
        with pytest.raises(FactorError, match="对象"):
            parse_params("[1, 2]")
