"""SQLiteStore 单元测试（spec §11）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.order import Fill, Order
from quant.backtest.position import Position
from quant.backtest.signal import Signal
from quant.state.sqlite_store import SQLiteStore


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


class TestHeartbeat:
    def test_save_and_get(self, store) -> None:
        assert store.get_heartbeat("worker") is None

        store.save_heartbeat("worker", 1_700_000_000_000)
        assert store.get_heartbeat("worker") == 1_700_000_000_000

    def test_overwrite(self, store) -> None:
        store.save_heartbeat("worker", 1)
        store.save_heartbeat("worker", 2)
        assert store.get_heartbeat("worker") == 2

    def test_separate_processes(self, store) -> None:
        store.save_heartbeat("worker", 1)
        store.save_heartbeat("monitor", 2)
        assert store.get_heartbeat("worker") == 1
        assert store.get_heartbeat("monitor") == 2


class TestSchemaCreation:
    def test_creates_parent_dir(self, tmp_path) -> None:
        path = tmp_path / "a" / "b" / "quant.db"
        store = SQLiteStore(path)
        store.close()
        assert path.exists()

    def test_reopen_existing_db(self, tmp_path) -> None:
        path = tmp_path / "quant.db"
        store = SQLiteStore(path)
        store.save_heartbeat("worker", 42)
        store.close()

        reopened = SQLiteStore(path)
        assert reopened.get_heartbeat("worker") == 42
        reopened.close()
