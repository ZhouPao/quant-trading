"""只读看板测试（M4 扩展）。

覆盖：
- service：概览/持仓/订单/成交/告警聚合与降级
- app：HTTP 端点、前端未构建时的根路径提示
- monitor 发布快照与告警到 Redis
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from unittest.mock import MagicMock

import fakeredis.aioredis
import httpx
import pytest

from quant.backtest.order import Fill, Order
from quant.backtest.position import Position
from quant.backtest.signal import Signal
from quant.dashboard.app import create_app
from quant.dashboard.service import DashboardService
from quant.execution.kill_switch import KillSwitch
from quant.foundation.config import OKXSettings, Settings
from quant.models import Bar
from quant.monitor.alerter import Alert
from quant.monitor.monitor import Monitor
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage

NOW = int(time.time() * 1000)


def _settings() -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True)
    )


def _signal(signal_id: str = "sig-1") -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side="buy",
        order_type="market",
        inst_id="BTC-USDT",
        qty=Decimal("1"),
        ts=NOW,
        signal_id=signal_id,
    )


def _snapshot(ts: int = NOW) -> dict:
    return {
        "ts": ts,
        "account": {
            "total_equity": "10000",
            "avail_usdt": "9000",
            "frozen_usdt": "1000",
            "unrealized_pnl": "12.5",
            "margin_ratio": None,
        },
        "metrics": {
            "max_drawdown": "0.02",
            "cancel_rate": None,
            "min_liq_distance": "0.35",
        },
        "positions": [
            {
                "inst_id": "BTC-USDT-SWAP",
                "qty": "2",
                "avg_price": "65000",
                "unrealized_pnl": "10",
                "leverage": "3",
                "liq_price": "40000",
                "mark_price": "65000",
                "liq_distance": "0.3846",
            }
        ],
    }


@pytest.fixture
def env(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SQLiteStore(tmp_path / "quant.db")
    settings = _settings()
    # 日报目录默认指向仓库内 reports/daily；每日任务会在真实目录落文件，
    # 不隔离的话 /api/reports 用例会随当天是否跑过日报而忽红忽绿
    settings.report.out_dir = str(tmp_path / "reports")
    service = DashboardService(store, redis, settings)
    yield service, store, redis, settings
    store.close()


@pytest.fixture
def candle_env(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SQLiteStore(tmp_path / "quant.db")
    settings = Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage={"data_dir": tmp_path / "data"},
    )
    service = DashboardService(store, redis, settings)
    yield service, store, settings
    store.close()


@pytest.fixture
def engine_env(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SQLiteStore(tmp_path / "quant.db")
    settings = Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage={"data_dir": tmp_path / "data"},
    )
    service = DashboardService(store, redis, settings)
    yield service, store, settings, redis
    store.close()


class TestOverview:
    async def test_empty_state(self, env) -> None:
        service, _store, _redis, _settings = env

        overview = await service.overview()

        assert overview["account"] is None
        assert overview["metrics"] is None
        assert overview["snapshot_age_seconds"] is None
        assert overview["worker"]["online"] is False
        assert overview["monitor"]["online"] is False
        assert overview["queue"] == {"pending": 0, "inflight": 0, "dead": 0}
        assert overview["kill_switch"] == {
            "active": False,
            "reason": None,
            "auth_required": False,
            "known": True,
        }
        assert overview["orders"] == {}
        # 快照缺失时账户列会变空，必须显式降级而不是静默显示 —
        assert overview["degraded"] == ["snapshot"]
        assert overview["positions_count"] == 0

    async def test_reads_published_snapshot(self, env) -> None:
        service, _store, redis, settings = env
        await redis.set(
            settings.monitor.dashboard_snapshot_key, json.dumps(_snapshot())
        )

        overview = await service.overview()

        assert overview["account"]["total_equity"] == "10000"
        assert overview["metrics"]["max_drawdown"] == "0.02"
        assert overview["positions_count"] == 1
        assert overview["snapshot_ts"] == NOW
        assert overview["snapshot_age_seconds"] is not None

    async def test_positions_count_matches_positions_table(self, env) -> None:
        """持仓数与持仓表同口径：本地记账 ∪ 快照，不能只数快照。"""
        from quant.backtest.position import Position

        service, store, redis, settings = env
        store.update_position(
            Position(inst_id="ETH-USDT", qty=Decimal("1"), avg_price=Decimal("2000"))
        )
        await redis.set(
            settings.monitor.dashboard_snapshot_key, json.dumps(_snapshot())
        )

        overview = await service.overview()
        positions = await service.positions()

        # 快照只有 BTC-USDT-SWAP，本地独有 ETH-USDT
        assert overview["positions_count"] == 2
        assert overview["positions_count"] == len(positions["positions"])

    async def test_heartbeat_online_and_offline(self, env) -> None:
        service, _store, redis, settings = env
        await redis.set(settings.monitor.worker_heartbeat_key, NOW)
        await redis.set(
            settings.monitor.monitor_heartbeat_key, NOW - 3_600_000
        )

        overview = await service.overview()

        assert overview["worker"]["online"] is True
        assert overview["monitor"]["online"] is False

    async def test_kill_switch_active(self, env) -> None:
        service, _store, redis, _settings = env
        await KillSwitch(redis).enable("manual panic")

        overview = await service.overview()

        assert overview["kill_switch"]["active"] is True
        assert overview["kill_switch"]["reason"] == "manual panic"

    async def test_queue_and_order_counts(self, env) -> None:
        service, store, redis, settings = env
        await redis.lpush(settings.redis.queue_key, "a")
        await redis.lpush(settings.redis.queue_key, "b")
        await redis.lpush(settings.redis.inflight_key, "c")
        await redis.lpush(settings.redis.dead_key, "d")

        signal = _signal()
        store.save_signal(signal, "executed")
        store.save_order(
            Order(
                order_id="o-1",
                signal=signal,
                status="pending",
                created_ts=NOW,
            )
        )

        overview = await service.overview()

        assert overview["queue"] == {"pending": 2, "inflight": 1, "dead": 1}
        assert overview["orders"] == {"pending": 1}

    async def test_degraded_when_redis_down(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        service = DashboardService(store, _BrokenRedis(), _settings())
        try:
            overview = await service.overview()
        finally:
            store.close()

        assert overview["degraded"] == ["redis"]
        assert overview["kill_switch"]["known"] is False
        assert overview["account"] is None

    async def test_invalid_snapshot_ignored(self, env) -> None:
        service, _store, redis, settings = env
        await redis.set(settings.monitor.dashboard_snapshot_key, "not-json")

        overview = await service.overview()

        assert overview["account"] is None
        assert overview["degraded"] == ["snapshot"]


class TestPositions:
    async def test_merges_local_and_remote(self, env) -> None:
        from quant.backtest.position import Position

        service, store, redis, settings = env
        store.update_position(
            Position(
                inst_id="BTC-USDT-SWAP",
                qty=Decimal("2"),
                avg_price=Decimal("64000"),
                realized_pnl=Decimal("5"),
            )
        )
        local_only = Position(inst_id="ETH-USDT", qty=Decimal("1"))
        store.update_position(local_only)
        await redis.set(
            settings.monitor.dashboard_snapshot_key, json.dumps(_snapshot())
        )

        data = await service.positions()

        by_inst = {p["inst_id"]: p for p in data["positions"]}
        assert by_inst["BTC-USDT-SWAP"]["local_qty"] == "2"
        assert by_inst["BTC-USDT-SWAP"]["liq_price"] == "40000"
        assert by_inst["ETH-USDT"]["qty"] is None
        assert by_inst["ETH-USDT"]["local_qty"] == "1"
        assert data["degraded"] == []

    async def test_remote_only_position_included(self, env) -> None:
        service, _store, redis, settings = env
        await redis.set(
            settings.monitor.dashboard_snapshot_key, json.dumps(_snapshot())
        )

        data = await service.positions()

        assert [p["inst_id"] for p in data["positions"]] == ["BTC-USDT-SWAP"]
        assert data["positions"][0]["local_qty"] is None

    async def test_spot_without_cost_shows_unknown(self, env) -> None:
        """现货无本地成本基数时按未知返回，避免把 0 显示成真实成本价。"""
        service, _store, redis, settings = env
        snapshot = _snapshot()
        snapshot["positions"] = [
            {
                "inst_id": "ETH-USDT",
                "qty": "2",
                "avg_price": "0",
                "unrealized_pnl": "0",
                "leverage": "1",
                "liq_price": None,
                "mark_price": "3000",
                "liq_distance": None,
            }
        ]
        await redis.set(
            settings.monitor.dashboard_snapshot_key, json.dumps(snapshot)
        )

        data = await service.positions()

        row = data["positions"][0]
        assert row["qty"] == "2"
        assert row["mark_price"] == "3000"
        assert row["avg_price"] is None
        assert row["unrealized_pnl"] is None

    async def test_strategy_pnl_uses_local_qty_and_cost(self, env) -> None:
        """策略盈亏只按本地数量与本地成本算，不掺入账户里的外部余额。"""
        from quant.backtest.position import Position

        service, store, redis, settings = env
        store.update_position(
            Position(
                inst_id="BTC-USDT",
                qty=Decimal("0.001"),
                avg_price=Decimal("80573.8"),
            )
        )
        snapshot = _snapshot()
        snapshot["positions"] = [
            {
                "inst_id": "BTC-USDT",
                # 账户余额远大于策略持仓，多出部分是外部转入
                "qty": "0.76633774",
                "avg_price": "0",
                "unrealized_pnl": "0",
                "leverage": "1",
                "liq_price": None,
                "mark_price": "80815.9",
                "liq_distance": None,
            }
        ]
        await redis.set(
            settings.monitor.dashboard_snapshot_key, json.dumps(snapshot)
        )

        data = await service.positions()

        row = data["positions"][0]
        assert row["local_qty"] == "0.001"
        assert row["local_avg_price"] == "80573.8"
        # 账户口径盈亏未知（成本基数缺失）
        assert row["unrealized_pnl"] is None
        # 策略口径：(80815.9 - 80573.8) × 0.001
        assert Decimal(row["strategy_unrealized_pnl"]) == Decimal("0.2421")

    async def test_strategy_pnl_empty_without_local_position(self, env) -> None:
        """本地无记录（纯交易所持仓）时策略盈亏留空。"""
        service, _store, redis, settings = env
        await redis.set(
            settings.monitor.dashboard_snapshot_key, json.dumps(_snapshot())
        )

        data = await service.positions()

        assert data["positions"][0]["strategy_unrealized_pnl"] is None

    async def test_flat_local_position_hidden(self, env) -> None:
        """已平掉的本地仓位（qty=0）不再出现在持仓列表。"""
        from quant.backtest.position import Position

        service, store, _redis, _settings = env
        store.update_position(Position(inst_id="BTC-USDT", qty=Decimal("0")))

        data = await service.positions()

        assert data["positions"] == []

    async def test_snapshot_missing_marks_degraded(self, env) -> None:
        """monitor 快照缺失时账户列全为空，必须显式降级。"""
        from quant.backtest.position import Position

        service, store, _redis, _settings = env
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("0.001"), avg_price=Decimal("80000"))
        )

        data = await service.positions()

        assert data["degraded"] == ["snapshot"]
        row = data["positions"][0]
        assert row["local_qty"] == "0.001"
        # 账户口径字段全部留空，不回退本地数量/成本
        assert row["qty"] is None
        assert row["avg_price"] is None


class TestOrdersAndFills:
    async def test_recent_orders_with_signal_context(self, env) -> None:
        service, store, _redis, _settings = env
        signal = _signal()
        store.save_signal(signal, "executed")
        store.save_order(
            Order(
                order_id="o-1",
                signal=signal,
                status="pending",
                created_ts=NOW,
            )
        )

        data = await service.orders(10)

        assert len(data["orders"]) == 1
        order = data["orders"][0]
        assert order["order_id"] == "o-1"
        assert order["inst_id"] == "BTC-USDT"
        assert order["side"] == "buy"
        assert order["status"] == "pending"

    async def test_fills_newest_first_with_total_fee(self, env) -> None:
        service, store, _redis, _settings = env
        store.save_fill(
            Fill(
                order_id="o-old",
                inst_id="BTC-USDT",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                fee=Decimal("0.1"),
                ts=NOW - 1000,
            )
        )
        store.save_fill(
            Fill(
                order_id="o-new",
                inst_id="BTC-USDT",
                side="sell",
                qty=Decimal("1"),
                price=Decimal("110"),
                fee=Decimal("0.11"),
                ts=NOW,
            )
        )

        data = await service.fills(10)

        assert [f["order_id"] for f in data["fills"]] == ["o-new", "o-old"]
        assert data["fills"][0]["notional"] == "110"
        assert data["total_fee"] == "0.21"


class TestPnl:
    @staticmethod
    def _fill(
        store,
        order_id: str,
        side: str,
        qty: str,
        price: str,
        ts: int,
        fee: str = "0",
    ) -> None:
        store.save_fill(
            Fill(
                order_id=order_id,
                inst_id="BTC-USDT",
                side=side,
                qty=Decimal(qty),
                price=Decimal(price),
                fee=Decimal(fee),
                ts=ts,
            )
        )

    async def test_empty(self, env) -> None:
        service, _store, _redis, _settings = env

        data = await service.pnl()

        assert data["fill_count"] == 0
        assert data["round_trips"] == 0
        assert data["total_pnl"] == "0"
        assert data["win_rate"] is None
        assert data["profit_factor"] is None
        assert data["first_ts"] is None
        assert data["degraded"] == []

    async def test_single_winning_round_trip(self, env) -> None:
        service, store, _redis, _settings = env
        self._fill(store, "o1", "buy", "1", "100", NOW - 2000, fee="0.1")
        self._fill(store, "o2", "sell", "1", "110", NOW - 1000, fee="0.11")

        data = await service.pnl()

        assert data["fill_count"] == 2
        assert data["round_trips"] == 1
        assert data["wins"] == 1
        assert data["losses"] == 0
        assert data["win_rate"] == "1.000000"
        # 10 - 0.1 - 0.11 = 9.79
        assert Decimal(data["total_pnl"]) == Decimal("9.79")
        assert Decimal(data["fee"]) == Decimal("0.21")
        assert Decimal(data["turnover"]) == Decimal("210")
        # 无亏损回合时盈亏比无定义
        assert data["profit_factor"] is None
        assert data["first_ts"] == NOW - 2000
        assert data["last_ts"] == NOW - 1000

    async def test_mixed_rounds_and_profit_factor(self, env) -> None:
        service, store, _redis, _settings = env
        # 回合 1：100 买 → 110 卖，赚 10
        self._fill(store, "o1", "buy", "1", "100", NOW - 5000)
        self._fill(store, "o2", "sell", "1", "110", NOW - 4000)
        # 回合 2：100 买 → 90 卖，亏 10
        self._fill(store, "o3", "buy", "1", "100", NOW - 3000)
        self._fill(store, "o4", "sell", "1", "90", NOW - 2000)

        data = await service.pnl()

        assert data["round_trips"] == 2
        assert data["wins"] == 1
        assert data["losses"] == 1
        assert data["win_rate"] == "0.500000"
        assert data["total_pnl"] == "0"
        assert data["profit_factor"] == "1.000000"
        assert data["best"] == "10"
        assert data["worst"] == "-10"

    async def test_open_position_not_counted(self, env) -> None:
        service, store, _redis, _settings = env
        self._fill(store, "o1", "buy", "1", "100", NOW - 1000)

        data = await service.pnl()

        assert data["fill_count"] == 1
        assert data["round_trips"] == 0
        assert data["total_pnl"] == "0"
        assert data["win_rate"] is None

    async def test_short_round_trip(self, env) -> None:
        service, store, _redis, _settings = env
        self._fill(store, "o1", "sell", "2", "110", NOW - 2000)
        self._fill(store, "o2", "buy", "2", "100", NOW - 1000)

        data = await service.pnl()

        assert data["round_trips"] == 1
        assert data["total_pnl"] == "20"


class TestReports:
    @staticmethod
    def _write(
        out_dir,
        name: str,
        *,
        dev: str = "0.000000",
        side: str = "match",
        mtime: float | None = None,
    ) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / name
        path.write_text(
            "metric,paper,backtest,deviation\n"
            f"trade_count,3,3,{dev}\n"
            "qty,1.5,1.5,0.000000\n"
            "notional,90000,90000,0.000000\n"
            "fee,90,90,0.000000\n"
            f"side_sequence,{side},match,{'0' if side == 'match' else '1'}\n",
            encoding="utf-8",
        )
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    async def test_empty_when_dir_missing(self, env, tmp_path) -> None:
        service, _store, _redis, settings = env
        settings.report.out_dir = tmp_path / "missing"

        data = await service.reports()

        assert data["reports"] == []
        assert data["degraded"] == []
        assert data["threshold"] == settings.report.threshold

    async def test_parse_and_status(self, env, tmp_path) -> None:
        service, _store, _redis, settings = env
        out_dir = tmp_path / "daily"
        settings.report.out_dir = out_dir
        self._write(
            out_dir, "2026-10-26-sma_cross-BTC-USDT.csv", mtime=1_700_000_000
        )
        self._write(
            out_dir,
            "2026-10-27-sma_cross-BTC-USDT.csv",
            dev="0.090000",
            mtime=1_700_100_000,
        )

        data = await service.reports()

        assert [r["date"] for r in data["reports"]] == ["2026-10-27", "2026-10-26"]
        by_date = {r["date"]: r for r in data["reports"]}
        assert by_date["2026-10-26"]["status"] == "PASS"
        assert by_date["2026-10-27"]["status"] == "FAIL"
        assert by_date["2026-10-27"]["max_deviation"] == pytest.approx(0.09)
        assert by_date["2026-10-26"]["strategy"] == "sma_cross"
        assert by_date["2026-10-26"]["inst"] == "BTC-USDT"
        assert len(by_date["2026-10-26"]["rows"]) == 5

    async def test_side_mismatch_fails(self, env, tmp_path) -> None:
        service, _store, _redis, settings = env
        out_dir = tmp_path / "daily"
        settings.report.out_dir = out_dir
        self._write(out_dir, "2026-10-26-sma_cross-BTC-USDT.csv", side="mismatch")

        data = await service.reports()

        assert data["reports"][0]["status"] == "FAIL"

    async def test_newest_first_and_limit(self, env, tmp_path) -> None:
        service, _store, _redis, settings = env
        out_dir = tmp_path / "daily"
        settings.report.out_dir = out_dir
        self._write(out_dir, "2026-10-25-sma_cross-BTC-USDT.csv", mtime=1_700_000_000)
        self._write(out_dir, "2026-10-26-sma_cross-BTC-USDT.csv", mtime=1_700_100_000)

        data = await service.reports(limit=1)

        assert len(data["reports"]) == 1
        assert data["reports"][0]["date"] == "2026-10-26"


class TestAlerts:
    async def test_reads_sqlite_newest_first(self, env) -> None:
        """落库开启时以 SQLite 为准，`ts`（事件时间）排序。"""
        service, store, _redis, settings = env
        for ts, level in ((NOW - 1000, "WARNING"), (NOW, "ERROR")):
            store.insert_alert(
                f"a-{ts}", "worker_heartbeat", level, f"m{ts}", "fp",
                created_at=ts,
            )

        data = await service.alerts(10)

        assert data["total"] == 2
        assert [a["alert_id"] for a in data["alerts"]] == [f"a-{NOW}", f"a-{NOW - 1000}"]
        assert data["degraded"] == []

    async def test_filters_and_pagination(self, env) -> None:
        service, store, _redis, settings = env
        for i in range(5):
            store.insert_alert(
                f"a-{i}", "margin_ratio" if i % 2 else "queue_backlog",
                "ERROR", f"m{i}", "fp", created_at=NOW + i,
            )

        page = await service.alerts(2, offset=1, rule="margin_ratio")

        assert page["total"] == 2
        assert [a["alert_id"] for a in page["alerts"]] == ["a-1"]

    async def test_falls_back_to_redis_when_persist_disabled(self, env) -> None:
        service, _store, redis, settings = env
        settings.monitor.alerts_persist = False
        key = settings.monitor.dashboard_alerts_key
        await redis.lpush(
            key,
            json.dumps(
                {
                    "rule": "worker_heartbeat",
                    "level": "ERROR",
                    "message": "lost",
                    "ts": NOW,
                }
            ),
        )
        await redis.lpush(key, "garbage")

        data = await service.alerts(10)

        assert len(data["alerts"]) == 1
        assert data["alerts"][0]["rule"] == "worker_heartbeat"
        # 回落路径拿不到总数，UI 据此提示「仅显示最近 N 条」
        assert data["total"] is None
        assert data["degraded"] == []

    async def test_degraded_when_redis_down(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        settings = _settings()
        settings.monitor.alerts_persist = False
        service = DashboardService(store, _BrokenRedis(), settings)
        try:
            data = await service.alerts(10)
        finally:
            store.close()

        assert data["alerts"] == []
        assert data["degraded"] == ["redis"]

    async def test_degraded_when_sqlite_down(self, env) -> None:
        """SQLite 读失败要显式降级并回落 Redis，而不是返回空列表。"""
        service, store, redis, settings = env
        await redis.lpush(
            settings.monitor.dashboard_alerts_key,
            json.dumps(
                {"rule": "margin_ratio", "level": "ERROR", "message": "m", "ts": NOW}
            ),
        )
        store.list_alerts = MagicMock(side_effect=RuntimeError("db locked"))

        data = await service.alerts(10)

        assert data["degraded"] == ["sqlite"]
        assert data["total"] is None
        assert len(data["alerts"]) == 1

    async def test_includes_active_silences(self, env) -> None:
        service, store, _redis, _settings = env
        store.insert_silence(
            "s-1", "margin_ratio", inst_id=None,
            until_ts=NOW + 3600_000, reason="维护", created_by="owner",
            created_at=NOW,
        )

        data = await service.alerts(10)

        assert [s["silence_id"] for s in data["silences"]] == ["s-1"]


class TestProcesses:
    def _table(self, names: list[str]) -> str:
        return json.dumps(
            {
                "ts": NOW,
                "pid": 100,
                "children": {
                    name: {
                        "pid": 200,
                        "started_at": NOW - 60_000,
                        "restarts": 2,
                        "last_exit": 1,
                    }
                    for name in names
                },
            }
        )

    async def _beat(self, redis, name: str, ts: int = NOW) -> None:
        await redis.set(
            f"quant:heartbeat:{name}",
            json.dumps({"ts": ts, "pid": 999}),
        )

    async def test_all_online(self, env) -> None:
        service, _store, redis, _settings = env
        names = ["candles", "webhook", "worker", "engine", "monitor", "dashboard"]
        await redis.set("quant:process:supervisor", self._table(names))
        for name in names:
            await self._beat(redis, name)

        data = await service.processes()

        assert data["online"] == 7
        assert data["offline"] == []
        assert data["degraded"] == []
        by_name = {p["name"]: p for p in data["processes"]}
        assert by_name["worker"]["restarts"] == 2
        assert by_name["worker"]["reason"] is None

    async def test_stale_heartbeat_reports_reason(self, env) -> None:
        service, _store, redis, settings = env
        await redis.set("quant:process:supervisor", self._table(["worker"]))
        await self._beat(redis, "worker", NOW - 10 * 60 * 1000)

        data = await service.processes()

        worker = next(p for p in data["processes"] if p["name"] == "worker")
        assert worker["online"] is False
        assert worker["reason"] == "stale"
        assert worker["age_seconds"] >= 600

    async def test_missing_key_is_no_key(self, env) -> None:
        service, _store, redis, _settings = env
        await redis.set("quant:process:supervisor", self._table(["worker"]))

        data = await service.processes()

        worker = next(p for p in data["processes"] if p["name"] == "worker")
        assert worker["reason"] == "no_key"

    async def test_unmanaged_process_is_not_managed(self, env) -> None:
        service, _store, redis, _settings = env
        # supervisor 在线，但只托管了 worker（如 --no-dashboard）
        await redis.set("quant:process:supervisor", self._table(["worker"]))
        await self._beat(redis, "worker")

        data = await service.processes()

        dashboard = next(p for p in data["processes"] if p["name"] == "dashboard")
        assert dashboard["online"] is False
        assert dashboard["reason"] == "not_managed"

    async def test_monitor_uses_300s_ttl(self, env) -> None:
        """monitor 写的是 300s TTL，按 120s 判活会误报离线。"""
        service, _store, redis, _settings = env
        await redis.set(
            "quant:process:supervisor", self._table(["monitor"])
        )
        await self._beat(redis, "monitor", NOW - 200_000)

        data = await service.processes()

        monitor = next(p for p in data["processes"] if p["name"] == "monitor")
        assert monitor["online"] is True

    async def test_redis_down_degrades(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        service = DashboardService(store, _BrokenRedis(), _settings())
        try:
            data = await service.processes()
        finally:
            store.close()

        assert data["degraded"] == ["redis"]
        assert all(p["online"] is False for p in data["processes"])


class TestAlertsApi:
    LOCAL = {"host": "127.0.0.1:8080", "X-Dashboard-Request": "1"}

    async def test_list_with_filters(self, env, tmp_path) -> None:
        service, store, _redis, _settings = env
        store.insert_alert(
            "a-1", "margin_ratio", "ERROR", "m", "fp", created_at=NOW
        )
        store.insert_alert(
            "a-2", "queue_backlog", "WARNING", "q", "fp", created_at=NOW + 1
        )
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/alerts?level=ERROR&limit=10")

        body = resp.json()
        assert resp.status_code == 200
        assert body["total"] == 1
        assert body["alerts"][0]["alert_id"] == "a-1"

    async def test_ack_requires_guard(self, env, tmp_path) -> None:
        service, store, _redis, _settings = env
        store.insert_alert("a-1", "margin_ratio", "ERROR", "m", "fp", created_at=NOW)
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.post(
                "/api/alerts/a-1/ack",
                json={"by": "alice"},
                headers={"host": "127.0.0.1:8080"},
            )
            ok = await client.post(
                "/api/alerts/a-1/ack", json={"by": "alice"}, headers=self.LOCAL
            )

        assert denied.status_code == 400
        assert ok.json()["acked"] is True
        row = store.list_alerts(10)[0][0]
        assert row["acked_by"] == "alice"

    async def test_silence_create_and_revoke(self, env, tmp_path) -> None:
        service, store, _redis, _settings = env
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/alerts/silence",
                json={"rule": "margin_ratio", "hours": 2, "reason": "维护"},
                headers=self.LOCAL,
            )
            silence_id = created.json()["silence_id"]
            listed = await client.get("/api/alerts?limit=5")
            revoked = await client.delete(
                f"/api/alerts/silence/{silence_id}", headers=self.LOCAL
            )

        assert listed.json()["silences"][0]["silence_id"] == silence_id
        assert revoked.json()["revoked"] is True
        assert store.list_active_silences(int(time.time() * 1000)) == []

    async def test_processes_endpoint(self, env, tmp_path) -> None:
        service, _store, redis, _settings = env
        await redis.set(
            "quant:process:supervisor",
            json.dumps({"ts": NOW, "pid": 1, "children": {}}),
        )
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/processes")

        body = resp.json()
        assert resp.status_code == 200
        assert body["total"] == 7
        assert {p["name"] for p in body["processes"]} == {
            "supervisor", "candles", "webhook", "worker",
            "engine", "monitor", "dashboard",
        }


class TestEquity:
    async def test_history_ascending(self, env) -> None:
        service, store, _redis, _settings = env
        self._save(store, NOW - 60_000, "100")
        self._save(store, NOW, "110")

        result = await service.equity(hours=24)

        assert [p["total_equity"] for p in result["points"]] == ["100", "110"]
        assert result["degraded"] == []

    async def test_window_excludes_old_points(self, env) -> None:
        service, store, _redis, _settings = env
        self._save(store, NOW - 48 * 3600 * 1000, "90")
        self._save(store, NOW, "110")

        result = await service.equity(hours=24)

        assert [p["total_equity"] for p in result["points"]] == ["110"]

    async def test_upsert_same_ts(self, env) -> None:
        _service, store, _redis, _settings = env
        self._save(store, NOW, "100")
        self._save(store, NOW, "105")

        points = store.get_equity_history(0)

        assert len(points) == 1
        assert points[0]["total_equity"] == "105"

    def test_peak_equity(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            assert store.get_peak_equity() is None
            self._save(store, NOW - 2000, "100")
            self._save(store, NOW - 1000, "250")
            self._save(store, NOW, "180")
            assert store.get_peak_equity() == Decimal("250")
        finally:
            store.close()

    @staticmethod
    def _save(store: SQLiteStore, ts: int, equity: str) -> None:
        store.save_equity_snapshot(
            ts=ts,
            total_equity=Decimal(equity),
            avail_usdt=Decimal(equity),
            frozen_usdt=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            margin_ratio=None,
        )


class TestEngine:
    HOUR = 3_600_000

    @staticmethod
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

    async def test_empty_state(self, engine_env) -> None:
        service, _store, _settings, _redis = engine_env

        data = await service.engine()

        assert data["online"] is False
        assert data["status"] is None
        assert data["watermark_ts"] is None
        assert data["last_bar_ts"] is None
        assert data["lag_bars"] is None
        assert data["catch_up_total"] is None
        assert data["degraded"] == []

    async def test_reports_status_watermark_and_lag(self, engine_env) -> None:
        service, _store, settings, redis = engine_env
        now = int(time.time() * 1000)
        last_closed = (now // self.HOUR) * self.HOUR - self.HOUR
        BarStorage(settings.storage.data_dir).save_bars(
            [
                self._bar(last_closed - 2 * self.HOUR),
                self._bar(last_closed - self.HOUR),
                self._bar(last_closed),
            ]
        )
        watermark = last_closed - self.HOUR
        status_key = (
            f"{settings.engine.status_key}:"
            f"{settings.engine.strategy}:{settings.engine.inst}"
        )
        marker_key = (
            f"{settings.engine.last_bar_key}:"
            f"{settings.engine.strategy}:{settings.engine.inst}"
        )
        await redis.set(
            status_key,
            json.dumps(
                {
                    "ts": now,
                    "status": "no_signal",
                    "bar_ts": watermark,
                    "reason": "",
                    "catch_up": 0,
                    "catch_up_total": 2,
                    "skipped_total": 1,
                    "signals_total": 3,
                    "ticks": 10,
                }
            ),
        )
        await redis.set(marker_key, str(watermark))

        data = await service.engine()

        assert data["online"] is True
        assert data["status"] == "no_signal"
        assert data["last_bar_ts"] == watermark
        assert data["watermark_ts"] == watermark
        assert data["local_bar_ts"] == last_closed
        assert data["lag_bars"] == 1
        assert data["catch_up_total"] == 2
        assert data["skipped_total"] == 1
        assert data["signals_total"] == 3
        assert data["ticks"] == 10
        assert data["degraded"] == []

    async def test_stale_status_marks_offline(self, engine_env) -> None:
        service, _store, settings, redis = engine_env
        status_key = (
            f"{settings.engine.status_key}:"
            f"{settings.engine.strategy}:{settings.engine.inst}"
        )
        await redis.set(
            status_key,
            json.dumps(
                {"ts": NOW - 3_600_000, "status": "no_signal", "bar_ts": NOW}
            ),
        )

        data = await service.engine()

        assert data["online"] is False
        assert data["status"] == "no_signal"
        assert data["status_age_seconds"] >= 3_600

    async def test_degraded_when_redis_down(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        service = DashboardService(store, _BrokenRedis(), _settings())
        try:
            data = await service.engine()
        finally:
            store.close()

        assert data["online"] is False
        assert data["status"] is None
        assert data["degraded"] == ["redis"]


class TestFreshness:
    """M1-7：区分「引擎落后」与「本地数据停更」两种不新鲜。"""

    HOUR = 3_600_000

    @staticmethod
    def _bar(ts: int) -> Bar:
        value = Decimal("100")
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

    @staticmethod
    async def _set_watermark(settings, redis, ts: int) -> None:
        await redis.set(
            f"{settings.engine.last_bar_key}:"
            f"{settings.engine.strategy}:{settings.engine.inst}",
            str(ts),
        )

    async def test_fresh_when_local_and_engine_in_sync(self, engine_env) -> None:
        service, _store, settings, redis = engine_env
        now = int(time.time() * 1000)
        last_closed = (now // self.HOUR) * self.HOUR - self.HOUR
        BarStorage(settings.storage.data_dir).save_bars([self._bar(last_closed)])
        await self._set_watermark(settings, redis, last_closed)

        data = await service.freshness()

        item = data["items"][0]
        assert item["inst_id"] == settings.engine.inst
        assert item["bar"] == settings.engine.bar
        assert item["last_bar_ts"] == last_closed
        assert item["lag_bars"] == 0
        assert item["missing_bars"] == 0
        assert item["stale"] is False
        assert data["stale_count"] == 0
        assert data["degraded"] == []

    async def test_stale_when_engine_lagging(self, engine_env) -> None:
        service, _store, settings, redis = engine_env
        now = int(time.time() * 1000)
        last_closed = (now // self.HOUR) * self.HOUR - self.HOUR
        BarStorage(settings.storage.data_dir).save_bars(
            [self._bar(last_closed - self.HOUR), self._bar(last_closed)]
        )
        await self._set_watermark(settings, redis, last_closed - self.HOUR)

        data = await service.freshness()

        item = data["items"][0]
        assert item["lag_bars"] == 1
        assert item["missing_bars"] == 0
        assert item["stale"] is True
        assert data["stale_count"] == 1

    async def test_stale_when_candles_stopped(self, engine_env) -> None:
        """水位与本地末尾相等（lag_bars=0），但两者都过期了。"""
        service, _store, settings, redis = engine_env
        now = int(time.time() * 1000)
        last_closed = (now // self.HOUR) * self.HOUR - self.HOUR
        stale_ts = last_closed - 3 * self.HOUR
        BarStorage(settings.storage.data_dir).save_bars([self._bar(stale_ts)])
        await self._set_watermark(settings, redis, stale_ts)

        data = await service.freshness()

        item = data["items"][0]
        assert item["lag_bars"] == 0
        assert item["missing_bars"] == 3
        assert item["stale"] is True

    async def test_degraded_when_redis_down(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        settings = _settings()
        settings.storage.data_dir = tmp_path / "data"
        service = DashboardService(store, _BrokenRedis(), settings)
        try:
            data = await service.freshness()
        finally:
            store.close()

        assert data["degraded"] == ["redis"]
        assert data["items"][0]["stale"] is False


class TestCandles:
    HOUR = 3_600_000

    @staticmethod
    def _bars(count: int, start_ts: int, inst: str = "BTC-USDT") -> list[Bar]:
        hour = 3_600_000
        out = []
        for i in range(count):
            price = Decimal(str(100 + i))
            out.append(
                Bar(
                    ts=start_ts + i * hour,
                    open=price,
                    high=price + 1,
                    low=price - 1,
                    close=price,
                    vol=Decimal("1.5"),
                    vol_quote=price,
                    confirm=1,
                    inst_id=inst,
                    bar_size="1H",
                )
            )
        return out

    async def test_returns_bars_and_defaults(self, candle_env) -> None:
        service, _store, settings = candle_env
        BarStorage(settings.storage.data_dir).save_bars(
            self._bars(50, NOW - 50 * self.HOUR)
        )

        result = await service.candles(limit=50)

        assert result["inst"] == settings.engine.inst
        assert result["bar"] == settings.engine.bar
        assert len(result["bars"]) == 50
        assert result["bars"][0]["open"] == "100"
        assert result["bars"][-1]["close"] == "149"
        assert result["available_bars"] == ["1H"]
        assert result["degraded"] == []

    async def test_no_data_marks_degraded(self, candle_env) -> None:
        service, _store, _settings = candle_env

        result = await service.candles()

        assert result["bars"] == []
        assert result["fills"] == []
        assert result["position"] is None
        assert result["degraded"] == ["candles"]

    async def test_invalid_bar(self, candle_env) -> None:
        service, _store, _settings = candle_env

        result = await service.candles(bar="3X")

        assert result["degraded"] == ["bar"]
        assert result["bars"] == []

    async def test_includes_fills_and_position(self, candle_env) -> None:
        service, store, settings = candle_env
        BarStorage(settings.storage.data_dir).save_bars(
            self._bars(20, NOW - 20 * self.HOUR)
        )
        signal = _signal()
        store.save_signal(signal, "filled")
        store.save_fill(
            Fill(
                order_id="o-1",
                inst_id="BTC-USDT",
                side="buy",
                qty=Decimal("0.5"),
                price=Decimal("120"),
                fee=Decimal("0.1"),
                ts=NOW - self.HOUR,
            )
        )
        store.save_fill(
            Fill(
                order_id="o-2",
                inst_id="ETH-USDT",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("3000"),
                fee=Decimal("0.1"),
                ts=NOW - self.HOUR,
            )
        )
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("0.5"), avg_price=Decimal("120"))
        )

        result = await service.candles(limit=20)

        assert len(result["fills"]) == 1
        assert result["fills"][0]["side"] == "buy"
        assert result["position"] == {"qty": "0.5", "avg_price": "120"}

    async def test_flat_position_omitted(self, candle_env) -> None:
        service, store, settings = candle_env
        BarStorage(settings.storage.data_dir).save_bars(
            self._bars(20, NOW - 20 * self.HOUR)
        )
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("0"), avg_price=Decimal("0"))
        )

        result = await service.candles(limit=20)

        assert result["position"] is None


class TestKillSwitch:
    LOCAL = {"host": "127.0.0.1:8080", "X-Dashboard-Request": "1"}

    async def test_service_toggle(self, env) -> None:
        service, _store, _redis, _settings = env

        result = await service.set_kill_switch(True, "manual stop")

        assert result["active"] is True
        assert result["reason"] == "manual stop"
        overview = await service.overview()
        assert overview["kill_switch"]["active"] is True
        assert overview["kill_switch"]["reason"] == "manual stop"
        assert overview["kill_switch"]["auth_required"] is False

        result = await service.set_kill_switch(False)
        assert result["active"] is False
        assert (await service.overview())["kill_switch"]["active"] is False

    async def test_rejects_missing_header_and_foreign_host(
        self, env, tmp_path
    ) -> None:
        service, _store, _redis, _settings = env
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            missing = await client.post(
                "/api/kill-switch",
                json={"action": "on"},
                headers={"host": "127.0.0.1:8080"},
            )
            foreign = await client.post(
                "/api/kill-switch",
                json={"action": "on"},
                headers={"host": "evil.example", "X-Dashboard-Request": "1"},
            )

        assert missing.status_code == 400
        assert foreign.status_code == 403
        assert (await service.overview())["kill_switch"]["active"] is False

    async def test_http_toggle(self, env, tmp_path) -> None:
        service, _store, _redis, _settings = env
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            on = await client.post(
                "/api/kill-switch",
                json={"action": "on", "reason": "手动暂停"},
                headers=self.LOCAL,
            )
            overview = await client.get("/api/overview")
            off = await client.post(
                "/api/kill-switch", json={"action": "off"}, headers=self.LOCAL
            )
            bad = await client.post(
                "/api/kill-switch", json={"action": "pause"}, headers=self.LOCAL
            )

        assert on.status_code == 200
        assert on.json()["active"] is True
        assert overview.json()["kill_switch"]["active"] is True
        assert overview.json()["kill_switch"]["reason"] == "手动暂停"
        assert off.status_code == 200
        assert off.json()["active"] is False
        assert bad.status_code == 422

    async def test_token_enforced_when_configured(self, env, tmp_path) -> None:
        service, _store, _redis, settings = env
        settings.dashboard.token = "s3cret"
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            no_token = await client.post(
                "/api/kill-switch", json={"action": "on"}, headers=self.LOCAL
            )
            wrong = await client.post(
                "/api/kill-switch",
                json={"action": "on"},
                headers={**self.LOCAL, "X-Dashboard-Token": "nope"},
            )
            ok = await client.post(
                "/api/kill-switch",
                json={"action": "on"},
                headers={**self.LOCAL, "X-Dashboard-Token": "s3cret"},
            )
            overview = await client.get("/api/overview")

        assert no_token.status_code == 401
        assert wrong.status_code == 401
        assert ok.status_code == 200
        assert ok.json()["auth_required"] is True
        assert overview.json()["kill_switch"]["auth_required"] is True


class TestRiskLimits:
    KEY = "quant:risk:limits:sma_cross"

    async def _publish(
        self,
        redis,
        *,
        rules: dict | None = None,
        paper_mode: bool = False,
        ts: int = NOW,
        sources: dict | None = None,
    ) -> None:
        payload = {
            "ts": ts,
            "strategy": "sma_cross",
            "paper_mode": paper_mode,
            "rules": {
                "max_position_pct": "0.3",
                "max_total_pct": "0.8",
                "max_order_qty": "10000",
                "daily_loss_limit": "0.05",
                "consecutive_loss_limit": 3,
                **(rules or {}),
            },
            "sources": sources or {},
        }
        await redis.set(self.KEY, json.dumps(payload))

    @staticmethod
    def _today_fills(store) -> None:
        now = int(time.time() * 1000)
        store.save_fill(
            Fill(
                order_id="o-buy",
                inst_id="BTC-USDT",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                fee=Decimal("0"),
                ts=now - 1000,
            )
        )
        store.save_fill(
            Fill(
                order_id="o-sell",
                inst_id="BTC-USDT",
                side="sell",
                qty=Decimal("1"),
                price=Decimal("90"),
                fee=Decimal("0"),
                ts=now,
            )
        )

    async def test_unknown_without_published_limits(self, env) -> None:
        service, _store, _redis, _settings = env

        result = await service.risk_limits()

        assert result["known"] is False
        assert result["strategy"] is None
        assert result["rules"] == []
        assert result["degraded"] == []

    async def test_computes_usage_and_levels(self, env) -> None:
        service, store, redis, _settings = env
        await self._publish(redis)
        await redis.set(
            service.settings.monitor.dashboard_snapshot_key,
            json.dumps(_snapshot()),
        )
        store.update_position(
            Position(
                inst_id="BTC-USDT",
                qty=Decimal("0.01"),
                avg_price=Decimal("60000"),
            )
        )
        self._today_fills(store)

        result = await service.risk_limits()

        assert result["known"] is True
        assert result["strategy"] == "sma_cross"
        assert result["paper_mode"] is False
        assert result["equity"] == "10000"
        assert result["equity_source"] == "snapshot"

        rules = {rule["key"]: rule for rule in result["rules"]}
        assert len(rules) == 5
        # 已实现亏损 10 / (10000 * 5%) = 2%
        assert rules["daily_loss"]["ratio"] == "0.020000"
        assert rules["daily_loss"]["level"] == "ok"
        # 连续亏损 1 / 3
        assert rules["consecutive_loss"]["ratio"] == "0.333333"
        # 单笔上限只给参考值，不画进度
        assert rules["order_notional"]["used"] is None
        assert rules["order_notional"]["ratio"] is None
        # 快照标记价 65000（BTC-USDT 前缀匹配 BTC-USDT-SWAP）
        assert Decimal(rules["inst_position"]["used"]) == Decimal("650.00")
        assert Decimal(rules["inst_position"]["limit"]) == Decimal("3000")
        assert Decimal(rules["total_position"]["limit"]) == Decimal("8000")
        assert result["degraded"] == []

    async def test_surfaces_limit_source(self, env) -> None:
        """限值来源要透出，用于区分「策略写死的」和「临时放宽的」。"""
        service, _store, redis, _settings = env
        await self._publish(
            redis,
            sources={
                "max_position_pct": "override",
                "max_total_pct": "strategy",
            },
        )

        result = await service.risk_limits()

        rules = {rule["key"]: rule for rule in result["rules"]}
        assert rules["inst_position"]["source"] == "override"
        assert rules["total_position"]["source"] == "strategy"
        # 未发布来源的规则保持 None，而不是编一个值
        assert rules["daily_loss"]["source"] is None
        assert result["degraded"] == []

    async def test_daily_loss_halt_at_limit(self, env) -> None:
        service, store, redis, _settings = env
        await self._publish(redis, rules={"daily_loss_limit": "0.0005"})
        self._today_fills(store)

        result = await service.risk_limits()

        rules = {rule["key"]: rule for rule in result["rules"]}
        # 亏损 10 > 上限 5 → 200%
        assert rules["daily_loss"]["ratio"] == "2.000000"
        assert rules["daily_loss"]["level"] == "halt"

    async def test_paper_mode_effective_values(self, env) -> None:
        service, _store, redis, _settings = env
        await self._publish(
            redis,
            rules={"max_position_pct": "1.0", "max_total_pct": "1.0"},
            paper_mode=True,
        )

        result = await service.risk_limits()

        rules = {rule["key"]: rule for rule in result["rules"]}
        assert result["paper_mode"] is True
        assert Decimal(rules["inst_position"]["limit"]) == Decimal("10000")
        assert Decimal(rules["total_position"]["limit"]) == Decimal("10000")

    async def test_equity_falls_back_to_initial_cash(self, env) -> None:
        service, _store, redis, settings = env
        await self._publish(redis)

        result = await service.risk_limits()

        assert result["equity_source"] == "initial_cash"
        assert Decimal(result["equity"]) == settings.backtest.initial_cash


class TestApp:
    async def test_api_endpoints(self, env, tmp_path) -> None:
        service, _store, _redis, _settings = env
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            health = await client.get("/api/health")
            overview = await client.get("/api/overview")
            orders = await client.get("/api/orders?limit=5")
            fills = await client.get("/api/fills")
            pnl = await client.get("/api/pnl")
            risk = await client.get("/api/risk-limits")
            reports = await client.get("/api/reports?limit=5")
            alerts = await client.get("/api/alerts")
            positions = await client.get("/api/positions")
            root = await client.get("/")

        assert health.status_code == 200
        assert overview.status_code == 200
        # 空库 + 无快照：快照本身被标记为降级是预期行为
        assert overview.json()["degraded"] == ["snapshot"]
        assert orders.status_code == 200
        assert orders.json()["orders"] == []
        assert fills.status_code == 200
        assert pnl.status_code == 200
        assert pnl.json()["fill_count"] == 0
        assert risk.status_code == 200
        assert risk.json()["known"] is False
        assert reports.status_code == 200
        assert reports.json()["reports"] == []
        assert alerts.status_code == 200
        assert positions.status_code == 200
        assert root.status_code == 200
        assert "api" in root.json()

    async def test_limit_validation(self, env, tmp_path) -> None:
        service, _store, _redis, _settings = env
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            too_big = await client.get("/api/orders?limit=9999")
            too_small = await client.get("/api/orders?limit=0")

        assert too_big.status_code == 422
        assert too_small.status_code == 422

    async def test_equity_endpoint(self, env, tmp_path) -> None:
        service, store, _redis, _settings = env
        TestEquity._save(store, NOW, "10000")
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ok = await client.get("/api/equity?hours=24")
            too_long = await client.get("/api/equity?hours=1000")
            too_short = await client.get("/api/equity?hours=0")

        assert ok.status_code == 200
        assert ok.json()["points"][0]["total_equity"] == "10000"
        assert too_long.status_code == 422
        assert too_short.status_code == 422

    async def test_engine_endpoint(self, env, tmp_path) -> None:
        service, _store, _redis, _settings = env
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/engine")

        assert response.status_code == 200
        payload = response.json()
        assert payload["online"] is False
        assert payload["status"] is None

    async def test_freshness_endpoint(self, engine_env, tmp_path) -> None:
        service, _store, _settings, _redis = engine_env
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/freshness")

        assert response.status_code == 200
        payload = response.json()
        assert payload["threshold_bars"] == 1
        assert payload["stale_count"] == 0
        assert len(payload["items"]) == 1
        # 本地无 bar 时不把缺失渲染成「新鲜」以外的具体数字
        assert payload["items"][0]["last_bar_ts"] is None
        assert payload["items"][0]["lag_bars"] is None

    async def test_candles_endpoint(self, candle_env, tmp_path) -> None:
        service, _store, _settings = candle_env
        app = create_app(service, tmp_path / "missing-dist")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ok = await client.get("/api/candles?limit=100")
            too_small = await client.get("/api/candles?limit=1")
            too_big = await client.get("/api/candles?limit=5000")

        assert ok.status_code == 200
        assert ok.json()["bars"] == []
        assert "candles" in ok.json()["degraded"]
        assert too_small.status_code == 422
        assert too_big.status_code == 422

    async def test_serves_built_frontend(self, env, tmp_path) -> None:
        service, _store, _redis, _settings = env
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        app = create_app(service, dist)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            page = await client.get("/")

        assert page.status_code == 200
        assert "ok" in page.text


class TestMonitorPublish:
    async def test_run_once_publishes_snapshot_and_alerts(
        self, tmp_path
    ) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        settings = _settings()
        client = _FakeMonitorClient()
        monitor = Monitor(client, redis, settings.monitor, store=store)
        try:
            await monitor.run_once()
        finally:
            store.close()

        raw = await redis.get(settings.monitor.dashboard_snapshot_key)
        payload = json.loads(raw)
        assert payload["account"]["total_equity"] == "10000"
        assert payload["metrics"]["max_drawdown"] == "0"

        alerts = await redis.lrange(
            settings.monitor.dashboard_alerts_key, 0, -1
        )
        assert len(alerts) >= 1
        assert json.loads(alerts[0])["rule"] == "worker_heartbeat"


class TestMonitorEquityPersistence:
    async def test_drawdown_restored_from_history(self, tmp_path) -> None:
        """重启后回撤从库里的历史峰值计算，不再归零。"""
        db = tmp_path / "quant.db"
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = _settings()

        seed = SQLiteStore(db)
        TestEquity._save(seed, NOW - 600_000, "20000")
        seed.close()

        store = SQLiteStore(db)
        monitor = Monitor(
            _FakeMonitorClient(), redis, settings.monitor, store=store
        )
        try:
            await monitor.run_once()
            assert monitor.metrics.max_drawdown == Decimal("0.5")
            history = store.get_equity_history(0)
        finally:
            store.close()

        # 本轮快照已落库，历史峰值仍为 20000
        assert len(history) == 2
        assert history[-1]["total_equity"] == "10000"
        assert history[-1]["margin_ratio"] is None

    async def test_persist_failure_does_not_break_cycle(self, tmp_path) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = _settings()
        store = SQLiteStore(tmp_path / "quant.db")
        store.close()  # 模拟写库失败

        monitor = Monitor(
            _FakeMonitorClient(), redis, settings.monitor, store=store
        )

        alerts = await monitor.run_once()

        assert isinstance(alerts, list)


class TestMonitorAlertCooldown:
    async def test_repeated_rule_published_once_within_cooldown(
        self, tmp_path
    ) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        settings = _settings()
        monitor = Monitor(
            _FakeMonitorClient(), redis, settings.monitor, store=store
        )
        key = settings.monitor.dashboard_alerts_key
        try:
            await monitor.run_once()
            await monitor.run_once()
            await monitor.run_once()
            published = await redis.lrange(key, 0, -1)
        finally:
            store.close()

        # 三轮都命中 worker_heartbeat，但冷却窗口内只入列一次
        assert len(published) == 1

    async def test_cooldown_zero_publishes_every_round(self, tmp_path) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        settings = _settings()
        settings.monitor.dashboard_alert_cooldown_seconds = 0
        monitor = Monitor(
            _FakeMonitorClient(), redis, settings.monitor, store=store
        )
        try:
            await monitor.run_once()
            await monitor.run_once()
            published = await redis.lrange(
                settings.monitor.dashboard_alerts_key, 0, -1
            )
        finally:
            store.close()

        assert len(published) == 2

    async def test_kill_switch_bypasses_cooldown(self, tmp_path) -> None:
        """冷却窗口内的第二次激活也必须入列，否则看不到是谁停的。"""
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        settings = _settings()
        monitor = Monitor(
            _FakeMonitorClient(), redis, settings.monitor, store=store
        )
        now = int(time.time() * 1000)
        try:
            await monitor._publish_alerts(
                [Alert("kill_switch", "ERROR", "紧急停止已激活 reason=cli", now)]
            )
            await monitor._publish_alerts(
                [
                    Alert(
                        "kill_switch",
                        "ERROR",
                        "紧急停止已激活 reason=web",
                        now + 1_000,
                    )
                ]
            )
            published = await redis.lrange(
                settings.monitor.dashboard_alerts_key, 0, -1
            )
        finally:
            store.close()

        assert len(published) == 2


class _BrokenRedis:
    async def get(self, *_args, **_kwargs):
        raise RuntimeError("redis down")

    async def llen(self, *_args, **_kwargs):
        raise RuntimeError("redis down")

    async def lrange(self, *_args, **_kwargs):
        raise RuntimeError("redis down")

    async def exists(self, *_args, **_kwargs):
        raise RuntimeError("redis down")


class _FakeMonitorClient:
    def get_balance(self) -> dict:
        return {
            "totalEq": "10000",
            "upl": "0",
            "mgnRatio": "",
            "details": [
                {"ccy": "USDT", "availBal": "10000", "frozenBal": "0"}
            ],
        }

    def get_positions(self, inst_type: str) -> list:
        return []

    def get_error_rate(self):
        return None
