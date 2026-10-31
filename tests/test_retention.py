"""数据保留清理测试。

覆盖：
- K线 parquet：部分行过期（重写文件）、整文件过期（删文件）、目录不存在
- 日报 CSV：按修改时间清理
- SQLite：signals/orders/fills 过期清理；权益快照保留历史峰值
- RetentionPurger：到点执行、当天去重、未开启/未到点不跑
"""
from __future__ import annotations

import os
import time
from decimal import Decimal
from pathlib import Path

import fakeredis.aioredis
import pytest

from quant.backtest.order import Fill, Order
from quant.backtest.signal import Signal
from quant.foundation.config import OKXSettings, Settings
from quant.models import Bar
from quant.monitor.monitor import Monitor
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage
from quant.storage.retention import (
    RetentionPurger,
    purge_bars,
    purge_reports,
)

HOUR = 3_600_000
DAY = 86_400_000
NOW = 1_700_000_000_000


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
        inst_id="BTC-USDT",
        bar_size="1H",
    )


def _signal(signal_id: str, ts: int) -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side="buy",
        order_type="market",
        inst_id="BTC-USDT",
        qty=Decimal("0.1"),
        ts=ts,
        signal_id=signal_id,
    )


def _settings(tmp_path: Path, **storage) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p"),
        storage={"data_dir": tmp_path / "data", **storage},
        state={"db_path": tmp_path / "data" / "state" / "quant.db"},
        report={"out_dir": tmp_path / "reports" / "daily"},
    )


def _local_ms(hour: int, minute: int) -> int:
    """构造本地时间今天 HH:MM 的毫秒时间戳（用于调度测试）。"""
    lt = time.localtime()
    ts = time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1)
    )
    return int(ts * 1000)


class TestPurgeBars:
    def test_removes_old_rows(self, tmp_path) -> None:
        data_dir = tmp_path / "data"
        storage = BarStorage(data_dir)
        cutoff = NOW
        old_ts = cutoff - 2 * HOUR
        new_ts = cutoff + HOUR
        storage.save_bars([_bar(old_ts), _bar(old_ts + HOUR), _bar(new_ts)])

        rows, files = purge_bars(data_dir, cutoff)

        assert (rows, files) == (2, 0)
        left = storage.load_bars("BTC-USDT", "1H", 0, new_ts + HOUR)
        assert [b.ts for b in left] == [new_ts]

    def test_removes_fully_expired_file(self, tmp_path) -> None:
        data_dir = tmp_path / "data"
        storage = BarStorage(data_dir)
        old_ts = NOW - 400 * DAY
        storage.save_bars([_bar(old_ts), _bar(old_ts + HOUR)])

        rows, files = purge_bars(data_dir, NOW)

        assert (rows, files) == (2, 1)
        assert storage.load_bars("BTC-USDT", "1H", 0, NOW) == []

    def test_missing_dir(self, tmp_path) -> None:
        assert purge_bars(tmp_path / "nothing", NOW) == (0, 0)

    def test_nothing_expired_is_noop(self, tmp_path) -> None:
        data_dir = tmp_path / "data"
        storage = BarStorage(data_dir)
        storage.save_bars([_bar(NOW - HOUR), _bar(NOW)])

        assert purge_bars(data_dir, NOW - DAY) == (0, 0)
        assert len(storage.load_bars("BTC-USDT", "1H", 0, NOW)) == 2


class TestPurgeReports:
    def test_deletes_only_old_files(self, tmp_path) -> None:
        out_dir = tmp_path / "reports"
        out_dir.mkdir()
        old = out_dir / "2020-01-01-sma.csv"
        new = out_dir / "2026-01-01-sma.csv"
        old.write_text("x")
        new.write_text("y")
        old_time = (NOW - 10 * DAY) / 1000
        os.utime(old, (old_time, old_time))

        removed = purge_reports(out_dir, NOW - DAY)

        assert removed == 1
        assert not old.exists()
        assert new.exists()

    def test_missing_dir(self, tmp_path) -> None:
        assert purge_reports(tmp_path / "nothing", NOW) == 0


