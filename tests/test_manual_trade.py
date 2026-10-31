"""看板手动减仓/全平（dashboard.manual）单元测试。"""
from __future__ import annotations

import json
import time
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest

from quant.backtest.order import Fill, Order
from quant.backtest.position import Position
from quant.backtest.signal import Signal
from quant.dashboard.app import create_app
from quant.dashboard.manual import ManualTradeError, ManualTrader, floor_to
from quant.dashboard.service import DashboardService
from quant.execution.broker import ExecutionResult
from quant.execution.kill_switch import KillSwitch
from quant.foundation.config import OKXSettings, Settings
from quant.state.sqlite_store import SQLiteStore

NOW = int(time.time() * 1000)


def _settings() -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True)
    )


def _spot(inst_id: str = "BTC-USDT", lot: str = "0.00001", min_sz: str = "0.00001"):
    return {
        "instId": inst_id,
        "instType": "SPOT",
        "baseCcy": "BTC",
        "quoteCcy": "USDT",
        "tickSz": "0.1",
        "lotSz": lot,
        "minSz": min_sz,
        "state": "live",
    }


def _swap(
    inst_id: str = "BTC-USDT-SWAP",
    ct_val: str = "0.01",
    lot: str = "1",
    min_sz: str = "1",
):
    return {
        "instId": inst_id,
        "instType": "SWAP",
        "ctVal": ct_val,
        "tickSz": "0.1",
        "lotSz": lot,
        "minSz": min_sz,
        "state": "live",
    }


class FakeClient:
    """只实现 ManualTrader 用到的几个方法。"""

    def __init__(
        self,
        instruments,
        balances=None,
        positions=None,
        last_price: str = "100",
    ) -> None:
        self.instruments = {item["instId"]: item for item in instruments}
        self.balances = balances or []
        self.positions = positions or []
        self.last_price = last_price

    def get(self, path: str, params: dict | None = None):
        if path == "/api/v5/market/ticker":
            return [{"instId": (params or {}).get("instId"), "last": self.last_price}]
        assert path == "/api/v5/public/instruments"
        inst_id = (params or {}).get("instId")
        inst_type = (params or {}).get("instType")
        raw = self.instruments.get(inst_id)
        if raw and raw["instType"] == inst_type:
            return [raw]
        return []

    def get_instrument(self, inst_id: str):
        """与 OKXClient 同语义：查不到返回 None。"""
        from quant.okx.client import _instrument_from_raw

        raw = self.instruments.get(inst_id)
        return _instrument_from_raw(raw) if raw else None

    def get_balance(self) -> dict:
        return {"details": self.balances}

    def get_positions(self, inst_type: str = "SPOT") -> list[dict]:
        return [p for p in self.positions if p["instType"] == inst_type]


class FakeBroker:
    def __init__(
        self,
        price: str = "100",
        canceled: int = 0,
        cancel_error: Exception | None = None,
    ) -> None:
        self.price = Decimal(price)
        self.calls: list[dict] = []
        self.canceled = canceled
        self.cancel_error = cancel_error
        # M8-17：撤单/改单/远端状态
        self.canceled_orders: list[tuple[str, str]] = []
        self.cancel_order_error: Exception | None = None
        self.remote_state: str | None = None
        self.amended: list[dict] = []

    async def cancel_all_open_orders(self) -> int:
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.canceled

    async def cancel_order(self, inst_id: str, ord_id: str) -> None:
        if self.cancel_order_error is not None:
            raise self.cancel_order_error
        self.canceled_orders.append((inst_id, ord_id))

    async def get_order(self, inst_id: str, ord_id: str) -> dict:
        if self.remote_state is None:
            raise RuntimeError("order not found")
        return {"instId": inst_id, "ordId": ord_id, "state": self.remote_state}

    async def amend_order(
        self,
        inst_id: str,
        ord_id: str,
        new_size: Decimal | None = None,
        new_price: Decimal | None = None,
    ) -> dict:
        self.amended.append(
            {
                "inst_id": inst_id,
                "order_id": ord_id,
                "new_size": new_size,
                "new_price": new_price,
            }
        )
        return {"ordId": ord_id}

    async def execute(self, signal, qty, market_price=None, reduce_only=False):
        self.calls.append(
            {
                "signal": signal,
                "qty": qty,
                "reduce_only": reduce_only,
            }
        )
        fill = Fill(
            order_id="o-manual",
            inst_id=signal.inst_id,
            side=signal.side,
            qty=qty,
            price=self.price,
            fee=Decimal("0.1"),
            ts=NOW,
        )
        order = Order(
            order_id="o-manual",
            signal=signal,
            status="filled",
            created_ts=NOW,
        )
        return ExecutionResult(order=order, fill=fill)


