"""SQLiteStore 单元测试（spec §11）。"""
from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from quant.backtest.order import Fill, Order
from quant.backtest.position import Position
from quant.backtest.signal import Signal
from quant.state.sqlite_store import SCHEMA_VERSION, SQLiteStore


def _signal(signal_id: str = "sig-1") -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side="buy",
        order_type="market",
        inst_id="BTC-USDT",
        qty=Decimal("1"),
        ts=1_700_000_000_000,
        signal_id=signal_id,
    )


def _fill(
    side: str = "buy",
    price: str = "100",
    ts: int = 1_700_000_000_000,
    qty: str = "1",
    fee: str = "0",
) -> Fill:
    return Fill(
        order_id=f"o-{ts}-{side}",
        inst_id="BTC-USDT",
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        ts=ts,
    )


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "state" / "quant.db")
    yield s
    s.close()


class TestSignalPersistence:
    def test_save_and_get(self, store) -> None:
        signal = _signal()
        store.save_signal(signal, "pending")

        row = store.get_signal("sig-1")
        assert row is not None
        assert row["strategy_id"] == "sma_cross"
        assert row["status"] == "pending"
        assert row["qty"] == "1"

    def test_update_status(self, store) -> None:
        signal = _signal()
        store.save_signal(signal, "pending")
        store.save_signal(signal, "executed")

        assert store.get_signal_status("sig-1") == "executed"

    def test_missing_signal(self, store) -> None:
        assert store.get_signal("nope") is None
        assert store.get_signal_status("nope") is None


class TestOrderPersistence:
    def test_save_order_and_fill(self, store) -> None:
        signal = _signal()
        store.save_signal(signal, "executed")
        order = Order(
            order_id="o-1", signal=signal, status="filled",
            created_ts=1_700_000_000_000,
        )
        store.save_order(order)

        fill = _fill()
        store.update_order_fill("o-1", fill)
        store.save_fill(fill)

        trades = store.get_recent_trades(10)
        assert len(trades) == 1
        assert trades[0].price == Decimal("100")

    def test_recent_trades_limit_and_order(self, store) -> None:
        for i in range(5):
            store.save_fill(_fill(ts=1_700_000_000_000 + i))
        # 使用不同 order_id 避免主键冲突
        trades = store.get_recent_trades(3)

        assert len(trades) == 3
        assert [t.ts for t in trades] == [
            1_700_000_000_002,
            1_700_000_000_003,
            1_700_000_000_004,
        ]

    def test_update_order_status(self, store) -> None:
        signal = _signal()
        store.save_signal(signal, "executed")
        store.save_order(
            Order(
                order_id="o-1", signal=signal, status="pending",
                created_ts=1_700_000_000_000,
            )
        )
        store.update_order_status("o-1", "canceled")

        assert store.get_open_orders() == []

    def test_get_open_orders(self, store) -> None:
        signal = _signal("sig-1")
        store.save_signal(signal, "executed")
        store.save_order(
            Order(
                order_id="o-1", signal=signal, status="pending",
                created_ts=1_700_000_000_000,
            )
        )

        open_orders = store.get_open_orders()
        assert len(open_orders) == 1
        assert open_orders[0]["order_id"] == "o-1"
        assert open_orders[0]["inst_id"] == "BTC-USDT"
        assert open_orders[0]["status"] == "pending"
        assert open_orders[0]["created_ts"] == 1_700_000_000_000

    def test_open_orders_include_live_and_partial(self, store) -> None:
        """live / partially_filled 也必须在对账范围内。"""
        for order_id, status in (
            ("o-live", "live"),
            ("o-part", "partially_filled"),
        ):
            signal = _signal(f"sig-{order_id}")
            store.save_signal(signal, "executed")
            store.save_order(
                Order(
                    order_id=order_id, signal=signal, status=status,
                    created_ts=1_700_000_000_000,
                )
            )

        ids = {o["order_id"] for o in store.get_open_orders()}

        assert ids == {"o-live", "o-part"}

    def test_partial_fills_accumulate(self, store) -> None:
        """两次部分成交必须累加，不能后一次覆盖前一次。"""
        signal = _signal()
        store.save_signal(signal, "executed")
        store.save_order(
            Order(
                order_id="o-1", signal=signal, status="pending",
                created_ts=1_700_000_000_000,
            )
        )

        store.update_order_fill(
            "o-1", _fill(qty="0.4", price="100"),
            status="partially_filled",
        )
        store.update_order_fill(
            "o-1", _fill(qty="0.6", price="120", ts=1_700_000_001_000),
            status="filled",
        )

        state = store.get_order_fill_state("o-1")
        assert state["filled_qty"] == Decimal("1.0")
        # 加权均价 (0.4*100 + 0.6*120) / 1.0 = 112
        assert state["filled_price"] == Decimal("112")
        assert state["status"] == "filled"

    def test_illegal_transition_is_ignored(self, store) -> None:
        """终态不可回退：非法迁移只跳过，不抛异常。"""
        signal = _signal()
        store.save_signal(signal, "executed")
        store.save_order(
            Order(
                order_id="o-1", signal=signal, status="filled",
                created_ts=1_700_000_000_000,
            )
        )

        assert store.update_order_status("o-1", "canceled") is False
        assert store.get_order_fill_state("o-1")["status"] == "filled"

    def test_same_status_is_noop(self, store) -> None:
        signal = _signal()
        store.save_signal(signal, "executed")
        store.save_order(
            Order(
                order_id="o-1", signal=signal, status="pending",
                created_ts=1_700_000_000_000,
            )
        )

        assert store.update_order_status("o-1", "pending") is False

    def test_update_status_missing_order(self, store) -> None:
        assert store.update_order_status("nope", "canceled") is False


