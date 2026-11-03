"""数据质量报告测试（MS4-C §9）。

覆盖：
- merge_report 的 open / repaired / 窗口外保持 open
- latest.json 原子写与损坏容错
- QualityReporter.check 读写闭环
- monitor 的 data_quality 告警与按 (inst, bar) 冷却
- quant check-data 命令退出码
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import fakeredis.aioredis
import pytest

from quant.cli.check_data import main as check_main
from quant.foundation.config import (
    MonitorSettings,
    OKXSettings,
    Settings,
    SmtpSettings,
)
from quant.models import Bar
from quant.monitor.alerter import Alert, EmailAlerter
from quant.monitor.monitor import Monitor
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage
from quant.storage.quality import (
    QualityReporter,
    load_report,
    merge_report,
    report_path,
    save_report,
)
from quant.storage.validator import ValidationReport

BAR_MS = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % BAR_MS)
NOW = T0 + 100 * BAR_MS


def _bar(ts: int, *, close: str = "100", confirm: int = 1) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal(close),
        vol=Decimal("1"),
        vol_quote=Decimal("100"),
        confirm=confirm,
        inst_id="BTC-USDT",
        bar_size="1H",
    )


def _report(
    *missing: tuple[int, int],
    invalid: list[tuple[int, str]] | None = None,
) -> ValidationReport:
    """构造一次校验结果：missing 是 (prev_ts, next_ts) 对。"""
    report = ValidationReport()
    for prev_ts, next_ts in missing:
        report.add_missing(prev_ts, next_ts, next_ts - prev_ts, BAR_MS)
    for ts, reason in invalid or []:
        report.add_invalid_ohlc(ts, reason)
    return report


# =====================================================================
# merge_report
# =====================================================================


class TestMergeReport:
    def test_first_gap_is_open(self) -> None:
        report = _report((T0, T0 + 2 * BAR_MS))
        merged = merge_report(
            None,
            report,
            inst_id="BTC-USDT",
            bar_size="1H",
            checked_range=(T0, T0 + 3 * BAR_MS),
            now=NOW,
            present=2,
        )

        assert merged["summary"] == {
            "expected": 3,
            "present": 2,
            "gaps_open": 1,
            "gaps_repaired": 0,
        }
        gap = merged["gaps"][0]
        assert gap["from"] == T0 + BAR_MS
        assert gap["to"] == T0 + BAR_MS
        assert gap["bars"] == 1
        assert gap["status"] == "open"
        assert "repaired_at" not in gap

    def test_still_missing_stays_open(self) -> None:
        prev = merge_report(
            None,
            _report((T0, T0 + 2 * BAR_MS)),
            inst_id="BTC-USDT",
            bar_size="1H",
            checked_range=(T0, T0 + 3 * BAR_MS),
            now=NOW,
            present=2,
        )

        merged = merge_report(
            prev,
            _report((T0, T0 + 3 * BAR_MS)),
            inst_id="BTC-USDT",
            bar_size="1H",
            checked_range=(T0, T0 + 3 * BAR_MS),
            now=NOW + 1,
            present=2,
        )

        gap = merged["gaps"][0]
        assert gap["status"] == "open"
        assert gap["bars"] == 2  # 缺口变大，用本次数据覆盖
        assert merged["summary"]["gaps_repaired"] == 0

    def test_gap_not_seen_again_is_repaired(self) -> None:
        prev = merge_report(
            None,
            _report((T0, T0 + 2 * BAR_MS)),
            inst_id="BTC-USDT",
            bar_size="1H",
            checked_range=(T0, T0 + 3 * BAR_MS),
            now=NOW,
            present=2,
        )

        merged = merge_report(
            prev,
            _report(),
            inst_id="BTC-USDT",
            bar_size="1H",
            checked_range=(T0, T0 + 3 * BAR_MS),
            now=NOW + 1,
            present=3,
        )

        gap = merged["gaps"][0]
        assert gap["status"] == "repaired"
        assert gap["repaired_at"] == NOW + 1
        assert merged["summary"]["gaps_open"] == 0
        assert merged["summary"]["gaps_repaired"] == 1
        assert merged["summary"]["expected"] == 3

    def test_gap_outside_window_keeps_open(self) -> None:
        """窗口外的旧缺口不能因为「本轮没看它」被当成修好。"""
        prev = {
            "inst_id": "BTC-USDT",
            "bar_size": "1H",
            "gaps": [
                {
                    "from": T0,
                    "to": T0,
                    "bars": 1,
                    "status": "open",
                }
            ],
        }

        merged = merge_report(
            prev,
            _report(),
            inst_id="BTC-USDT",
            bar_size="1H",
            checked_range=(T0 + 10 * BAR_MS, T0 + 20 * BAR_MS),
            now=NOW,
            present=11,
        )

        gap = merged["gaps"][0]
        assert gap["status"] == "open"
        assert "repaired_at" not in gap
        assert merged["summary"]["gaps_open"] == 1
        assert merged["summary"]["gaps_repaired"] == 0

    def test_invalid_ohlc_is_carried(self) -> None:
        merged = merge_report(
            None,
            _report(invalid=[(T0, "high<low")]),
            inst_id="BTC-USDT",
            bar_size="1H",
            checked_range=(T0, T0 + BAR_MS),
            now=NOW,
            present=2,
        )

        assert merged["invalid_ohlc"] == [{"ts": T0, "reason": "high<low"}]

    def test_gap_left_edge_filled_updates_instead_of_duplicating(self) -> None:
        """缺口左端被补上一根后 from 位移：按区间相交配对，不再重复累积（P1）。"""
        prev = {
            "gaps": [
                {
                    "from": T0 + BAR_MS,
                    "to": T0 + 2 * BAR_MS,
                    "bars": 2,
                    "status": "open",
                }
            ],
        }
        # 本次只剩右半根：missing (prev=T0+BAR, next=T0+3BAR) → from=to=T0+2BAR
        merged = merge_report(
            prev,
            _report((T0 + BAR_MS, T0 + 3 * BAR_MS)),
            inst_id="BTC-USDT",
            bar_size="1H",
            checked_range=(T0, T0 + 3 * BAR_MS),
            now=NOW + 1,
            present=2,
        )

        opens = [g for g in merged["gaps"] if g["status"] == "open"]
        assert len(opens) == 1
        assert opens[0]["from"] == T0 + 2 * BAR_MS
        assert opens[0]["bars"] == 1
        assert merged["summary"]["gaps_open"] == 1

    def test_one_old_gap_split_into_two_keeps_both(self) -> None:
        """旧缺口中段被填、两端仍缺：两段都要保留，不能丢。"""
        prev = {
            "gaps": [
                {
                    "from": T0 + BAR_MS,
                    "to": T0 + 4 * BAR_MS,
                    "bars": 4,
                    "status": "open",
                }
            ],
        }
        merged = merge_report(
            prev,
            _report((T0, T0 + 2 * BAR_MS), (T0 + 3 * BAR_MS, T0 + 5 * BAR_MS)),
            inst_id="BTC-USDT",
            bar_size="1H",
            checked_range=(T0, T0 + 5 * BAR_MS),
            now=NOW + 1,
            present=1,
        )

        opens = [g for g in merged["gaps"] if g["status"] == "open"]
        assert {g["from"] for g in opens} == {T0 + BAR_MS, T0 + 4 * BAR_MS}
        assert merged["summary"]["gaps_open"] == 2

    def test_straddling_gap_not_marked_repaired(self) -> None:
        """缺口跨出窗口且本次无重叠：窗口外部分无法判定，保持 open 不误标 repaired。"""
        prev = {
            "gaps": [
                {
                    "from": T0,
                    "to": T0 + 10 * BAR_MS,
                    "bars": 10,
                    "status": "open",
                }
            ],
        }
        merged = merge_report(
            prev,
            _report(),
            inst_id="BTC-USDT",
            bar_size="1H",
            checked_range=(T0, T0 + 2 * BAR_MS),
            now=NOW + 1,
            present=2,
        )

        gap = merged["gaps"][0]
        assert gap["status"] == "open"
        assert "repaired_at" not in gap
        assert merged["summary"]["gaps_repaired"] == 0


# =====================================================================
# latest.json 读写
# =====================================================================


class TestReportStorage:
    def test_roundtrip_layout(self, tmp_path) -> None:
        report = {
            "inst_id": "BTC-USDT-SWAP",
            "bar_size": "1H",
            "generated_at": NOW,
            "checked_range": [T0, NOW],
            "summary": {"expected": 1, "present": 1, "gaps_open": 0, "gaps_repaired": 0},
            "gaps": [],
            "invalid_ohlc": [],
        }

        path = save_report(tmp_path, report)

        assert path == tmp_path / "quality" / "BTC-USDT-SWAP" / "1H" / "latest.json"
        assert load_report(tmp_path, "BTC-USDT-SWAP", "1H") == report
        assert not path.with_suffix(".json.tmp").exists()

    def test_corrupt_json_returns_none(self, tmp_path) -> None:
        path = report_path(tmp_path, "BTC-USDT", "1H")
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        assert load_report(tmp_path, "BTC-USDT", "1H") is None

    def test_missing_returns_none(self, tmp_path) -> None:
        assert load_report(tmp_path, "BTC-USDT", "1H") is None


# =====================================================================
# QualityReporter
# =====================================================================


class TestQualityReporter:
    def test_check_without_data_returns_none(self, tmp_path) -> None:
        reporter = QualityReporter(tmp_path)

        assert reporter.check("BTC-USDT", "1H", start_ts=T0, end_ts=T0) is None

    def test_check_writes_report_and_repairs(self, tmp_path) -> None:
        storage = BarStorage(tmp_path)
        storage.save_bars([_bar(T0), _bar(T0 + BAR_MS), _bar(T0 + 4 * BAR_MS)])
        reporter = QualityReporter(tmp_path, storage=storage)

        first = reporter.check(
            "BTC-USDT",
            "1H",
            now=NOW,
            start_ts=T0,
            end_ts=T0 + 4 * BAR_MS,
        )

        assert first is not None
        assert first["summary"]["present"] == 3
        assert first["summary"]["expected"] == 5
        assert first["summary"]["gaps_open"] == 1
        assert reporter.load("BTC-USDT", "1H") == first

        storage.save_bars([_bar(T0 + 2 * BAR_MS), _bar(T0 + 3 * BAR_MS)])
        second = reporter.check(
            "BTC-USDT",
            "1H",
            now=NOW + 1,
            start_ts=T0,
            end_ts=T0 + 4 * BAR_MS,
        )

        assert second is not None
        assert second["summary"]["gaps_open"] == 0
        assert second["summary"]["gaps_repaired"] == 1

    def test_summaries_lists_all(self, tmp_path) -> None:
        for inst, bar in (("BTC-USDT", "1H"), ("ETH-USDT", "4H")):
            save_report(
                tmp_path,
                {
                    "inst_id": inst,
                    "bar_size": bar,
                    "generated_at": NOW,
                    "checked_range": [T0, NOW],
                    "summary": {
                        "expected": 2,
                        "present": 2,
                        "gaps_open": 0,
                        "gaps_repaired": 0,
                    },
                    "gaps": [],
                    "invalid_ohlc": [],
                },
            )

        out = QualityReporter(tmp_path).summaries()

        assert {(item["inst_id"], item["bar_size"]) for item in out} == {
            ("BTC-USDT", "1H"),
            ("ETH-USDT", "4H"),
        }
        assert all(item["summary"]["gaps_open"] == 0 for item in out)


# =====================================================================
# monitor 告警
# =====================================================================


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _monitor(redis, tmp_path, **kwargs) -> Monitor:
    return Monitor(
        client=MagicMock(),
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
        quality=QualityReporter(tmp_path),
        **kwargs,
    )


def _write_quality(tmp_path, *, inst="BTC-USDT", bar="1H", open_n=1) -> None:
    save_report(
        tmp_path,
        {
            "inst_id": inst,
            "bar_size": bar,
            "generated_at": NOW,
            "checked_range": [T0, NOW],
            "summary": {
                "expected": 100,
                "present": 100 - open_n,
                "gaps_open": open_n,
                "gaps_repaired": 0,
            },
            "gaps": [],
            "invalid_ohlc": [],
        },
    )


class TestMonitorQualityAlert:
    async def test_open_gap_creates_alert(self, redis, tmp_path) -> None:
        _write_quality(tmp_path)
        monitor = _monitor(redis, tmp_path)

        alerts = await monitor._check_quality(NOW)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.rule == "data_quality"
        assert alert.level == "WARNING"
        assert alert.cooldown_key == "BTC-USDT:1H"
        assert "BTC-USDT 1H" in alert.message
        assert "缺失约 1 根" in alert.message

    async def test_no_open_gap_no_alert(self, redis, tmp_path) -> None:
        _write_quality(tmp_path, open_n=0)
        monitor = _monitor(redis, tmp_path)

        assert await monitor._check_quality(NOW) == []

    async def test_without_reporter_returns_empty(self, redis, tmp_path) -> None:
        monitor = Monitor(
            client=MagicMock(),
            redis=redis,
            settings=MonitorSettings(),
            alerter=EmailAlerter(SmtpSettings()),
        )

        assert await monitor._check_quality(NOW) == []

    async def test_cooldown_is_per_inst_and_bar(self, redis, tmp_path) -> None:
        """同品种 1H 的告警不能吞掉 4H；同 (inst, bar) 才去重。"""
        monitor = _monitor(redis, tmp_path)

        await monitor._publish_alerts(
            [
                Alert(
                    "data_quality",
                    "WARNING",
                    "1h",
                    NOW,
                    inst_id="BTC-USDT",
                    cooldown_key="BTC-USDT:1H",
                ),
                Alert(
                    "data_quality",
                    "WARNING",
                    "4h",
                    NOW,
                    inst_id="BTC-USDT",
                    cooldown_key="BTC-USDT:4H",
                ),
            ]
        )
        await monitor._publish_alerts(
            [
                Alert(
                    "data_quality",
                    "WARNING",
                    "1h again",
                    NOW + 1,
                    inst_id="BTC-USDT",
                    cooldown_key="BTC-USDT:1H",
                )
            ]
        )

        assert await redis.llen("quant:dashboard:alerts") == 2

    async def test_uses_quality_cooldown_window(self, redis, tmp_path) -> None:
        monitor = _monitor(redis, tmp_path, quality_cooldown_seconds=7200)

        assert monitor._cooldown_window("data_quality") == 7200
        assert monitor._cooldown_window("margin_ratio") == (
            MonitorSettings().dashboard_alert_cooldown_seconds
        )


# =====================================================================
# quant check-data
# =====================================================================


def _settings(tmp_path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage={"data_dir": tmp_path / "data"},
    )


class TestCheckDataCli:
    @patch("quant.cli.check_data.QualityReporter")
    @patch("quant.cli.check_data.BarStorage")
    @patch("quant.cli.check_data.Settings.load")
    def test_clean_returns_0_and_uses_list_years(
        self, mock_load, mock_storage_cls, mock_reporter_cls, tmp_path
    ) -> None:
        mock_load.return_value = _settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage.list_years.return_value = [2024]
        mock_storage_cls.return_value = mock_storage
        mock_reporter = MagicMock()
        mock_reporter.check.return_value = {
            "summary": {
                "expected": 10,
                "present": 10,
                "gaps_open": 0,
                "gaps_repaired": 0,
            },
            "gaps": [],
            "invalid_ohlc": [],
        }
        mock_reporter_cls.return_value = mock_reporter

        args = MagicMock(inst="BTC-USDT", bar="1H", start=None, end=None, config="x")
        ret = check_main(args)

        assert ret == 0
        call = mock_reporter.check.call_args
        assert call.args == ("BTC-USDT", "1H")
        assert call.kwargs["start_ts"] == 1704067200000  # 2024-01-01 UTC
        assert call.kwargs["end_ts"] == 1735689600000  # 2025-01-01 UTC

    @patch("quant.cli.check_data.QualityReporter")
    @patch("quant.cli.check_data.BarStorage")
    @patch("quant.cli.check_data.Settings.load")
    def test_open_gap_returns_1(
        self, mock_load, mock_storage_cls, mock_reporter_cls, tmp_path
    ) -> None:
        mock_load.return_value = _settings(tmp_path)
        mock_storage_cls.return_value = MagicMock()
        mock_reporter = MagicMock()
        mock_reporter.check.return_value = {
            "summary": {
                "expected": 10,
                "present": 9,
                "gaps_open": 1,
                "gaps_repaired": 0,
            },
            "gaps": [{"from": T0, "to": T0, "bars": 1, "status": "open"}],
            "invalid_ohlc": [],
        }
        mock_reporter_cls.return_value = mock_reporter

        args = MagicMock(
            inst="BTC-USDT",
            bar="1H",
            start="2024-01-01",
            end="2024-12-31",
            config="x",
        )
        assert check_main(args) == 1

    @patch("quant.cli.check_data.BarStorage")
    @patch("quant.cli.check_data.Settings.load")
    def test_no_local_data_returns_1(
        self, mock_load, mock_storage_cls, tmp_path
    ) -> None:
        mock_load.return_value = _settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage.list_years.return_value = []
        mock_storage_cls.return_value = mock_storage

        args = MagicMock(inst="BTC-USDT", bar="1H", start=None, end=None, config="x")

        assert check_main(args) == 1


# =====================================================================
# dashboard 接口
# =====================================================================


class TestQualityApi:
    async def test_list_and_detail(self, tmp_path) -> None:
        from quant.dashboard.service import DashboardService

        settings = _settings(tmp_path)
        store = SQLiteStore(tmp_path / "quant.db")
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        full = {
            "inst_id": "BTC-USDT",
            "bar_size": "1H",
            "generated_at": NOW,
            "checked_range": [T0, NOW],
            "summary": {
                "expected": 5,
                "present": 4,
                "gaps_open": 1,
                "gaps_repaired": 0,
            },
            "gaps": [
                {"from": T0, "to": T0, "bars": 1, "status": "open"}
            ],
            "invalid_ohlc": [],
        }
        save_report(settings.storage.data_dir, full)
        service = DashboardService(store, redis, settings)
        try:
            listing = await service.quality()
            assert listing["degraded"] == []
            assert listing["reports"][0]["inst_id"] == "BTC-USDT"
            assert listing["reports"][0]["summary"]["gaps_open"] == 1

            detail = await service.quality("BTC-USDT", "1H")
            assert detail["report"] == full

            missing = await service.quality("BTC-USDT", "4H")
            assert missing["report"] is None

            partial = await service.quality("BTC-USDT", None)
            assert "error" in partial
        finally:
            store.close()
            await redis.aclose()
