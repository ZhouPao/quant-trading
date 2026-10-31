"""Reconciler 对账测试（F3.7）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.order import Order
from quant.backtest.position import Position
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
        ct_vals: dict[str, str] | None = None,
    ) -> None:
        self.orders = orders
        self.cancel_codes = cancel_codes or {}
        self.ct_vals = ct_vals or {}
        self.cancel_calls: list[list[tuple[str, str]]] = []

    async def get_order(self, inst_id: str, ord_id: str) -> dict:
        return self.orders.get(ord_id, {})

    async def ct_val(self, inst_id: str) -> Decimal:
        """对账用面值；默认按现货 1 处理。"""
        return Decimal(self.ct_vals.get(inst_id, "1"))

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


# ---------------------------------------------------------------------------
# 三项对账（M8-18）：持仓 / 余额 / 差异落库与告警
# ---------------------------------------------------------------------------


class FakeClient:
    """对账用的账户/持仓接口；`fail` 可模拟整体不可用。"""

    def __init__(
        self,
        positions: dict[str, list[dict]] | None = None,
        balance: dict | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.positions = positions or {}
        self.balance = balance or {"totalEq": "0", "details": []}
        self.fail = fail

    def get_positions(self, inst_type: str = "SWAP") -> list[dict]:
        if self.fail:
            raise RuntimeError("positions unavailable")
        return self.positions.get(inst_type, [])

    def get_balance(self) -> dict:
        if self.fail:
            raise RuntimeError("balance unavailable")
        return self.balance


class RecordingAlerter:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> str:
        self.events.append(event)
        return f"a-{len(self.events)}"


def _open_rows(store, kind: str | None = None) -> list[dict]:
    rows, _total = store.list_reconciliations(kind=kind, only_open=True)
    return rows


@pytest.mark.asyncio
async def test_position_excess_is_auto_fixed(store) -> None:
    """本地多于交易所：自动下调，避免卖出不存在的仓位。"""
    store.update_position(
        Position(
            inst_id="BTC-USDT",
            qty=Decimal("0.5"),
            avg_price=Decimal("60000"),
            realized_pnl=Decimal("0"),
        )
    )
    client = FakeClient(
        balance={"totalEq": "1000", "details": [{"ccy": "BTC", "cashBal": "0.2"}]}
    )
    reconciler = Reconciler(store, FakeBroker({}), client=client)

    await reconciler.reconcile(now_ms=1_700_000_000_000)

    assert store.get_position("BTC-USDT").qty == Decimal("0.2")
    assert [d["severity"] for d in reconciler.discrepancies] == ["auto"]
    rows, _ = store.list_reconciliations(kind="position")
    assert len(rows) == 1
    assert rows[0]["severity"] == "auto"
    assert rows[0]["resolved"] == 1
    assert rows[0]["local_value"] == "0.5"
    assert rows[0]["remote_value"] == "0.2"


@pytest.mark.asyncio
async def test_position_shortfall_is_manual_and_alerts_once(store) -> None:
    """本地少于交易所：不自动加仓，记录待人工确认，只告警一次。"""
    store.update_position(
        Position(
            inst_id="ETH-USDT",
            qty=Decimal("1"),
            avg_price=Decimal("2000"),
            realized_pnl=Decimal("0"),
        )
    )
    client = FakeClient(
        balance={"totalEq": "1000", "details": [{"ccy": "ETH", "cashBal": "2"}]}
    )
    alerter = RecordingAlerter()
    reconciler = Reconciler(
        store, FakeBroker({}), client=client, alert_service=alerter
    )

    await reconciler.reconcile(now_ms=1_700_000_000_000)
    await reconciler.reconcile(now_ms=1_700_000_060_000)

    # 本地不动（外部转入不按策略成本价入账）
    assert store.get_position("ETH-USDT").qty == Decimal("1")
    rows = _open_rows(store)
    assert len(rows) == 1
    assert rows[0]["severity"] == "manual"
    assert rows[0]["local_value"] == "1"
    assert rows[0]["remote_value"] == "2"
    # 持续存在的同一差异不重复告警
    assert len(alerter.events) == 1
    assert alerter.events[0].rule == "reconcile_mismatch"


@pytest.mark.asyncio
async def test_contract_position_remote_only_is_manual(store) -> None:
    """交易所有合约持仓而本地没有：必须人工确认。"""
    client = FakeClient(
        positions={
            "SWAP": [
                {"instId": "BTC-USDT-SWAP", "pos": "5"},
                {"instId": "ETH-USDT-SWAP", "pos": "0"},
            ]
        }
    )
    broker = FakeBroker({}, ct_vals={"BTC-USDT-SWAP": "0.01"})
    reconciler = Reconciler(store, broker, client=client)

    await reconciler.reconcile(now_ms=1_700_000_000_000)

    rows = _open_rows(store, kind="position")
    assert len(rows) == 1
    assert rows[0]["key"] == "BTC-USDT-SWAP"
    assert rows[0]["remote_value"] == "0.05"
    assert rows[0]["severity"] == "manual"


@pytest.mark.asyncio
async def test_unknown_spot_coin_is_ignored(store) -> None:
    """本地从未记账的现货币视为用户自持，不参与比对。"""
    client = FakeClient(
        balance={"totalEq": "1000", "details": [{"ccy": "DOGE", "cashBal": "100"}]}
    )
    reconciler = Reconciler(store, FakeBroker({}), client=client)

    await reconciler.reconcile(now_ms=1_700_000_000_000)

    assert reconciler.discrepancies == []
    assert _open_rows(store) == []


@pytest.mark.asyncio
async def test_balance_mismatch_recorded_as_manual(store) -> None:
    store.save_equity_snapshot(
        ts=1_699_999_999_000,
        total_equity=Decimal("10000"),
        avail_usdt=Decimal("9000"),
        frozen_usdt=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        margin_ratio=None,
    )
    client = FakeClient(balance={"totalEq": "9000", "details": []})
    reconciler = Reconciler(store, FakeBroker({}), client=client)

    await reconciler.reconcile(now_ms=1_700_000_000_000)

    rows = _open_rows(store, kind="balance")
    assert len(rows) == 1
    assert rows[0]["key"] == "total_equity"
    assert rows[0]["local_value"] == "10000"
    assert rows[0]["remote_value"] == "9000"
    assert rows[0]["diff"] == "1000"
    assert rows[0]["severity"] == "manual"


@pytest.mark.asyncio
async def test_balance_skipped_when_snapshot_is_stale(store) -> None:
    """快照过旧说明本地视图不可信，跳过而不是报一个假差异。"""
    store.save_equity_snapshot(
        ts=1_600_000_000_000,
        total_equity=Decimal("10000"),
        avail_usdt=Decimal("9000"),
        frozen_usdt=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        margin_ratio=None,
    )
    client = FakeClient(balance={"totalEq": "9000", "details": []})
    reconciler = Reconciler(
        store, FakeBroker({}), client=client, snapshot_max_age_ms=60_000
    )

    await reconciler.reconcile(now_ms=1_700_000_000_000)

    assert _open_rows(store, kind="balance") == []


@pytest.mark.asyncio
async def test_client_failure_keeps_local_positions(store) -> None:
    """接口整体失败不能当成「交易所没有持仓」而清空本地记账。"""
    store.update_position(
        Position(
            inst_id="BTC-USDT",
            qty=Decimal("0.5"),
            avg_price=Decimal("60000"),
            realized_pnl=Decimal("0"),
        )
    )
    reconciler = Reconciler(
        store, FakeBroker({}), client=FakeClient(fail=True)
    )

    await reconciler.reconcile(now_ms=1_700_000_000_000)

    assert store.get_position("BTC-USDT").qty == Decimal("0.5")
    assert reconciler.discrepancies == []


@pytest.mark.asyncio
async def test_canceled_order_records_auto_discrepancy(store) -> None:
    _seed_pending(store, "o-1")
    broker = FakeBroker({"o-1": {"ordId": "o-1", "state": "canceled"}})
    reconciler = Reconciler(store, broker)

    await reconciler.reconcile(now_ms=1_700_000_000_000)

    rows, _ = store.list_reconciliations(kind="order")
    assert len(rows) == 1
    assert rows[0]["severity"] == "auto"
    assert rows[0]["resolved"] == 1
    assert rows[0]["remote_value"] == "canceled"


@pytest.mark.asyncio
async def test_resolved_discrepancy_reopens_on_recurrence(store) -> None:
    """用户标记处理后同一差异再次出现，应新增一条待办。"""
    store.update_position(
        Position(
            inst_id="ETH-USDT",
            qty=Decimal("1"),
            avg_price=Decimal("2000"),
            realized_pnl=Decimal("0"),
        )
    )
    client = FakeClient(
        balance={"totalEq": "1000", "details": [{"ccy": "ETH", "cashBal": "2"}]}
    )
    reconciler = Reconciler(store, FakeBroker({}), client=client)

    await reconciler.reconcile(now_ms=1_700_000_000_000)
    rec_id = _open_rows(store)[0]["rec_id"]
    assert store.resolve_reconciliation(
        rec_id, by="tester", now_ms=1_700_000_001_000
    )

    await reconciler.reconcile(now_ms=1_700_000_060_000)

    rows = _open_rows(store)
    assert len(rows) == 1
    assert rows[0]["rec_id"] != rec_id