class TestPositionPersistence:
    def test_default_flat(self, store) -> None:
        position = store.get_position("BTC-USDT")
        assert position.inst_id == "BTC-USDT"
        assert position.qty == Decimal("0")

    def test_update_and_read(self, store) -> None:
        position = Position(
            inst_id="BTC-USDT",
            qty=Decimal("2"),
            avg_price=Decimal("100"),
            realized_pnl=Decimal("5"),
        )
        store.update_position(position)

        loaded = store.get_position("BTC-USDT")
        assert loaded.qty == Decimal("2")
        assert loaded.avg_price == Decimal("100")
        assert loaded.realized_pnl == Decimal("5")

    def test_get_all_positions(self, store) -> None:
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("1"), avg_price=Decimal("100"))
        )
        store.update_position(
            Position(inst_id="ETH-USDT", qty=Decimal("2"), avg_price=Decimal("10"))
        )

        positions = store.get_all_positions()
        assert {p.inst_id for p in positions} == {"BTC-USDT", "ETH-USDT"}


class TestPnlStatistics:
    def test_realized_pnl_round_trip(self, store) -> None:
        store.save_fill(_fill("buy", "100", ts=1000))
        store.save_fill(_fill("sell", "110", ts=2000))

        assert store.get_today_realized_pnl(0) == Decimal("10")

    def test_realized_pnl_with_fees(self, store) -> None:
        store.save_fill(_fill("buy", "100", ts=1000, fee="0.1"))
        store.save_fill(_fill("sell", "110", ts=2000, fee="0.1"))

        assert store.get_today_realized_pnl(0) == Decimal("9.8")

    def test_realized_pnl_since_filter(self, store) -> None:
        store.save_fill(_fill("buy", "100", ts=1000))
        store.save_fill(_fill("sell", "110", ts=2000))

        assert store.get_today_realized_pnl(0) == Decimal("10")
        # 只统计 sell 之后的成交 → 无完整回合
        assert store.get_today_realized_pnl(2000) == Decimal("0")

    def test_open_position_not_counted(self, store) -> None:
        store.save_fill(_fill("buy", "100", ts=1000))
        assert store.get_today_realized_pnl(0) == Decimal("0")


class TestSchemaCreation:
    def test_creates_parent_dir(self, tmp_path) -> None:
        path = tmp_path / "a" / "b" / "quant.db"
        store = SQLiteStore(path)
        store.close()
        assert path.exists()

    def test_reopen_existing_db(self, tmp_path) -> None:
        path = tmp_path / "quant.db"
        store = SQLiteStore(path)
        store.save_signal(_signal(), "executed")
        store.close()

        reopened = SQLiteStore(path)
        assert reopened.get_signal_status("sig-1") == "executed"
        reopened.close()


