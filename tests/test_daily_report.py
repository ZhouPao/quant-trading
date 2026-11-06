"""模拟盘日报（偏离自动比对）测试。

覆盖：
- `HH:MM` 解析与「当天是否该出报告」判定
- build_report 的异常路径（无成交/无 K线/周期不支持）
- DailyReporter 到点生成 CSV、Redis 去重、邮件发送
- Monitor 调用日报失败不影响监控主循环
"""
from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.order import Fill
from quant.foundation.config import OKXSettings, Settings
from quant.models import Bar
from quant.monitor.monitor import Monitor
from quant.report.daily import (
    DailyReporter,
    ReportError,
    build_report,
    is_due,
    parse_at,
    report_csv_rows,
    write_report_csv,
)
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage
from quant.strategies.python.sma_cross import SmaCrossStrategy

HOUR = 3_600_000
BASE_TS = 1_700_000_000_000


class TestParamSource:
    """M4-8：日报回测必须与实盘用同一套参数（toml ← DB 覆盖）。"""

    def test_load_strategy_reads_db_override(self, tmp_path) -> None:
        from quant.report.daily import _load_strategy

        settings = _settings(tmp_path)
        store = SQLiteStore(settings.state.db_path)
        try:
            store.set_strategy_param("sma_cross", "fast_len", "5", 1000)

            _cls, params = _load_strategy(
                "sma_cross", settings.report.config_dir, store, True
            )
        finally:
            store.close()

        # 看板改过参数后，日报回测不能再只读 toml（会误判 FAIL）
        assert params["fast_len"] == 5
        assert params["slow_len"] == 50

    def test_load_strategy_can_ignore_db(self, tmp_path) -> None:
        from quant.report.daily import _load_strategy

        settings = _settings(tmp_path)
        store = SQLiteStore(settings.state.db_path)
        try:
            store.set_strategy_param("sma_cross", "fast_len", "5", 1000)

            _cls, params = _load_strategy(
                "sma_cross", settings.report.config_dir, store, False
            )
        finally:
            store.close()

        assert params["fast_len"] == 10


def _bars(count: int = 400) -> list[Bar]:
    """缓慢震荡的 K线，触发 sma_cross 交易。"""
    bars = []
    for i in range(count):
        price = Decimal("100") + Decimal(i % 20)
        bars.append(
            Bar(
                ts=BASE_TS + i * HOUR,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                vol=Decimal("10"),
                vol_quote=Decimal("1000"),
                confirm=1,
                inst_id="BTC-USDT",
                bar_size="1H",
            )
        )
    return bars


def _settings(tmp_path: Path, **report) -> Settings:
    # 用临时策略配置，避免依赖仓库里 config/strategies 的实盘参数
    config_dir = tmp_path / "strategies"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "sma_cross.toml").write_text(
        '[strategy]\n'
        'strategy_id = "sma_cross"\n'
        'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n\n'
        "[params]\n"
        "fast_len = 10\n"
        "slow_len = 50\n"
    )
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p"),
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "data" / "state" / "quant.db"},
        report={
            "enabled": True,
            "at": "00:00",
            "out_dir": tmp_path / "reports",
            "config_dir": config_dir,
            **report,
        },
    )


def _local_ts(hour: int, minute: int = 0) -> int:
    """今天本地时间 hour:minute 的毫秒时间戳。"""
    now = time.localtime()
    return int(
        time.mktime(
            (now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1)
        )
        * 1000
    )


def _backtest_fills() -> list[Fill]:
    """用默认策略参数跑一次回测，得到与 paper_report 一致的成交序列。"""
    result = Backtest(
        strategy=SmaCrossStrategy(),
        matcher=Matcher(
            slippage_rate=Decimal("0.0005"),
            commission_rate=Decimal("0.001"),
        ),
        initial_cash=Decimal("10000"),
        params={"fast_len": 10, "slow_len": 50, "commission_rate": "0.001"},
    ).run(_bars())
    assert result.trades, "fixture must produce trades"
    return result.trades