@pytest.fixture
def env(tmp_path):
    store = SQLiteStore(tmp_path / "quant.db")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = _settings()
    broker = FakeBroker()
    trader = ManualTrader(settings, store, redis)
    # 绕过真实 HTTP：直接注入假客户端/执行器
    trader._client = FakeClient([_spot()])
    trader._broker = broker
    yield trader, store, redis, settings, broker
    store.close()


def test_floor_to_truncates() -> None:
    assert floor_to(Decimal("0.123456"), Decimal("0.001")) == Decimal("0.123")


def _save_live_order(store: SQLiteStore, order_id: str = "o-live") -> None:
    """造一条仍在挂单簿的本地订单（急停撤单的同步对象）。"""
    signal = Signal(
        strategy_id="sma_cross",
        side="buy",
        order_type="market",
        inst_id="BTC-USDT",
        qty=Decimal("1"),
        ts=NOW,
        signal_id=f"sig-{order_id}",
    )
    store.save_signal(signal, "executed")
    store.save_order(
        Order(order_id=order_id, signal=signal, status="live", created_ts=NOW)
    )


class TestCancelOpenOrders:
    async def test_cancels_exchange_and_marks_local(self, env) -> None:
        trader, store, _redis, _settings, broker = env
        broker.canceled = 2
        _save_live_order(store)

        result = await trader.cancel_open_orders()

        assert result == {"exchange_canceled": 2, "local_marked": 1}
        assert store.get_open_orders() == []

    async def test_exchange_failure_keeps_local_status(self, env) -> None:
        trader, store, _redis, _settings, broker = env
        broker.cancel_error = RuntimeError("api down")
        _save_live_order(store)

        with pytest.raises(ManualTradeError) as exc:
            await trader.cancel_open_orders()

        assert exc.value.status == 502
        # 交易所没撤成功就不能谎报本地已撤，否则页面会留下真实挂单
        assert len(store.get_open_orders()) == 1

    async def test_missing_credentials_rejected(self, tmp_path) -> None:
        settings = Settings(
            okx=OKXSettings(api_key="", secret="", passphrase="")
        )
        store = SQLiteStore(tmp_path / "quant.db")
        trader = ManualTrader(settings, store)
        try:
            with pytest.raises(ManualTradeError) as exc:
                await trader.cancel_open_orders()
            assert exc.value.status == 503
        finally:
            store.close()


class TestReduce:
    async def test_clamps_to_exchange_available(self, env) -> None:
        trader, store, _redis, _settings, broker = env
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("1"), avg_price=Decimal("100"))
        )
        trader._client = FakeClient(
            [_spot()], balances=[{"ccy": "BTC", "availBal": "0.3"}]
        )

        result = await trader.reduce(
            "BTC-USDT", ratio=Decimal("1"), qty=None, reason="test"
        )

        assert result["submitted_qty"] == "0.3"
        assert broker.calls[0]["qty"] == Decimal("0.3")
        assert broker.calls[0]["reduce_only"] is False

    async def test_floors_to_lot_size(self, env) -> None:
        trader, _store, _redis, _settings, broker = env
        trader._client = FakeClient(
            [_spot(lot="0.001", min_sz="0.001")],
            balances=[{"ccy": "BTC", "availBal": "0.123456"}],
        )

        result = await trader.reduce(
            "BTC-USDT", ratio=None, qty=Decimal("0.123456"), reason="test"
        )

        assert result["submitted_qty"] == "0.123"
        assert broker.calls[0]["qty"] == Decimal("0.123")

    async def test_rejects_below_min_size(self, env) -> None:
        trader, _store, _redis, _settings, _broker = env
        trader._client = FakeClient(
            [_spot(lot="0.001", min_sz="0.001")],
            balances=[{"ccy": "BTC", "availBal": "0.0004"}],
        )

        with pytest.raises(ManualTradeError, match="低于最小下单量"):
            await trader.reduce(
                "BTC-USDT", ratio=None, qty=Decimal("0.0004"), reason="test"
            )

    async def test_ratio_requires_local_position(self, env) -> None:
        trader, _store, _redis, _settings, _broker = env
        trader._client = FakeClient(
            [_spot()], balances=[{"ccy": "BTC", "availBal": "0.5"}]
        )

        with pytest.raises(ManualTradeError, match="本地无持仓"):
            await trader.reduce(
                "BTC-USDT", ratio=Decimal("0.5"), qty=None, reason="test"
            )

    async def test_full_reduce_zeroes_local_position(self, env) -> None:
        trader, store, _redis, _settings, _broker = env
        store.update_position(
            Position(
                inst_id="BTC-USDT",
                qty=Decimal("0.5"),
                avg_price=Decimal("80"),
            )
        )
        trader._client = FakeClient(
            [_spot()], balances=[{"ccy": "BTC", "availBal": "0.5"}]
        )

        await trader.reduce("BTC-USDT", ratio=Decimal("1"), qty=None, reason="test")

        position = store.get_position("BTC-USDT")
        assert position.qty == 0
        assert position.avg_price == 0
        # (100 - 80) * 0.5
        assert position.realized_pnl == Decimal("10.0")

    async def test_requires_reason(self, env) -> None:
        trader, _store, _redis, _settings, _broker = env

        with pytest.raises(ManualTradeError, match="原因"):
            await trader.reduce("BTC-USDT", ratio=Decimal("1"), qty=None, reason=" ")

    async def test_rejects_both_inputs(self, env) -> None:
        trader, _store, _redis, _settings, _broker = env

        with pytest.raises(ManualTradeError, match="只能提供一个"):
            await trader.reduce(
                "BTC-USDT", ratio=Decimal("0.5"), qty=Decimal("0.1"), reason="x"
            )


