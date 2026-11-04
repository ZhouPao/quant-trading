"""M10-7 doctor 测试：健康/缺库/缺凭证/数据过期（Redis 检查跳过）。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quant.cli.doctor import run_doctor
from quant.foundation.config import (
    BacktestSettings,
    OKXSettings,
    Settings,
    StorageSettings,
)
from quant.models import Bar
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage

NOW = 1_700_000_000_000
HOUR = 3_600_000
INST = "BTC-USDT"
BAR = "1H"


def _bar(ts: int) -> Bar:
    price = Decimal("100")
    return Bar(
        ts=ts,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        vol=Decimal("1"),
        vol_quote=Decimal("100"),
        confirm=1,
        inst_id=INST,
        bar_size=BAR,
    )


def _settings(
    tmp_path: Path,
    *,
    with_db: bool = True,
    with_bars: bool = True,
    bar_age_ms: int = HOUR,
    credentials: tuple[str, str, str] = ("k", "s", "p"),
) -> Settings:
    data_dir = tmp_path / "data"
    settings = Settings(
        okx=OKXSettings(
            api_key=credentials[0],
            secret=credentials[1],
            passphrase=credentials[2],
            demo=True,
        ),
        storage=StorageSettings(data_dir=data_dir),
        state={"db_path": data_dir / "state" / "quant.db"},
        backtest=BacktestSettings(artifacts_dir=data_dir / "backtest"),
        engine={"inst": INST, "bar": BAR},
    )
    if with_db:
        SQLiteStore(settings.state.db_path).close()
    if with_bars:
        BarStorage(data_dir).save_bars([_bar(NOW - bar_age_ms)])
    return settings


def _by_name(report) -> dict:
    return {item.name: item for item in report.checks}


class TestRunDoctor:
    def test_healthy_report(self, tmp_path) -> None:
        settings = _settings(tmp_path)

        report = run_doctor(settings, now_ms=NOW, skip_redis=True)

        checks = _by_name(report)
        assert checks["sqlite"].status == "ok"
        assert checks["okx"].status == "ok"
        assert checks["data"].status == "ok"
        assert checks["artifacts"].status == "ok"
        assert report.ok is True

    def test_missing_db_is_fail(self, tmp_path) -> None:
        settings = _settings(tmp_path, with_db=False)

        report = run_doctor(settings, now_ms=NOW, skip_redis=True)

        assert _by_name(report)["sqlite"].status == "fail"
        assert report.ok is False

    def test_missing_credentials_is_fail(self, tmp_path) -> None:
        settings = _settings(tmp_path, credentials=("", "", ""))

        report = run_doctor(settings, now_ms=NOW, skip_redis=True)

        check = _by_name(report)["okx"]
        assert check.status == "fail"
        assert "api_key" in check.detail

    def test_stale_bars_warns(self, tmp_path) -> None:
        settings = _settings(tmp_path, bar_age_ms=3 * 24 * HOUR)

        report = run_doctor(settings, now_ms=NOW, skip_redis=True)

        assert _by_name(report)["data"].status == "warn"
        # warn 不影响可用性
        assert report.ok is True
        assert report.strict_ok is False

    def test_no_bars_warns(self, tmp_path) -> None:
        settings = _settings(tmp_path, with_bars=False)

        report = run_doctor(settings, now_ms=NOW, skip_redis=True)

        assert _by_name(report)["data"].status == "warn"

    def test_live_mode_is_warn(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        settings.okx.demo = False

        report = run_doctor(settings, now_ms=NOW, skip_redis=True)

        assert _by_name(report)["okx"].status == "warn"

    def test_smtp_unconfigured_is_warn(self, tmp_path) -> None:
        settings = _settings(tmp_path)

        report = run_doctor(settings, now_ms=NOW, skip_redis=True)

        assert _by_name(report)["smtp"].status == "warn"