class _FakeAlertService:
    """M4-8：记录 publish 调用的假告警服务。"""

    def __init__(self) -> None:
        self.events = []

    async def publish(self, event) -> str:
        self.events.append(event)
        return "alert-test-1"


class _BrokenAlertService:
    """publish 总是抛错：验证日报主流程不被拖垮。"""

    async def publish(self, event) -> str:
        raise RuntimeError("alert backend down")


class FakeAlerter:
    """记录 send 调用的假邮件发送器。"""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self.sent: list[tuple[str, str]] = []

    def enabled(self) -> bool:
        return self._enabled

    def send(self, subject: str, body: str) -> bool:
        self.sent.append((subject, body))
        return True


@pytest.fixture
def workspace(tmp_path):
    """真实 SQLite + parquet 数据，回测成交已回填到 fills 表。"""
    settings = _settings(tmp_path)
    BarStorage(settings.storage.data_dir).save_bars(_bars())
    store = SQLiteStore(settings.state.db_path)
    for fill in _backtest_fills():
        store.save_fill(fill)
    yield settings, store
    store.close()


class TestParseAt:
    def test_valid(self) -> None:
        assert parse_at("00:00") == 0
        assert parse_at("08:30") == 510
        assert parse_at("23:59") == 23 * 60 + 59

    @pytest.mark.parametrize("value", ["24:00", "08:60", "8", "ab:cd", ""])
    def test_invalid(self, value: str) -> None:
        assert parse_at(value) is None


class TestIsDue:
    def test_before_scheduled_time(self) -> None:
        now = _local_ts(7, 30)
        assert is_due(now, parse_at("08:00"), None) is False

    def test_after_scheduled_time(self) -> None:
        now = _local_ts(8, 30)
        assert is_due(now, parse_at("08:00"), None) is True

    def test_already_ran_today(self) -> None:
        now = _local_ts(9, 0)
        today = time.strftime("%Y-%m-%d", time.localtime(now / 1000))
        assert is_due(now, parse_at("08:00"), today) is False

    def test_ran_yesterday(self) -> None:
        now = _local_ts(9, 0)
        assert is_due(now, parse_at("08:00"), "2000-01-01") is True