class TestSwap:
    async def test_contracts_and_reduce_only(self, env) -> None:
        trader, store, _redis, _settings, broker = env
        store.update_position(
            Position(
                inst_id="BTC-USDT-SWAP",
                qty=Decimal("0.05"),
                avg_price=Decimal("100"),
            )
        )
        trader._client = FakeClient(
            [_swap()],
            positions=[{"instType": "SWAP", "instId": "BTC-USDT-SWAP", "pos": "5"}],
        )

        result = await trader.reduce(
            "BTC-USDT-SWAP", ratio=Decimal("1"), qty=None, reason="test"
        )

        assert result["inst_type"] == "SWAP"
        assert result["contracts"] == "5"
        assert result["submitted_qty"] == "0.05"
        call = broker.calls[0]
        assert call["reduce_only"] is True
        # 全链路基础币口径：张数换算由 broker 负责
        assert call["qty"] == Decimal("0.05")
        assert call["signal"].qty == Decimal("0.05")


class TestFlatten:
    async def test_aggregates_failures(self, env) -> None:
        trader, store, _redis, _settings, _broker = env
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("1"), avg_price=Decimal("100"))
        )
        store.update_position(
            Position(inst_id="ETH-USDT", qty=Decimal("2"), avg_price=Decimal("10"))
        )
        # 只给 BTC 的配置，ETH 查询失败
        trader._client = FakeClient(
            [_spot()], balances=[{"ccy": "BTC", "availBal": "1"}]
        )

        result = await trader.flatten(reason="panic")

        assert result["total"] == 2
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert result["ok"] is False
        errors = [item for item in result["results"] if not item["ok"]]
        assert errors[0]["inst_id"] == "ETH-USDT"

    async def test_includes_snapshot_positions(self, env) -> None:
        """快照里的合约多头即使本地没记录也要平（防漏平）。"""
        trader, store, redis, settings, _broker = env
        await redis.set(
            settings.monitor.dashboard_snapshot_key,
            json.dumps(
                {
                    "ts": NOW,
                    "positions": [{"inst_id": "BTC-USDT-SWAP", "qty": "0.05"}],
                }
            ),
        )
        trader._client = FakeClient(
            [_swap()],
            positions=[{"instType": "SWAP", "instId": "BTC-USDT-SWAP", "pos": "5"}],
        )

        result = await trader.flatten(reason="panic")

        assert result["total"] == 1
        assert result["succeeded"] == 1

    async def test_ignores_spot_positions_from_snapshot(self, env) -> None:
        """快照里的现货余额（含非策略持仓）不参与全平。"""
        trader, _store, redis, settings, broker = env
        await redis.set(
            settings.monitor.dashboard_snapshot_key,
            json.dumps(
                {
                    "ts": NOW,
                    "positions": [
                        {"inst_id": "BTC-USDT", "qty": "0.5"},
                        {"inst_id": "ETH-USDT", "qty": "1"},
                        {"inst_id": "OKB-USDT", "qty": "100"},
                    ],
                }
            ),
        )

        result = await trader.flatten(reason="panic")

        assert result["total"] == 0
        assert broker.calls == []

    async def test_uses_exchange_qty_when_local_stale(self, env) -> None:
        """本地记录偏小时，全平仍以交易所可用量为准，避免留下残余。"""
        trader, store, _redis, _settings, broker = env
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("0.1"), avg_price=Decimal("100"))
        )
        trader._client = FakeClient(
            [_spot()], balances=[{"ccy": "BTC", "availBal": "0.5"}]
        )

        result = await trader.flatten(reason="panic")

        assert result["succeeded"] == 1
        assert broker.calls[0]["qty"] == Decimal("0.5")
        assert store.get_position("BTC-USDT").qty == 0

    async def test_untracked_position_stays_flat(self, env) -> None:
        """本地无记录但交易所有仓位：全平后本地仍为空仓，不能变成空头。"""
        trader, store, redis, settings, broker = env
        await redis.set(
            settings.monitor.dashboard_snapshot_key,
            json.dumps(
                {
                    "ts": NOW,
                    "positions": [{"inst_id": "BTC-USDT-SWAP", "qty": "0.05"}],
                }
            ),
        )
        trader._client = FakeClient(
            [_swap()],
            positions=[{"instType": "SWAP", "instId": "BTC-USDT-SWAP", "pos": "5"}],
        )

        result = await trader.flatten(reason="panic")

        assert result["succeeded"] == 1
        assert broker.calls[0]["qty"] == Decimal("0.05")
        assert store.get_position("BTC-USDT-SWAP").qty == 0