class TestPurgeState:
    def test_deletes_old_records(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            cutoff = NOW
            for sig in (_signal("old", cutoff - HOUR), _signal("new", cutoff + HOUR)):
                store.save_signal(sig, "filled")
                order = Order(
                    order_id=f"o-{sig.signal_id}",
                    signal=sig,
                    status="filled",
                    created_ts=sig.ts,
                )
                store.save_order(order)
                store.save_fill(
                    Fill(
                        order_id=order.order_id,
                        inst_id="BTC-USDT",
                        side="buy",
                        qty=Decimal("0.1"),
                        price=Decimal("100"),
                        fee=Decimal("0.01"),
                        ts=sig.ts,
                    )
                )

            deleted = store.purge_before(cutoff)

            assert deleted == {
                "fills": 1,
                "orders": 1,
                "signals": 1,
                "equity_snapshots": 0,
            }
            assert store.get_signal("old") is None
            assert store.get_signal("new") is not None
            assert len(store.get_recent_trades(10)) == 1
        finally:
            store.close()

    def test_keeps_equity_peak(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            cutoff = NOW
            for ts, equity in (
                (cutoff - 3 * HOUR, "5000"),
                (cutoff - 2 * HOUR, "20000"),  # 历史峰值，过期但必须保留
                (cutoff - HOUR, "6000"),
                (cutoff + HOUR, "7000"),
            ):
                store.save_equity_snapshot(
                    ts,
                    Decimal(equity),
                    Decimal(equity),
                    Decimal("0"),
                    Decimal("0"),
                    None,
                )

            deleted = store.purge_before(cutoff)

            assert deleted["equity_snapshots"] == 2
            assert store.get_peak_equity() == Decimal("20000")
            history = store.get_equity_history(0)
            assert [row["ts"] for row in history] == [cutoff - 2 * HOUR, cutoff + HOUR]
        finally:
            store.close()


class TestPurgeAlerts:
    """M9-12：告警按级别分档清理，且清理口径独立于 storage.retention_days。"""

    def test_purges_by_level(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        day = 86_400_000
        try:
            for level, age_days in (
                ("ERROR", 100),   # 超过 90 天 → 删
                ("ERROR", 80),    # 未超 90 天 → 留
                ("WARNING", 40),  # 超过 30 天 → 删
                ("WARNING", 10),  # 留
                ("INFO", 10),     # 超过 7 天 → 删
                ("INFO", 1),      # 留
            ):
                store.insert_alert(
                    f"{level}-{age_days}",
                    "margin_ratio",
                    level,
                    "x",
                    "fp",
                    created_at=NOW - age_days * day,
                )

            deleted = store.purge_alerts(NOW)

            assert deleted == {"ERROR": 1, "WARNING": 1, "INFO": 1}
            rows, total = store.list_alerts(10)
            assert total == 3
            assert {r["alert_id"] for r in rows} == {
                "ERROR-80",
                "WARNING-10",
                "INFO-1",
            }
        finally:
            store.close()

    def test_zero_days_keeps_level(self, tmp_path) -> None:
        """0 表示该级别不自动清理，避免误配把历史一次性清空。"""
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            store.insert_alert(
                "old-info", "dead_letter", "INFO", "x", "fp",
                created_at=NOW - 365 * 86_400_000,
            )

            deleted = store.purge_alerts(NOW, info_days=0)

            assert deleted["INFO"] == 0
            assert store.list_alerts(10)[1] == 1
        finally:
            store.close()


class TestRetentionPurger:
    def _env(self, tmp_path, **storage):
        settings = _settings(tmp_path, **storage)
        store = SQLiteStore(settings.state.db_path)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        return settings, store, redis

    async def test_runs_and_marks_day(self, tmp_path) -> None:
        settings, store, redis = self._env(
            tmp_path, retention_days=30, retention_at="00:00"
        )
        BarStorage(settings.storage.data_dir).save_bars([_bar(NOW - 90 * DAY)])
        try:
            purger = RetentionPurger(settings, store, redis)
            now = _local_ms(1, 0)

            first = await purger.maybe_run(now)
            second = await purger.maybe_run(now + HOUR)

            assert first is not None
            assert first.bars_deleted == 1
            assert second is None
            assert await redis.get(settings.storage.retention_last_run_key) == (
                time.strftime("%Y-%m-%d", time.localtime(now / 1000))
            )
        finally:
            store.close()

    async def test_not_due_before_at(self, tmp_path) -> None:
        settings, store, redis = self._env(
            tmp_path, retention_days=30, retention_at="23:59"
        )
        try:
            result = await RetentionPurger(settings, store, redis).maybe_run(
                _local_ms(0, 30)
            )
        finally:
            store.close()
        assert result is None

    async def test_disabled(self, tmp_path) -> None:
        settings, store, redis = self._env(
            tmp_path, retention_enabled=False, retention_at="00:00"
        )
        try:
            assert not RetentionPurger(settings, store, redis).enabled
            result = await RetentionPurger(settings, store, redis).maybe_run(NOW)
        finally:
            store.close()
        assert result is None

    @pytest.mark.parametrize("days", [0, -1])
    def test_non_positive_days_disables(self, tmp_path, days) -> None:
        settings, store, _ = self._env(
            tmp_path, retention_days=days, retention_at="00:00"
        )
        try:
            assert not RetentionPurger(settings, store).enabled
        finally:
            store.close()

    def test_run_aggregates_all_sources(self, tmp_path) -> None:
        settings, store, _ = self._env(tmp_path, retention_days=30)
        BarStorage(settings.storage.data_dir).save_bars(
            [_bar(NOW - 90 * DAY), _bar(NOW - HOUR)]
        )
        store.save_signal(_signal("old", NOW - 90 * DAY), "filled")
        store.insert_alert(
            "old-alert", "queue_backlog", "INFO", "x", "fp",
            created_at=NOW - 30 * DAY,
        )
        reports = settings.report.out_dir
        reports.mkdir(parents=True)
        stale = reports / "old.csv"
        stale.write_text("x")
        old_time = (NOW - 90 * DAY) / 1000
        os.utime(stale, (old_time, old_time))
        try:
            result = RetentionPurger(settings, store).run(NOW)
        finally:
            store.close()

        assert result.cutoff_ts == NOW - 30 * DAY
        assert result.bars_deleted == 1
        assert result.signals_deleted == 1
        assert result.report_files_removed == 1
        assert result.alerts_deleted == 1
        assert "bars=1" in result.summary()


class _FakeMonitorClient:
    """Monitor 采集用的假 OKX 客户端。"""

    def get_balance(self) -> dict:
        return {
            "totalEq": "10000",
            "upl": "0",
            "mgnRatio": "",
            "details": [{"ccy": "USDT", "availBal": "10000", "frozenBal": "0"}],
        }

    def get_positions(self, inst_type: str) -> list:
        return []

    def get_error_rate(self):
        return None


class _StubPurger:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls = 0
        self._exc = exc

    async def maybe_run(self, now_ms: int):
        self.calls += 1
        if self._exc:
            raise self._exc
        return None


class TestMonitorHook:
    async def test_run_once_triggers_retention(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(settings.state.db_path)
        stub = _StubPurger()
        monitor = Monitor(
            _FakeMonitorClient(),
            redis,
            settings.monitor,
            queue_key="q",
            dead_key="d",
            store=store,
            retention=stub,
        )
        try:
            await monitor.run_once()
        finally:
            store.close()
        assert stub.calls == 1

    async def test_failure_does_not_break_cycle(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(settings.state.db_path)
        monitor = Monitor(
            _FakeMonitorClient(),
            redis,
            settings.monitor,
            queue_key="q",
            dead_key="d",
            store=store,
            retention=_StubPurger(exc=RuntimeError("boom")),
        )
        try:
            await monitor.run_once()
        finally:
            store.close()
