"""P0-2 上线检查清单：四项检查的三态与降级行为。"""
from __future__ import annotations

import time
from pathlib import Path

from quant.dashboard.readiness import ReadinessView
from quant.foundation.config import OKXSettings, Settings
from quant.strategies.consistency import DriftItem

_MS_PER_DAY = 86_400_000


def _settings(tmp_path: Path) -> Settings:
    cfg = tmp_path / "strategies"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "sma_cross.toml").write_text(
        '[strategy]\nstrategy_id = "sma_cross"\n'
        'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n',
        encoding="utf-8",
    )
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p"),
        engine={"config_dir": cfg},
    )


class _FakeStore:
    def __init__(self, *, pine=None, wf_total=0, earliest=None) -> None:
        self._pine = pine
        self._wf_total = wf_total
        self._earliest = earliest

    def worst_pine_deviation(self, strategy_id, params_text, *_a, **_kw):
        return self._pine

    def list_backtest_runs(self, *_a, **_kw):
        return [], self._wf_total

    def earliest_equity_ts(self):
        return self._earliest


def _view(tmp_path: Path, store, monkeypatch, consistency_items=()):
    monkeypatch.setattr(
        "quant.dashboard.readiness.check_consistency",
        lambda: list(consistency_items),
    )
    monkeypatch.setattr(
        "quant.dashboard.readiness.resolve_params",
        lambda *a, **k: ({"fast_len": 10}, {}),
    )
    return ReadinessView(store, _settings(tmp_path))


def _by_key(result):
    return {item["key"]: item for item in result["items"]}


class TestReadiness:
    def test_all_pass_is_ready(self, tmp_path, monkeypatch) -> None:
        store = _FakeStore(
            pine={"run_id": "r123456789", "deviation": "0.02"},
            wf_total=2,
            earliest=int(time.time() * 1000) - 30 * _MS_PER_DAY,
        )
        result = _view(tmp_path, store, monkeypatch).snapshot("sma_cross")

        assert result["ready"] is True
        assert result["degraded"] == []
        items = _by_key(result)
        assert all(item["status"] == "pass" for item in items.values())

    def test_pine_not_covered_is_unknown(self, tmp_path, monkeypatch) -> None:
        store = _FakeStore(earliest=int(time.time() * 1000) - 30 * _MS_PER_DAY)
        result = _view(tmp_path, store, monkeypatch).snapshot("sma_cross")

        assert result["ready"] is False
        assert _by_key(result)["pine"]["status"] == "unknown"
        assert "未做过" in _by_key(result)["pine"]["detail"]

    def test_pine_over_limit_is_fail(self, tmp_path, monkeypatch) -> None:
        store = _FakeStore(
            pine={"run_id": "r123456789", "deviation": "0.08"},
            earliest=int(time.time() * 1000) - 30 * _MS_PER_DAY,
        )
        result = _view(tmp_path, store, monkeypatch).snapshot("sma_cross")

        assert _by_key(result)["pine"]["status"] == "fail"
        assert "8.00%" in _by_key(result)["pine"]["detail"]

    def test_unregistered_drift_is_fail(self, tmp_path, monkeypatch) -> None:
        drift = DriftItem(
            strategy="sma_cross",
            kind="params",
            key="fast_len",
            detail="pine=5 python=10",
            known=False,
        )
        store = _FakeStore(
            earliest=int(time.time() * 1000) - 30 * _MS_PER_DAY
        )
        result = _view(
            tmp_path, store, monkeypatch, consistency_items=[drift]
        ).snapshot("sma_cross")

        assert _by_key(result)["consistency"]["status"] == "fail"
        # 其它策略的差异不冤枉本策略
        other = DriftItem(
            strategy="rsi", kind="params", key="k", detail="", known=False
        )
        clean = _view(
            tmp_path, store, monkeypatch, consistency_items=[other, drift]
        ).snapshot("rsi")
        assert _by_key(clean)["consistency"]["status"] == "fail"
        clean_for_sma = _view(
            tmp_path, store, monkeypatch, consistency_items=[other]
        ).snapshot("sma_cross")
        assert _by_key(clean_for_sma)["consistency"]["status"] == "pass"

    def test_short_paper_history_is_fail(self, tmp_path, monkeypatch) -> None:
        store = _FakeStore(
            pine={"run_id": "r123", "deviation": "0.01"},
            wf_total=1,
            earliest=int(time.time() * 1000) - 3 * _MS_PER_DAY,
        )
        result = _view(tmp_path, store, monkeypatch).snapshot("sma_cross")

        assert _by_key(result)["paper"]["status"] == "fail"
        assert "≥ 14 天" in _by_key(result)["paper"]["detail"]

    def test_no_equity_snapshot_is_unknown(self, tmp_path, monkeypatch) -> None:
        store = _FakeStore(
            pine={"run_id": "r123", "deviation": "0.01"},
            wf_total=1,
        )
        result = _view(tmp_path, store, monkeypatch).snapshot("sma_cross")

        assert _by_key(result)["paper"]["status"] == "unknown"

    def test_query_failure_degrades_explicitly(
        self, tmp_path, monkeypatch
    ) -> None:
        class _Broken:
            def worst_pine_deviation(self, *a, **kw):
                raise RuntimeError("sqlite down")

            def list_backtest_runs(self, *a, **kw):
                raise RuntimeError("sqlite down")

            def earliest_equity_ts(self):
                raise RuntimeError("sqlite down")

        result = _view(tmp_path, _Broken(), monkeypatch).snapshot("sma_cross")

        assert result["ready"] is False
        assert {"pine", "walkforward", "equity"} <= set(result["degraded"])
        items = _by_key(result)
        assert items["pine"]["status"] == "unknown"
        assert items["walkforward"]["status"] == "unknown"
        assert items["paper"]["status"] == "unknown"

    def test_default_strategy_from_settings(self, tmp_path, monkeypatch) -> None:
        store = _FakeStore(earliest=None)
        result = _view(tmp_path, store, monkeypatch).snapshot()

        assert result["strategy"] == "sma_cross"