class TestOrderLinkage:
    async def test_manual_order_keeps_signal_context(self, env) -> None:
        """手动订单要落 signals：orders 靠 JOIN 取品种/方向/类型/数量。"""
        trader, store, redis, settings, _broker = env
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("1"), avg_price=Decimal("100"))
        )
        trader._client = FakeClient(
            [_spot()], balances=[{"ccy": "BTC", "availBal": "1"}]
        )

        result = await trader.reduce(
            "BTC-USDT", ratio=Decimal("0.5"), qty=None, reason="test"
        )

        data = await DashboardService(store, redis, settings).orders(10)
        row = data["orders"][0]
        assert row["order_id"] == result["order_id"]
        assert row["inst_id"] == "BTC-USDT"
        assert row["side"] == "sell"
        assert row["order_type"] == "market"
        assert row["qty"] == "0.5"
        assert row["status"] == "filled"
        assert row["filled_qty"] == "0.5"

    async def test_sell_more_than_local_does_not_flip_position(self, env) -> None:
        """交易所成交量大于本地记录时，本地只结平已知持仓，不翻成空头。"""
        trader, store, _redis, _settings, _broker = env
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("0.2"), avg_price=Decimal("80"))
        )
        trader._client = FakeClient(
            [_spot()], balances=[{"ccy": "BTC", "availBal": "1"}]
        )

        await trader.reduce("BTC-USDT", ratio=None, qty=Decimal("1"), reason="test")

        position = store.get_position("BTC-USDT")
        assert position.qty == 0
        assert position.avg_price == 0
        # 只按已知的 0.2 计算：(100 - 80) * 0.2
        assert position.realized_pnl == Decimal("4.0")


async def _set_equity(redis, settings, value: str = "10000") -> None:
    """monitor 发布的账户快照（风控基数）。"""
    await redis.set(
        settings.monitor.dashboard_snapshot_key,
        json.dumps(
            {"ts": NOW, "account": {"total_equity": value, "avail_usdt": value}}
        ),
    )


def _save_open_limit_order(
    store: SQLiteStore,
    order_id: str = "o-limit",
    *,
    qty: str = "0.5",
    price: str | None = "100",
    side: str = "buy",
    order_type: str = "limit",
    status: str = "live",
) -> None:
    signal = Signal(
        strategy_id="manual",
        side=side,
        order_type=order_type,
        inst_id="BTC-USDT",
        qty=Decimal(qty),
        price=Decimal(price) if price is not None else None,
        ts=NOW,
        signal_id=f"sig-{order_id}",
    )
    store.save_signal(signal, "executed")
    store.save_order(
        Order(order_id=order_id, signal=signal, status=status, created_ts=NOW)
    )


