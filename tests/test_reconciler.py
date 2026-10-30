"""Reconciler 对账测试（F3.7）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.order import Order
from quant.backtest.signal import Signal
from quant.execution.reconciler import Reconciler
from quant.state.sqlite_store import SQLiteStore


def _signal(signal_id: str, inst_id: str = "BTC-USDT") -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side="buy",
        order_type="market",
        inst_id=inst_id,
        qty=Decimal("1"),
        ts=1_700_000_000_000,
        signal_id=signal_id,
    )


def _filled_detail(
    side: str = "buy", qty: str = "0.5", px: str = "65000"
) -> dict:
    return {
        "ordId": "o-1",
        "instId": "BTC-USDT",
        "state": "filled",
        "side": side,
        "accFillSz": qty,
        "avgPx": px,
        "fee": "-32.5",
        "fillTime": "1700000001000",
    }


class FakeBroker:
    def __init__(
        self,
        orders: dict[str, dict],
        cancel_codes: dict[str, str] | None = None,
    ) -> None:
        self.orders = orders
        self.cancel_codes = cancel_codes or {}
        self.cancel_calls: list[list[tuple[str, str]]] = []

    async def get_order(self, inst_id: str, ord_id: str) -> dict:
        return self.orders.get(ord_id, {})

    async def ct_val(self, inst_id: str) -> Decimal:
        """对账用面值；测试里都是现货，返回 1。"""
        return Decimal("1")

    async def cancel_orders(
        self, orders: list[tuple[str, str]]
    ) -> dict[str, str]:
        self.cancel_calls.append(list(orders))
        return {
            ord_id: self.cancel_codes.get(ord_id, "0")
            for _inst_id, ord_id in orders
        }


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "quant.db")
    yield s
    s.close()


def _seed_pending(store, order_id: str = "o-1") -> None:
    signal = _signal(signal_id=f"sig-{order_id}")
    store.save_signal(signal, "executed")
    store.save_order(
        Order(
            order_id=order_id,
            signal=signal,
            status="pending",
            created_ts=1_700_000_000_000,
        )
    )


@pytest.mark.asyncio
async def test_reconcile_filled_order(store) -> None:
    _seed_pending(store, "o-1")
    broker = FakeBroker({"o-1": _filled_detail()})
    reconciler = Reconciler(store, broker)

    mismatches = await reconciler.reconcile()

    assert mismatches == 1
    assert store.get_open_orders() == []
    trades = store.get_recent_trades(10)
    assert len(trades) == 1
    assert trades[0].qty == Decimal("0.5")
    assert trades[0].side == "buy"
    position = store.get_position("BTC-USDT")
    assert position.qty == Decimal("0.5")
    assert position.avg_price == Decimal("65000")


@pytest.mark.asyncio
async def test_reconcile_canceled_order(store) -> None:
    _seed_pending(store, "o-1")
    broker = FakeBroker({"o-1": {"ordId": "o-1", "state": "canceled"}})
    reconciler = Reconciler(store, broker)

    mismatches = await reconciler.reconcile()

    assert mismatches == 1
    assert store.get_open_orders() == []


@pytest.mark.asyncio
async def test_reconcile_still_open_keeps_pending(store) -> None:
    _seed_pending(store, "o-1")
    broker = FakeBroker({"o-1": {"ordId": "o-1", "state": "live"}})
    reconciler = Reconciler(store, broker)

    mismatches = await reconciler.reconcile()

    assert mismatches == 0
    assert len(store.get_open_orders()) == 1


@pytest.mark.asyncio
async def test_reconcile_no_open_orders(store) -> None:
    reconciler = Reconciler(store, FakeBroker({}))

    assert await reconciler.reconcile() == 0


@pytest.mark.asyncio
async def test_reconcile_timeout_cancels_stale_order(store) -> None:
    _seed_pending(store, "o-1")
    broker = FakeBroker({"o-1": {"ordId": "o-1", "state": "live"}})
    reconciler = Reconciler(store, broker, order_timeout_seconds=60)

    changed = await reconciler.reconcile(now_ms=1_700_000_060_000)

    assert changed == 1
    assert broker.cancel_calls == [[("BTC-USDT", "o-1")]]
    assert store.get_open_orders() == []


@pytest.mark.asyncio
async def test_reconcile_timeout_batches_multiple_orders(store) -> None:
    _seed_pending(store, "o-1")
    _seed_pending(store, "o-2")
    broker = FakeBroker(
        {
            "o-1": {"ordId": "o-1", "state": "live"},
            "o-2": {"ordId": "o-2", "state": "partially_filled"},
        }
    )
    reconciler = Reconciler(store, broker, order_timeout_seconds=60)

    changed = await reconciler.reconcile(now_ms=1_700_000_120_000)

    assert changed == 2
    # 两笔挂单合并为一次批量撤单请求
    assert len(broker.cancel_calls) == 1
    assert set(broker.cancel_calls[0]) == {("BTC-USDT", "o-1"), ("BTC-USDT", "o-2")}
    assert store.get_open_orders() == []


@pytest.mark.asyncio
async def test_reconcile_before_timeout_keeps_pending(store) -> None:
    _seed_pending(store, "o-1")
    broker = FakeBroker({"o-1": {"ordId": "o-1", "state": "live"}})
    reconciler = Reconciler(store, broker, order_timeout_seconds=60)

    changed = await reconciler.reconcile(now_ms=1_700_000_059_999)

    assert changed == 0
    assert broker.cancel_calls == []
    assert len(store.get_open_orders()) == 1


@pytest.mark.asyncio
async def test_reconcile_timeout_disabled_keeps_pending(store) -> None:
    _seed_pending(store, "o-1")
    broker = FakeBroker({"o-1": {"ordId": "o-1", "state": "live"}})
    reconciler = Reconciler(store, broker, order_timeout_seconds=0)

    changed = await reconciler.reconcile(now_ms=1_800_000_000_000)

    assert changed == 0
    assert broker.cancel_calls == []
    assert len(store.get_open_orders()) == 1


@pytest.mark.asyncio
async def test_reconcile_cancel_failure_keeps_pending(store) -> None:
    _seed_pending(store, "o-1")
    broker = FakeBroker(
        {"o-1": {"ordId": "o-1", "state": "live"}},
        cancel_codes={"o-1": "51400"},
    )
    reconciler = Reconciler(store, broker, order_timeout_seconds=60)

    changed = await reconciler.reconcile(now_ms=1_700_000_060_000)

    assert changed == 0
    assert len(store.get_open_orders()) == 1


def test_reconciler_rejects_negative_timeout(store) -> None:
    with pytest.raises(ValueError):
        Reconciler(store, FakeBroker({}), order_timeout_seconds=-1)


def _partial_detail(qty: str, state: str = "partially_filled") -> dict:
    detail = _filled_detail(qty=qty)
    detail["state"] = state
    return detail


@pytest.mark.asyncio
async def test_partial_fill_recorded_as_partially_filled(store) -> None:
    _seed_pending(store, "o-1")
    broker = FakeBroker({"o-1": _partial_detail("0.2")})
    reconciler = Reconciler(store, broker)

    changed = await reconciler.reconcile()

    assert changed == 1
    state = store.get_order_fill_state("o-1")
    assert state["status"] == "partially_filled"
    assert state["filled_qty"] == Decimal("0.2")
    assert store.get_position("BTC-USDT").qty == Decimal("0.2")
    # 部分成交仍需继续对账
    assert len(store.get_open_orders()) == 1


@pytest.mark.asyncio
async def test_partial_fills_use_increment_not_cumulative(store) -> None:
    """OKX 的 accFillSz 是累计值，落本地时必须相减。"""
    _seed_pending(store, "o-1")
    broker = FakeBroker({"o-1": _partial_detail("0.2")})
    reconciler = Reconciler(store, broker)
    await reconciler.reconcile(now_ms=1_700_000_100_000)

    # 第二轮远端累计成交 0.5 → 本次增量应为 0.3
    broker.orders["o-1"] = _partial_detail("0.5", state="filled")
    await reconciler.reconcile(now_ms=1_700_000_200_000)

    state = store.get_order_fill_state("o-1")
    assert state["status"] == "filled"
    assert state["filled_qty"] == Decimal("0.5")
    assert store.get_position("BTC-USDT").qty == Decimal("0.5")
    assert store.get_open_orders() == []
    # 两条成交明细，合计 0.5，不是 0.2 + 0.5
    trades = store.get_recent_trades(10)
    assert sum((t.qty for t in trades), Decimal("0")) == Decimal("0.5")


@pytest.mark.asyncio
async def test_repeated_reconcile_does_not_double_count(store) -> None:
    _seed_pending(store, "o-1")
    broker = FakeBroker({"o-1": _filled_detail(qty="0.5")})
    reconciler = Reconciler(store, broker)
    await reconciler.reconcile()

    # 订单已终结，第二轮不应再进对账集合
    changed = await reconciler.reconcile()

    assert changed == 0
    assert store.get_order_fill_state("o-1")["filled_qty"] == Decimal("0.5")
