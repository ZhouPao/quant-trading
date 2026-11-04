"""M3-7 research export：K线 / 本地成交 / 回测结果三种导出。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.backtest.order import Fill
from quant.cli.research import main as research_main
from quant.foundation.config import OKXSettings, Settings, StorageSettings
from quant.models import Bar
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage

HOUR = 3_600_000
START = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
INST = "BTC-USDT"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage=StorageSettings(data_dir=tmp_path / "data"),
        state={"db_path": tmp_path / "data" / "state" / "quant.db"},
    )


def _bar(ts: int) -> Bar:
    price = Decimal("100")
    return Bar(
        ts=ts,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        vol=Decimal("1"),
        vol_quote=price,
        confirm=1,
        inst_id=INST,
        bar_size="1H",
    )


def _args(tmp_path: Path, **overrides) -> SimpleNamespace:
    payload = {
        "research_command": "export",
        "inst": INST,
        "bar": "1H",
        "start": "2026-09-01",
        "end": "2026-09-02",
        "out": str(tmp_path / "out"),
        "run_id": None,
        "config": "config/config.toml",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    BarStorage(settings.storage.data_dir).save_bars(
        [_bar(START), _bar(START + HOUR)]
    )
    monkeypatch.setattr(
        "quant.cli.research.Settings.load", lambda _path: settings
    )
    return settings


class TestResearchExport:
    def test_exports_bars_and_local_fills(self, env, tmp_path) -> None:
        store = SQLiteStore(env.state.db_path)
        try:
            store.save_fill(
                Fill(
                    order_id="o1",
                    inst_id=INST,
                    side="buy",
                    qty=Decimal("1"),
                    price=Decimal("100"),
                    fee=Decimal("0.1"),
                    ts=START + HOUR,
                )
            )
            store.save_fill(
                Fill(
                    order_id="o2",
                    inst_id="ETH-USDT",
                    side="buy",
                    qty=Decimal("1"),
                    price=Decimal("10"),
                    fee=Decimal("0.01"),
                    ts=START + HOUR,
                )
            )
        finally:
            store.close()

        code = research_main(_args(tmp_path))

        assert code == 0
        out = tmp_path / "out"
        bars = pd.read_parquet(out / "bars.parquet")
        trades = pd.read_parquet(out / "trades.parquet")
        manifest = json.loads((out / "manifest.json").read_text("utf-8"))
        assert len(bars) == 2
        # 只导出该品种，ETH 的成交不应混进来
        assert list(trades["inst_id"]) == [INST]
        assert manifest["bars"] == 2
        assert manifest["run_id"] is None
        # files 只列数据文件（manifest 本身不列）
        assert set(manifest["files"]) == {"bars.parquet", "trades.parquet"}

    def test_exports_run_artifacts_and_metrics(self, env, tmp_path) -> None:
        out = tmp_path / "out"
        source = tmp_path / "run-trades.parquet"
        pd.DataFrame(
            [
                {
                    "ts": START,
                    "side": "sell",
                    "qty": "1",
                    "price": "110",
                    "fee": "0.1",
                }
            ]
        ).to_parquet(source, index=False)

        store = SQLiteStore(env.state.db_path)
        try:
            store.create_backtest_run(
                "r1",
                strategy_id="sma_cross",
                inst_id=INST,
                bar="1H",
                start_ts=START,
                end_ts=START + 24 * HOUR,
                params='{"fast_len": 10}',
                kind="backtest",
                status="done",
                created_at=START,
            )
            store.save_backtest_metrics("r1", {"sharpe": "1.5"})
            store.add_backtest_artifact(
                "r1", "trades", str(source), source.stat().st_size, START
            )
        finally:
            store.close()

        code = research_main(_args(tmp_path, run_id="r1"))

        assert code == 0
        detail = json.loads((out / "backtest.json").read_text("utf-8"))
        assert detail["run"]["run_id"] == "r1"
        assert detail["metrics"]["sharpe"] == "1.5"
        assert (out / "trades.parquet").is_file()
        manifest = json.loads((out / "manifest.json").read_text("utf-8"))
        assert manifest["run_id"] == "r1"

    def test_missing_bars_fails(self, env, tmp_path) -> None:
        code = research_main(
            _args(tmp_path, start="2025-01-01", end="2025-01-02")
        )

        assert code == 1
        assert not (tmp_path / "out" / "bars.parquet").exists()

    def test_bad_date_fails(self, env, tmp_path) -> None:
        code = research_main(_args(tmp_path, start="2026/09/01"))

        assert code == 1