class TestPlace:
    """M8-17：手工开仓（市价/限价）与风控闸门。"""

    async def test_market_buy_persists_position(self, env) -> None:
        trader, store, redis, settings, broker = env
        await _set_equity(redis, settings)

        result = await trader.place_order(
            "BTC-USDT",
            side="buy",
            order_type="market",
            qty=Decimal("0.5"),
            price=None,
            reason="test",
        )

        assert result["ok"] is True
        call = broker.calls[0]
        assert call["qty"] == Decimal("0.5")
        # 开仓不能带 reduceOnly，否则交易所会拒单/变成只减
        assert call["reduce_only"] is False
        assert call["signal"].strategy_id == "manual"
        assert call["signal"].side == "buy"
        assert call["signal"].signal_id.startswith("manual-")

        row = store.get_order("o-manual")
        assert row is not None
        assert row["inst_id"] == "BTC-USDT"
        assert row["qty"] == "0.5"
        assert store.get_position("BTC-USDT").qty == Decimal("0.5")

    async def test_limit_order_carries_price(self, env) -> None:
        trader, _store, redis, settings, broker = env
        await _set_equity(redis, settings)

        await trader.place_order(
            "BTC-USDT",
            side="sell",
            order_type="limit",
            qty=Decimal("0.5"),
            price=Decimal("120"),
            reason="test",
        )

        signal = broker.calls[0]["signal"]
        assert signal.order_type == "limit"
        assert signal.price == Decimal("120")

    async def test_limit_requires_price(self, env) -> None:
        trader, _store, _redis, _settings, _broker = env

        with pytest.raises(ManualTradeError, match="限价单必须提供"):
            await trader.place_order(
                "BTC-USDT",
                side="buy",
                order_type="limit",
                qty=Decimal("1"),
                price=None,
                reason="test",
            )

    async def test_requires_reason(self, env) -> None:
        trader, _store, _redis, _settings, _broker = env

        with pytest.raises(ManualTradeError, match="原因"):
            await trader.place_order(
                "BTC-USDT",
                side="buy",
                order_type="market",
                qty=Decimal("1"),
                price=None,
                reason=" ",
            )

    async def test_missing_snapshot_rejected(self, env) -> None:
        trader, _store, _redis, _settings, broker = env

        with pytest.raises(ManualTradeError) as exc:
            await trader.place_order(
                "BTC-USDT",
                side="buy",
                order_type="market",
                qty=Decimal("0.5"),
                price=None,
                reason="test",
            )

        assert exc.value.status == 503
        assert broker.calls == []

    async def test_stale_snapshot_rejected(self, env) -> None:
        """快照存在但已过期：按不可用处理，不能拿旧权益放大敞口。"""
        trader, _store, redis, settings, broker = env
        await redis.set(
            settings.monitor.dashboard_snapshot_key,
            json.dumps(
                {
                    "ts": NOW - 3_600_000,
                    "account": {"total_equity": "10000"},
                }
            ),
        )

        with pytest.raises(ManualTradeError) as exc:
            await trader.place_order(
                "BTC-USDT",
                side="buy",
                order_type="market",
                qty=Decimal("0.5"),
                price=None,
                reason="test",
            )

        assert exc.value.status == 503
        assert broker.calls == []

    async def test_kill_switch_blocks_new_exposure(self, env) -> None:
        trader, _store, redis, settings, broker = env
        await _set_equity(redis, settings)
        await KillSwitch(redis).enable("panic")

        with pytest.raises(ManualTradeError, match="紧急停止") as exc:
            await trader.place_order(
                "BTC-USDT",
                side="buy",
                order_type="market",
                qty=Decimal("0.5"),
                price=None,
                reason="test",
            )

        assert exc.value.status == 409
        assert broker.calls == []

    async def test_risk_adjusts_oversized_order(self, env) -> None:
        """名义/比例规则是「减量」而非拒单：请求 200 会被压到风控允许值。"""
        trader, store, redis, settings, broker = env
        await _set_equity(redis, settings)

        result = await trader.place_order(
            "BTC-USDT",
            side="buy",
            order_type="market",
            qty=Decimal("200"),
            price=None,
            reason="test",
        )

        # 权益 10000 × max_position_pct 0.3 ÷ 价格 100 = 30
        assert result["adjusted"] is True
        assert Decimal(str(result["submitted_qty"])) == Decimal("30")
        assert broker.calls[0]["qty"] == Decimal("30.0")
        events = store.list_risk_events(strategy_id="manual")
        assert events and events[0]["decision"] == "adjusted"

    async def test_daily_loss_rejects_and_writes_event(self, env) -> None:
        trader, store, redis, settings, broker = env
        await _set_equity(redis, settings)
        # 今日已实现亏损 999，超过 10000 × 0.05 = 500
        store.save_fill(
            Fill(
                order_id="o-loss-buy",
                inst_id="BTC-USDT",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("1000"),
                fee=Decimal("0"),
                ts=NOW - 1000,
            )
        )
        store.save_fill(
            Fill(
                order_id="o-loss-sell",
                inst_id="BTC-USDT",
                side="sell",
                qty=Decimal("1"),
                price=Decimal("1"),
                fee=Decimal("0"),
                ts=NOW,
            )
        )

        with pytest.raises(ManualTradeError, match="风控拒单") as exc:
            await trader.place_order(
                "BTC-USDT",
                side="buy",
                order_type="market",
                qty=Decimal("0.5"),
                price=None,
                reason="test",
            )

        assert exc.value.status == 409
        assert broker.calls == []
        events = store.list_risk_events(strategy_id="manual")
        assert events and events[0]["rule"] == "daily_loss_limit"
        assert events[0]["decision"] == "halted"

    async def test_swap_qty_converted_to_contracts(self, env) -> None:
        trader, _store, redis, settings, broker = env
        await _set_equity(redis, settings)
        trader._client = FakeClient([_swap(ct_val="0.01", lot="1", min_sz="1")])

        result = await trader.place_order(
            "BTC-USDT-SWAP",
            side="buy",
            order_type="market",
            qty=Decimal("0.055"),
            price=None,
            reason="test",
        )

        assert result["contracts"] == "5"
        assert result["submitted_qty"] == "0.05"
        assert broker.calls[0]["qty"] == Decimal("0.05")