class TestMigrations:
    """结构迁移（MS4-A §4.1）：_SCHEMA 是 v1，_MIGRATIONS 从 v2 起。"""

    def _tables(self, path) -> set[str]:
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            return {r[0] for r in rows}
        finally:
            conn.close()

    def _indexes(self, path) -> set[str]:
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            return {r[0] for r in rows}
        finally:
            conn.close()

    def test_fresh_db_reaches_latest_version(self, tmp_path) -> None:
        path = tmp_path / "quant.db"
        SQLiteStore(path).close()

        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == (
                SCHEMA_VERSION
            )
        finally:
            conn.close()

    def test_fresh_db_tables(self, tmp_path) -> None:
        path = tmp_path / "quant.db"
        SQLiteStore(path).close()

        tables = self._tables(path)
        assert {"alerts", "alert_silences"} <= tables
        assert "heartbeat" not in tables

    def test_fresh_db_indexes(self, tmp_path) -> None:
        path = tmp_path / "quant.db"
        SQLiteStore(path).close()

        indexes = self._indexes(path)
        assert {
            "idx_alerts_created",
            "idx_alerts_state",
            "idx_alerts_rule",
            "idx_silences_active",
            "idx_signals_ts",
            "idx_orders_created",
            "idx_fills_ts",
            "idx_risk_events_ts",
            "idx_risk_events_rule",
            "idx_risk_events_signal",
            "idx_overrides_effective",
        } <= indexes

    def test_fresh_db_has_risk_tables(self, tmp_path) -> None:
        path = tmp_path / "quant.db"
        SQLiteStore(path).close()
        assert {"risk_events", "risk_overrides"} <= self._tables(path)

    def test_legacy_db_upgrades_and_keeps_data(self, tmp_path) -> None:
        """老库：user_version=0、6 张表且 heartbeat 有数据。"""
        path = tmp_path / "quant.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE signals (
                signal_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                inst_id TEXT NOT NULL,
                qty TEXT NOT NULL,
                price TEXT,
                ts INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE heartbeat (
                process TEXT PRIMARY KEY,
                ts INTEGER NOT NULL
            );
            INSERT INTO signals VALUES
                ('sig-old', 'sma_cross', 'buy', 'market',
                 'BTC-USDT', '1', NULL, 1000, 'executed', 1000);
            INSERT INTO heartbeat VALUES ('worker', 123);
            """
        )
        conn.commit()
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()

        store = SQLiteStore(path)
        try:
            assert store.get_signal_status("sig-old") == "executed"
        finally:
            store.close()

        assert "heartbeat" not in self._tables(path)
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == (
                SCHEMA_VERSION
            )
        finally:
            conn.close()

    def test_reopen_is_idempotent(self, tmp_path) -> None:
        path = tmp_path / "quant.db"
        store = SQLiteStore(path)
        store.save_signal(_signal(), "executed")
        store.close()

        for _ in range(3):
            store = SQLiteStore(path)
            assert store.get_signal_status("sig-1") == "executed"
            store.close()

        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == (
                SCHEMA_VERSION
            )
        finally:
            conn.close()


class TestRiskEvents:
    """risk_events 落库与查询（MS4-B §11.1）。"""

    def _event(self, **overrides) -> dict:
        row = {
            "event_id": "e1",
            "ts": 1000,
            "inst_id": "BTC-USDT",
            "signal_id": "sig-1",
            "decision": "rejected",
            "strategy_id": "sma_cross",
            "rule": "max_order_qty",
            "limit_name": "max_order_qty",
            "limit_value": "1000",
            "actual_value": "2000",
            "qty_before": "20",
            "qty_after": "0",
            "equity": "10000",
            "reason": "order notional 2000 exceeds max",
        }
        row.update(overrides)
        return row

    def test_insert_and_list(self, store) -> None:
        store.insert_risk_event(**self._event())

        rows = store.list_risk_events()
        assert len(rows) == 1
        assert rows[0]["rule"] == "max_order_qty"
        assert rows[0]["actual_value"] == "2000"

    def test_duplicate_event_id_is_ignored(self, store) -> None:
        store.insert_risk_event(**self._event())
        store.insert_risk_event(**self._event(reason="changed"))

        rows = store.list_risk_events()
        assert len(rows) == 1
        assert rows[0]["reason"] == "order notional 2000 exceeds max"

    def test_filters(self, store) -> None:
        store.insert_risk_event(**self._event(event_id="e1", ts=1000))
        store.insert_risk_event(
            **self._event(
                event_id="e2",
                ts=2000,
                decision="adjusted",
                rule="max_position_pct",
            )
        )

        assert len(store.list_risk_events(decision="adjusted")) == 1
        assert len(store.list_risk_events(rule="max_order_qty")) == 1
        assert len(store.list_risk_events(since=1500)) == 1
        assert store.count_risk_events() == 2
        assert store.count_risk_events(strategy_id="other") == 0

    def test_newest_first_and_paging(self, store) -> None:
        for i in range(3):
            store.insert_risk_event(
                **self._event(event_id=f"e{i}", ts=1000 + i)
            )

        rows = store.list_risk_events(limit=2)
        assert [r["event_id"] for r in rows] == ["e2", "e1"]
        assert [r["event_id"] for r in store.list_risk_events(limit=2, offset=2)] == [
            "e0"
        ]


class TestEffectiveOverrides:
    """risk_overrides 的「最新未撤销行即生效」（MS4-B §11.2）。"""

    def _write(self, store, *, param: str = "max_position_pct", value: str,
               at: int = 1000, revoked: int | None = None,
               strategy_id: str = "sma_cross") -> None:
        store._execute(  # noqa: SLF001 - 写入 API 属于后续切片
            """
            INSERT INTO risk_overrides
                (strategy_id, param, old_value, new_value, reason, source,
                 changed_by, changed_at, revoked_at)
            VALUES (?, ?, NULL, ?, 'test', 'cli', 'tester', ?, ?)
            """,
            (strategy_id, param, value, at, revoked),
        )

    def test_latest_row_wins(self, store) -> None:
        self._write(store, value="0.2", at=1000, revoked=1500)
        self._write(store, value="0.4", at=2000)
        self._write(store, value="0.5", at=3000)

        assert store.get_effective_overrides("sma_cross") == {
            "sma_cross:max_position_pct": "0.5"
        }

    def test_all_revoked_means_no_override(self, store) -> None:
        """全部撤销 = 无覆盖，回落 ②③ 取严（不能冻结配置值）。"""
        self._write(store, value="0.4", at=1000, revoked=2000)
        assert store.get_effective_overrides("sma_cross") == {}

    def test_strategy_isolation(self, store) -> None:
        self._write(store, value="0.2", strategy_id="a")
        self._write(store, value="0.4", strategy_id="b")

        assert store.get_effective_overrides("a") == {"a:max_position_pct": "0.2"}
        assert store.get_effective_overrides("b") == {"b:max_position_pct": "0.4"}
        assert store.get_effective_overrides() == {
            "a:max_position_pct": "0.2",
            "b:max_position_pct": "0.4",
        }


class TestAlerts:
    """alerts 落库与状态机（MS4-A §4.2 / §5.3）。"""

    def _insert(self, store, alert_id: str = "a1", **kwargs) -> bool:
        params = {
            "rule": "margin_ratio",
            "level": "ERROR",
            "message": "保证金率过低",
            "fingerprint": "fp",
            "created_at": 1000,
        }
        params.update(kwargs)
        return store.insert_alert(alert_id, **params)

    def test_insert_and_list(self, store) -> None:
        self._insert(store)

        rows, total = store.list_alerts(10)

        assert total == 1
        assert rows[0]["alert_id"] == "a1"
        assert rows[0]["state"] == "open"
        assert rows[0]["suppressed"] == 0

    def test_duplicate_id_is_ignored(self, store) -> None:
        assert self._insert(store) is True
        assert self._insert(store) is False
        assert store.list_alerts(10)[1] == 1

    def test_newest_first(self, store) -> None:
        self._insert(store, "a1", created_at=1000)
        self._insert(store, "a2", created_at=2000)

        rows, _ = store.list_alerts(10)

        assert [r["alert_id"] for r in rows] == ["a2", "a1"]

    def test_filters_share_where_clause(self, store) -> None:
        self._insert(store, "a1", rule="margin_ratio", created_at=1000)
        self._insert(store, "a2", rule="queue_backlog", level="WARNING",
                     created_at=2000)
        store.resolve_alerts("queue_backlog", inst_id=None, now_ms=3000)

        rows, total = store.list_alerts(10, rule="queue_backlog", state="resolved")

        assert total == 1
        assert rows[0]["alert_id"] == "a2"

    def test_time_range(self, store) -> None:
        self._insert(store, "a1", created_at=1000)
        self._insert(store, "a2", created_at=2000)

        rows, total = store.list_alerts(10, since_ms=1500)

        assert total == 1
        assert rows[0]["alert_id"] == "a2"

    def test_ack_is_idempotent(self, store) -> None:
        self._insert(store)

        assert store.ack_alert("a1", "alice", 5000) is True
        row1 = store.list_alerts(10)[0][0]
        assert store.ack_alert("a1", "bob", 6000) is True
        row2 = store.list_alerts(10)[0][0]

        assert row1["state"] == "acked"
        assert row2["acked_by"] == "alice"
        assert row2["acked_at"] == 5000

    def test_ack_unknown(self, store) -> None:
        assert store.ack_alert("nope", "alice", 1) is False

    def test_resolve_scopes_by_inst(self, store) -> None:
        self._insert(store, "a1", inst_id="BTC-USDT")
        self._insert(store, "a2", inst_id="ETH-USDT")
        self._insert(store, "a3", inst_id=None)

        assert store.resolve_alerts(
            "margin_ratio", inst_id="BTC-USDT", now_ms=9000
        ) == 1
        assert store.resolve_alerts(
            "margin_ratio", inst_id=None, now_ms=9000
        ) == 1

        states = {r["alert_id"]: r["state"] for r in store.list_alerts(10)[0]}
        assert states == {"a1": "resolved", "a2": "open", "a3": "resolved"}

    def test_resolve_leaves_resolved_untouched(self, store) -> None:
        self._insert(store)
        store.resolve_alerts("margin_ratio", inst_id=None, now_ms=1000)

        assert store.resolve_alerts("margin_ratio", inst_id=None, now_ms=2000) == 0


class TestAlertSilences:
    def _insert(self, store, silence_id: str = "s1", **kwargs) -> None:
        params = {
            "rule": "margin_ratio",
            "inst_id": None,
            "until_ts": 5000,
            "reason": "维护",
            "created_by": "owner",
            "created_at": 1000,
        }
        params.update(kwargs)
        store.insert_silence(silence_id, params.pop("rule"), **params)

    def test_active_before_until(self, store) -> None:
        self._insert(store)

        assert len(store.list_active_silences(4000)) == 1
        assert store.list_active_silences(5000) == []

    def test_revoke(self, store) -> None:
        self._insert(store)

        assert store.revoke_silence("s1", 2000) is True
        assert store.list_active_silences(3000) == []
        # 重复解除返回 False
        assert store.revoke_silence("s1", 3000) is False


class TestStrategyParams:
    def test_set_and_get(self, store) -> None:
        assert store.set_strategy_param("sma_cross", "fast_len", "8", 1000)

        rows = store.get_strategy_params("sma_cross")

        assert rows == [{"param": "fast_len", "value": "8", "updated_at": 1000}]

    def test_same_value_is_noop(self, store) -> None:
        store.set_strategy_param("sma_cross", "fast_len", "8", 1000)

        assert store.set_strategy_param("sma_cross", "fast_len", "8", 2000) is False
        assert store.list_param_changes("sma_cross")[1] == 1

    def test_change_history_records_old_and_new(self, store) -> None:
        store.set_strategy_param("sma_cross", "fast_len", "8", 1000, reason="调参")
        store.set_strategy_param(
            "sma_cross", "fast_len", "12", 2000,
            reason="再调", changed_by="alice", git_commit="abc123",
        )

        rows, total = store.list_param_changes("sma_cross")

        assert total == 2
        assert rows[0]["new_value"] == "12"
        assert rows[0]["old_value"] == "8"
        assert rows[0]["changed_by"] == "alice"
        assert rows[0]["git_commit"] == "abc123"
        assert rows[1]["old_value"] is None

    def test_strategy_isolation(self, store) -> None:
        store.set_strategy_param("sma_cross", "fast_len", "8", 1000)
        store.set_strategy_param("rsi", "fast_len", "9", 1000)

        assert len(store.get_strategy_params("sma_cross")) == 1
        assert store.get_strategy_params("sma_cross")[0]["value"] == "8"


class TestBacktestRuns:
    def _create(self, store, run_id: str = "r1", **kwargs) -> None:
        params = {
            "strategy_id": "sma_cross",
            "inst_id": "BTC-USDT",
            "bar": "1H",
            "start_ts": 1000,
            "end_ts": 2000,
            "params": '{"fast_len": 5}',
            "kind": "backtest",
            "status": "pending",
            "created_at": 3000,
        }
        params.update(kwargs)
        store.create_backtest_run(run_id, **params)

    def test_create_and_get(self, store) -> None:
        self._create(store)

        row = store.get_backtest_run("r1")

        assert row["status"] == "pending"
        assert row["warmup_bars"] == 200
        assert store.get_backtest_run("nope") is None

    def test_update_status(self, store) -> None:
        self._create(store)

        assert store.update_run_status(
            "r1", "running", started_at=4000
        ) is True
        row = store.get_backtest_run("r1")
        assert row["status"] == "running"
        assert row["started_at"] == 4000
        assert store.update_run_status("nope", "done") is False

    def test_fail_interrupted_only_touches_unfinished(self, store) -> None:
        self._create(store, "a", status="running")
        self._create(store, "b", status="pending")
        self._create(store, "c", status="done")

        assert store.fail_interrupted_runs(9000) == 2

        assert store.get_backtest_run("a")["error"] == "interrupted"
        assert store.get_backtest_run("b")["finished_at"] == 9000
        assert store.get_backtest_run("c")["error"] is None

    def test_list_filters_share_where(self, store) -> None:
        self._create(store, "a", strategy_id="sma_cross", created_at=1000)
        self._create(store, "b", strategy_id="rsi", bar="4H", created_at=2000)
        store.update_run_status("b", "done")

        rows, total = store.list_backtest_runs(
            10, strategy_id="rsi", status="done"
        )

        assert total == 1
        assert rows[0]["run_id"] == "b"

    def test_list_pagination_and_order(self, store) -> None:
        for i, run_id in enumerate(("a", "b", "c")):
            self._create(store, run_id, created_at=1000 + i)

        rows, total = store.list_backtest_runs(2, offset=1)

        assert total == 3
        assert [r["run_id"] for r in rows] == ["b", "a"]

    def test_count_runs_with_prefix(self, store) -> None:
        self._create(store, "20261031-sma_cross-BTC-USDT-001")
        self._create(store, "20261031-sma_cross-BTC-USDT-002")
        self._create(store, "20261030-sma_cross-BTC-USDT-001")

        assert store.count_runs_with_prefix("20261031-sma_cross-BTC-USDT-") == 2

    def test_metrics_roundtrip(self, store) -> None:
        self._create(store)

        store.save_backtest_metrics(
            "r1", {"sharpe": "1.5", "trade_count": 4, "unknown": "x"}
        )
        store.save_backtest_metrics("r1", {"sharpe": "2.0"})

        row = store.get_backtest_metrics("r1")
        assert row["sharpe"] == "2.0"
        assert row["trade_count"] == 4
        assert "unknown" not in row

    def test_metrics_empty_is_noop(self, store) -> None:
        self._create(store)

        store.save_backtest_metrics("r1", {"unknown": 1})

        assert store.get_backtest_metrics("r1") is None

    def test_artifacts(self, store) -> None:
        self._create(store)

        store.add_backtest_artifact("r1", "equity", "/tmp/e.parquet", 10, 5000)
        store.add_backtest_artifact("r1", "trades", "/tmp/t.parquet", 20, 5000)

        rows = store.list_backtest_artifacts("r1")

        assert [r["kind"] for r in rows] == ["equity", "trades"]
        assert rows[0]["size"] == 10