class TestBuildReport:
    def test_missing_fills(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        store = SQLiteStore(settings.state.db_path)
        try:
            with pytest.raises(ReportError, match="no paper fills"):
                build_report(
                    settings, store, strategy="sma_cross", inst="BTC-USDT", bar="1H"
                )
        finally:
            store.close()

    def test_unsupported_bar(self, workspace) -> None:
        settings, store = workspace
        with pytest.raises(ReportError, match="unsupported bar"):
            build_report(
                settings, store, strategy="sma_cross", inst="BTC-USDT", bar="2H"
            )

    def test_missing_bars(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        store = SQLiteStore(settings.state.db_path)
        try:
            store.save_fill(
                Fill(
                    order_id="o1",
                    inst_id="BTC-USDT",
                    side="buy",
                    qty=Decimal("1"),
                    price=Decimal("100"),
                    fee=Decimal("0.1"),
                    ts=BASE_TS,
                )
            )
            with pytest.raises(ReportError, match="no local bars"):
                build_report(
                    settings, store, strategy="sma_cross", inst="BTC-USDT", bar="1H"
                )
        finally:
            store.close()

    def test_happy_path(self, workspace) -> None:
        settings, store = workspace
        report = build_report(
            settings, store, strategy="sma_cross", inst="BTC-USDT", bar="1H"
        )
        assert report.within_threshold is True
        assert report.paper.trade_count == report.backtest.trade_count

    def test_window_starting_mid_history(self, workspace) -> None:
        """窗口首笔若是平掉更早建立的仓位，回测必须在回看段连续成交才能复现。

        这正是日报不能用 `warmup_bars` 的原因：预热段若禁止成交，回测在窗口
        起点必然空仓，首笔卖出就对不上模拟盘。
        """
        settings, store = workspace
        report = build_report(
            settings,
            store,
            strategy="sma_cross",
            inst="BTC-USDT",
            bar="1H",
            limit=6,
        )
        assert report.paper.trade_count == 6
        assert report.within_threshold is True
        assert report.paper.trade_count == report.backtest.trade_count

    def test_uses_configured_window_size(self, workspace) -> None:
        """回测窗口必须与配置一致，否则策略看到的均线窗口与回看段长度不符。"""
        settings, store = workspace
        settings.backtest.window_size = 60

        report = build_report(
            settings, store, strategy="sma_cross", inst="BTC-USDT", bar="1H"
        )

        assert report.within_threshold is True


class TestCsv:
    def test_rows_and_file(self, workspace, tmp_path) -> None:
        settings, store = workspace
        report = build_report(
            settings, store, strategy="sma_cross", inst="BTC-USDT", bar="1H"
        )
        rows = report_csv_rows(report)
        assert [r[0] for r in rows] == [
            "trade_count",
            "qty",
            "notional",
            "fee",
            "pnl",
            "avg_price",
            "side_sequence",
        ]
        path = tmp_path / "out" / "daily.csv"
        write_report_csv(path, report)
        assert "trade_count" in path.read_text()


class TestDailyReporter:
    async def test_skipped_before_scheduled_time(self, workspace) -> None:
        settings, store = workspace
        settings.report.at = "23:59"
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        reporter = DailyReporter(settings, store, redis, FakeAlerter())

        assert await reporter.maybe_run(_local_ts(0, 1)) is None
        assert not Path(settings.report.out_dir).exists()

    async def test_disabled_reporter(self, workspace) -> None:
        settings, store = workspace
        settings.report.enabled = False
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        reporter = DailyReporter(settings, store, redis, FakeAlerter())

        assert reporter.enabled is False
        assert await reporter.maybe_run(_local_ts(23, 59)) is None

    async def test_generates_csv_and_marks_once(self, workspace) -> None:
        settings, store = workspace
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        alerter = FakeAlerter()
        reporter = DailyReporter(settings, store, redis, alerter)

        now = _local_ts(12, 0)
        result = await reporter.maybe_run(now)

        assert result is not None
        assert result.status == "PASS"
        assert result.csv_path.exists()
        assert "trade_count" in result.csv_path.read_text()
        assert await redis.get(settings.report.last_run_key) == result.csv_path.name[:10]
        assert len(alerter.sent) == 1
        assert "日报" in alerter.sent[0][0]

        # 同一天再调用不重复生成
        assert await reporter.maybe_run(_local_ts(13, 0)) is None
        assert len(alerter.sent) == 1

    async def test_email_skipped_when_disabled(self, workspace) -> None:
        settings, store = workspace
        settings.report.email = False
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        alerter = FakeAlerter()
        reporter = DailyReporter(settings, store, redis, alerter)

        result = await reporter.maybe_run(_local_ts(12, 0))

        assert result is not None
        assert alerter.sent == []

    async def test_fail_publishes_alert(self, workspace) -> None:
        """M4-8：偏离超阈值（FAIL）时进 alerts 告警体系。"""
        settings, store = workspace
        # 追加一笔模拟盘成交制造数量偏离，阈值压到 0 -> FAIL
        first = _backtest_fills()[0]
        store.save_fill(
            Fill(
                order_id="extra-1",
                inst_id=first.inst_id,
                side=first.side,
                qty=first.qty,
                price=first.price,
                fee=first.fee,
                ts=first.ts,
            )
        )
        settings.report.threshold = 0.0
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        alerts = _FakeAlertService()
        reporter = DailyReporter(
            settings, store, redis, FakeAlerter(), alert_service=alerts
        )

        result = await reporter.maybe_run(_local_ts(12, 0))

        assert result is not None and result.status == "FAIL"
        assert len(alerts.events) == 1
        event = alerts.events[0]
        assert event.rule == "daily_report_deviation"
        assert event.level == "WARNING"
        assert "FAIL" in event.message and "阈值" in event.message

    async def test_pass_does_not_publish_alert(self, workspace) -> None:
        settings, store = workspace
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        alerts = _FakeAlertService()
        reporter = DailyReporter(
            settings, store, redis, FakeAlerter(), alert_service=alerts
        )

        result = await reporter.maybe_run(_local_ts(12, 0))

        assert result is not None and result.status == "PASS"
        assert alerts.events == []

    async def test_alert_publish_failure_does_not_break_report(
        self, workspace
    ) -> None:
        """告警发布异常只记日志，日报仍正常返回。"""
        settings, store = workspace
        first = _backtest_fills()[0]
        store.save_fill(
            Fill(
                order_id="extra-1",
                inst_id=first.inst_id,
                side=first.side,
                qty=first.qty,
                price=first.price,
                fee=first.fee,
                ts=first.ts,
            )
        )
        settings.report.threshold = 0.0
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        reporter = DailyReporter(
            settings,
            store,
            redis,
            FakeAlerter(),
            alert_service=_BrokenAlertService(),
        )

        result = await reporter.maybe_run(_local_ts(12, 0))

        assert result is not None and result.status == "FAIL"

    async def test_no_fills_marks_day_without_result(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        BarStorage(settings.storage.data_dir).save_bars(_bars())
        store = SQLiteStore(settings.state.db_path)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        try:
            reporter = DailyReporter(settings, store, redis, FakeAlerter())
            assert await reporter.maybe_run(_local_ts(12, 0)) is None
            assert await redis.get(settings.report.last_run_key) is not None
        finally:
            store.close()

    async def test_invalid_at_disables_reporter(self, workspace) -> None:
        settings, store = workspace
        settings.report.at = "25:00"
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        reporter = DailyReporter(settings, store, redis, FakeAlerter())

        assert reporter.enabled is False
        assert await reporter.maybe_run(_local_ts(23, 59)) is None


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


class _StubDaily:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls = 0
        self._exc = exc

    async def maybe_run(self, now_ms: int):
        self.calls += 1
        if self._exc:
            raise self._exc
        return None


class TestMonitorHooksDailyReport:
    async def test_run_once_triggers_daily(self, tmp_path) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        stub = _StubDaily()
        monitor = Monitor(
            _FakeMonitorClient(),
            redis,
            _settings(tmp_path).monitor,
            queue_key="q",
            dead_key="d",
            store=store,
            daily=stub,
        )
        try:
            await monitor.run_once()
        finally:
            store.close()
        assert stub.calls == 1

    async def test_daily_failure_does_not_break_cycle(self, tmp_path) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        stub = _StubDaily(exc=RuntimeError("boom"))
        monitor = Monitor(
            _FakeMonitorClient(),
            redis,
            _settings(tmp_path).monitor,
            queue_key="q",
            dead_key="d",
            store=store,
            daily=stub,
        )
        try:
            alerts = await monitor.run_once()
        finally:
            store.close()
        assert isinstance(alerts, list)
        assert stub.calls == 1


class TestPaperReportUsesSharedBuilder:
    """paper-report CLI 与日报共用同一份偏离计算逻辑。"""

    def test_paper_report_cli_still_works(self, workspace, tmp_path, monkeypatch) -> None:
        from quant.cli import paper_report

        settings, store = workspace
        store.close()  # main 会自己开库
        monkeypatch.setattr(
            paper_report.Settings, "load", classmethod(lambda cls, p: settings)
        )
        out = tmp_path / "cli.csv"
        args = SimpleNamespace(
            strategy="sma_cross",
            inst="BTC-USDT",
            bar="1H",
            limit=500,
            threshold=0.05,
            window_hours=24,
            out=str(out),
            config_dir=str(settings.report.config_dir),
            config="config/config.toml",
        )
        assert paper_report.main(args) == 0
        assert "trade_count" in out.read_text()