class TestCancel:
    """M8-17：撤单。"""

    async def test_cancel_marks_local_and_is_idempotent(self, env) -> None:
        trader, store, _redis, _settings, broker = env
        _save_open_limit_order(store)

        first = await trader.cancel_order("o-limit", reason="test")
        second = await trader.cancel_order("o-limit", reason="test")

        assert first["status"] == "canceled"
        assert first["already"] is False
        assert second["already"] is True
        assert broker.canceled_orders == [("BTC-USDT", "o-limit")]
        assert store.get_order("o-limit")["status"] == "canceled"

    async def test_remote_canceled_counts_as_success(self, env) -> None:
        """交易所报错但远端已是 canceled：按成功处理，不误报失败。"""
        trader, store, _redis, _settings, broker = env
        _save_open_limit_order(store)
        broker.cancel_order_error = RuntimeError("51400 order does not exist")
        broker.remote_state = "canceled"

        result = await trader.cancel_order("o-limit", reason="test")

        assert result["status"] == "canceled"
        assert store.get_order("o-limit")["status"] == "canceled"

    async def test_failure_keeps_status(self, env) -> None:
        trader, store, _redis, _settings, broker = env
        _save_open_limit_order(store)
        broker.cancel_order_error = RuntimeError("api down")
        broker.remote_state = "live"

        with pytest.raises(ManualTradeError) as exc:
            await trader.cancel_order("o-limit", reason="test")

        assert exc.value.status == 502
        assert store.get_order("o-limit")["status"] == "live"

    async def test_missing_order(self, env) -> None:
        trader, _store, _redis, _settings, _broker = env

        with pytest.raises(ManualTradeError) as exc:
            await trader.cancel_order("o-none", reason="test")

        assert exc.value.status == 404


class TestAmend:
    """M8-17：改价/改量。"""

    async def test_amend_price_and_qty_updates_signal(self, env) -> None:
        trader, store, redis, settings, broker = env
        await _set_equity(redis, settings)
        _save_open_limit_order(store, qty="0.5", price="100")

        result = await trader.amend_order(
            "o-limit", qty=Decimal("0.4"), price=Decimal("90"), reason="test"
        )

        assert broker.amended == [
            {
                "inst_id": "BTC-USDT",
                "order_id": "o-limit",
                "new_size": Decimal("0.4"),
                "new_price": Decimal("90"),
            }
        ]
        assert result["submitted_qty"] == "0.4"
        row = store.get_order("o-limit")
        assert row["qty"] == "0.4"
        assert row["price"] == "90"
        # 订单仍在挂单簿，状态不动
        assert row["status"] == "live"

    async def test_amend_price_only_skips_size(self, env) -> None:
        trader, store, redis, settings, broker = env
        await _set_equity(redis, settings)
        _save_open_limit_order(store, qty="0.5", price="100")

        await trader.amend_order(
            "o-limit", qty=None, price=Decimal("95"), reason="test"
        )

        assert broker.amended[0]["new_size"] is None
        assert store.get_order("o-limit")["price"] == "95"

    async def test_amend_rejects_market_order(self, env) -> None:
        trader, store, _redis, _settings, _broker = env
        _save_open_limit_order(store, order_type="market", price=None)

        with pytest.raises(ManualTradeError, match="只有限价单"):
            await trader.amend_order(
                "o-limit", qty=Decimal("0.1"), price=None, reason="test"
            )

    async def test_amend_rejects_closed_order(self, env) -> None:
        trader, store, _redis, _settings, _broker = env
        _save_open_limit_order(store, status="filled")

        with pytest.raises(ManualTradeError, match="已终结"):
            await trader.amend_order(
                "o-limit", qty=None, price=Decimal("95"), reason="test"
            )

    async def test_amend_requires_one_field(self, env) -> None:
        trader, _store, _redis, _settings, _broker = env

        with pytest.raises(ManualTradeError, match="至少提供一个"):
            await trader.amend_order(
                "o-limit", qty=None, price=None, reason="test"
            )

    async def test_amend_clamped_by_risk(self, env) -> None:
        """改量同样受风控约束：超限时按允许值下发，并回写 signals。"""
        trader, store, redis, settings, broker = env
        await _set_equity(redis, settings)
        _save_open_limit_order(store, qty="0.5", price="100")

        # 500 会被压到 10000 × 0.3 ÷ 100 = 30
        result = await trader.amend_order(
            "o-limit", qty=Decimal("500"), price=None, reason="test"
        )

        assert result["submitted_qty"] == "30.0"
        assert broker.amended[0]["new_size"] == Decimal("30.0")
        assert store.get_order("o-limit")["qty"] == "30.0"

    async def test_amend_rejected_by_daily_loss(self, env) -> None:
        trader, store, redis, settings, broker = env
        await _set_equity(redis, settings)
        _save_open_limit_order(store, qty="0.5", price="100")
        store.save_fill(
            Fill(
                order_id="o-loss-buy",
                inst_id="BTC-USDT",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("1000"),
                fee=Decimal("0"),
                ts=NOW - 1000,
            )
        )
        store.save_fill(
            Fill(
                order_id="o-loss-sell",
                inst_id="BTC-USDT",
                side="sell",
                qty=Decimal("1"),
                price=Decimal("1"),
                fee=Decimal("0"),
                ts=NOW,
            )
        )

        with pytest.raises(ManualTradeError, match="风控拒单"):
            await trader.amend_order(
                "o-limit", qty=None, price=Decimal("90"), reason="test"
            )

        assert broker.amended == []
        assert store.get_order("o-limit")["price"] == "100"


class TestRoutes:
    LOCAL = {"host": "127.0.0.1:8080", "X-Dashboard-Request": "1"}

    def _app(self, tmp_path, manual=True):
        store = SQLiteStore(tmp_path / "quant.db")
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = _settings()
        service = DashboardService(store, redis, settings)
        trader = None
        if manual:
            trader = ManualTrader(settings, store, redis)
            trader._client = FakeClient(
                [_spot()], balances=[{"ccy": "BTC", "availBal": "1"}]
            )
            trader._broker = FakeBroker()
            trader._store = store
        return create_app(service, tmp_path / "missing", manual=trader), store, redis

    async def test_reduce_route(self, tmp_path) -> None:
        app, store, _redis = self._app(tmp_path)
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("1"), avg_price=Decimal("90"))
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/positions/reduce",
                json={"inst_id": "BTC-USDT", "ratio": 0.5, "reason": "test"},
                headers=self.LOCAL,
            )

        assert response.status_code == 200
        assert response.json()["submitted_qty"] == "0.5"
        store.close()

    async def test_reduce_requires_header(self, tmp_path) -> None:
        app, store, _redis = self._app(tmp_path)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/positions/reduce",
                json={"inst_id": "BTC-USDT", "ratio": 0.5, "reason": "test"},
                headers={"host": "127.0.0.1:8080"},
            )

        assert response.status_code == 400
        store.close()

    async def test_flatten_requires_confirm_word(self, tmp_path) -> None:
        app, store, _redis = self._app(tmp_path)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/positions/flatten",
                json={"reason": "panic", "confirm": "yes"},
                headers=self.LOCAL,
            )

        assert response.status_code == 400
        store.close()

    async def test_route_503_when_manual_disabled(self, tmp_path) -> None:
        app, store, _redis = self._app(tmp_path, manual=False)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/positions/reduce",
                json={"inst_id": "BTC-USDT", "ratio": 1, "reason": "test"},
                headers=self.LOCAL,
            )

        assert response.status_code == 503
        store.close()

    async def test_manual_order_route(self, tmp_path) -> None:
        app, store, redis = self._app(tmp_path)
        await _set_equity(redis, _settings())

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/manual/orders",
                json={
                    "inst_id": "BTC-USDT",
                    "side": "buy",
                    "order_type": "market",
                    "qty": "0.5",
                    "reason": "test",
                },
                headers=self.LOCAL,
            )

        assert response.status_code == 200
        assert response.json()["submitted_qty"] == "0.5"
        store.close()

    async def test_manual_order_requires_header(self, tmp_path) -> None:
        app, store, _redis = self._app(tmp_path)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/manual/orders",
                json={
                    "inst_id": "BTC-USDT",
                    "side": "buy",
                    "order_type": "market",
                    "qty": "0.5",
                    "reason": "test",
                },
                headers={"host": "127.0.0.1:8080"},
            )

        assert response.status_code == 400
        store.close()

    async def test_cancel_route(self, tmp_path) -> None:
        app, store, _redis = self._app(tmp_path)
        _save_open_limit_order(store)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/manual/orders/o-limit/cancel",
                json={"reason": "test"},
                headers=self.LOCAL,
            )

        assert response.status_code == 200
        assert response.json()["status"] == "canceled"
        store.close()

    async def test_amend_route(self, tmp_path) -> None:
        app, store, redis = self._app(tmp_path)
        await _set_equity(redis, _settings())
        _save_open_limit_order(store)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/manual/orders/o-limit/amend",
                json={"price": "90", "reason": "test"},
                headers=self.LOCAL,
            )

        assert response.status_code == 200
        assert response.json()["price"] == "90"
        store.close()


class TestKillSwitchCancel:
    """M7-4：看板急停默认同时撤销交易所挂单。"""

    LOCAL = {"host": "127.0.0.1:8080", "X-Dashboard-Request": "1"}

    def _app(self, tmp_path, *, manual=True, canceled=2, cancel_error=None):
        store = SQLiteStore(tmp_path / "quant.db")
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = _settings()
        service = DashboardService(store, redis, settings)
        trader = None
        broker = None
        if manual:
            trader = ManualTrader(settings, store, redis)
            trader._client = FakeClient(
                [_spot()], balances=[{"ccy": "BTC", "availBal": "1"}]
            )
            broker = FakeBroker(canceled=canceled, cancel_error=cancel_error)
            trader._broker = broker
        app = create_app(service, tmp_path / "missing", manual=trader)
        return app, store, redis, broker

    async def test_on_cancels_by_default(self, tmp_path) -> None:
        app, store, redis, _broker = self._app(tmp_path)
        _save_live_order(store)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/kill-switch",
                json={"action": "on", "reason": "panic"},
                headers=self.LOCAL,
            )

        body = response.json()
        assert response.status_code == 200
        assert body["active"] is True
        assert body["cancel"] == {
            "ok": True,
            "exchange_canceled": 2,
            "local_marked": 1,
        }
        assert store.get_open_orders() == []
        assert await redis.exists("quant:kill-switch") == 1
        store.close()

    async def test_can_skip_cancel(self, tmp_path) -> None:
        app, store, _redis, _broker = self._app(tmp_path)
        _save_live_order(store)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/kill-switch",
                json={"action": "on", "cancel_orders": False},
                headers=self.LOCAL,
            )

        body = response.json()
        assert body["active"] is True
        assert "cancel" not in body
        assert len(store.get_open_orders()) == 1
        store.close()

    async def test_cancel_failure_keeps_stop_flag(self, tmp_path) -> None:
        app, store, redis, _broker = self._app(
            tmp_path, cancel_error=RuntimeError("api down")
        )
        _save_live_order(store)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/kill-switch",
                json={"action": "on", "reason": "panic"},
                headers=self.LOCAL,
            )
            overview = await client.get("/api/overview")

        body = response.json()
        # 撤单失败绝不能回滚停止标志，否则等于「以为停了其实没停」
        assert body["active"] is True
        assert body["cancel"]["ok"] is False
        assert "api down" in body["cancel"]["error"]
        assert overview.json()["kill_switch"]["active"] is True
        assert len(store.get_open_orders()) == 1
        assert await redis.exists("quant:kill-switch") == 1
        store.close()

    async def test_without_trader_still_stops(self, tmp_path) -> None:
        app, store, _redis, _broker = self._app(tmp_path, manual=False)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/kill-switch",
                json={"action": "on"},
                headers=self.LOCAL,
            )

        body = response.json()
        assert body["active"] is True
        assert body["cancel"]["ok"] is False
        assert "手动交易未启用" in body["cancel"]["error"]
        store.close()

    async def test_off_does_not_cancel(self, tmp_path) -> None:
        app, store, _redis, _broker = self._app(tmp_path)
        _save_live_order(store)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8080",
        ) as client:
            response = await client.post(
                "/api/kill-switch", json={"action": "off"}, headers=self.LOCAL
            )

        assert "cancel" not in response.json()
        assert len(store.get_open_orders()) == 1
        store.close()


class TestCredentials:
    async def test_503_without_okx_keys(self, tmp_path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        settings = Settings(
            okx=OKXSettings(api_key="", secret="", passphrase="", demo=True)
        )
        trader = ManualTrader(settings, store)
        # 走真实 _ensure_client 路径：凭据为空必须 503 而不是抛未捕获异常
        trader._client = None
        trader._broker = None
        with pytest.raises(ManualTradeError) as err:
            trader._ensure_client()
        assert err.value.status == 503
        store.close()
