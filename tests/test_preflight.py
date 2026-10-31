"""下单前预检测试（M7-7）。

覆盖四类硬约束：产品配置、数量精度、最小下单量、最小名义金额、现货余额，
以及「余额读不到时放行」的取舍与 worker 侧的拒单留痕。
"""
from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis
import pytest

from quant.backtest.signal import Signal
from quant.execution.executor import Executor
from quant.execution.worker import Worker
from quant.models.instrument import Instrument
from quant.queue.signal_queue import SignalQueue
from quant.risk.checker import RiskChecker
from quant.risk.config import RiskConfig
from quant.risk.preflight import (
    RULE_BALANCE,
    RULE_INSTRUMENT,
    RULE_LOT_SIZE,
    RULE_MIN_NOTIONAL,
    RULE_MIN_SIZE,
    PreflightChecker,
)
from quant.state.sqlite_store import SQLiteStore

SPOT = Instrument(
    inst_id="BTC-USDT",
    inst_type="SPOT",
    base_ccy="BTC",
    quote_ccy="USDT",
    settle_ccy="",
    tick_sz=Decimal("0.1"),
    lot_sz=Decimal("0.0001"),
    min_sz=Decimal("0.0001"),
    ct_val=Decimal("1"),
    state="live",
    list_time=0,
)

SWAP = Instrument(
    inst_id="BTC-USDT-SWAP",
    inst_type="SWAP",
    base_ccy="BTC",
    quote_ccy="USDT",
    settle_ccy="USDT",
    tick_sz=Decimal("0.1"),
    lot_sz=Decimal("1"),
    min_sz=Decimal("1"),
    ct_val=Decimal("0.01"),
    state="live",
    list_time=0,
)


class FakeClient:
    """只实现预检用到的两个只读方法。"""

    def __init__(
        self,
        instruments: dict[str, Instrument] | None = None,
        balance: dict | None = None,
        balance_error: Exception | None = None,
    ) -> None:
        self._instruments = instruments or {}
        self._balance = balance or {}
        self._balance_error = balance_error
        self.balance_calls = 0

    def get_instrument(self, inst_id: str) -> Instrument | None:
        return self._instruments.get(inst_id)

    def get_balance(self) -> dict:
        self.balance_calls += 1
        if self._balance_error is not None:
            raise self._balance_error
        return self._balance


def _signal(
    *,
    inst_id: str = "BTC-USDT",
    side: str = "buy",
    qty: str = "0.001",
    price: str | None = None,
) -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side=side,  # type: ignore[arg-type]
        order_type="limit" if price is not None else "market",
        inst_id=inst_id,
        qty=Decimal(qty),
        ts=1_700_000_000_000,
        price=Decimal(price) if price is not None else None,
        signal_id="sig-1",
    )


def _balance(ccy: str, amount: str) -> dict:
    return {"details": [{"ccy": ccy, "availBal": amount}]}


class TestInstrumentChecks:
    async def test_missing_instrument_rejected(self) -> None:
        checker = PreflightChecker(FakeClient())

        result = await checker.check(_signal(), Decimal("0.001"), Decimal("100"))

        assert result.ok is False
        assert result.rule == RULE_INSTRUMENT

    async def test_spot_qty_floored_to_lot_size(self) -> None:
        checker = PreflightChecker(FakeClient({"BTC-USDT": SPOT}))

        result = await checker.check(
            _signal(qty="0.00123"), Decimal("0.00123"), Decimal("100")
        )

        assert result.ok is True
        assert result.qty == Decimal("0.0012")

    async def test_spot_below_min_size_rejected(self) -> None:
        tiny = Instrument(
            inst_id="BTC-USDT",
            inst_type="SPOT",
            base_ccy="BTC",
            quote_ccy="USDT",
            settle_ccy="",
            tick_sz=Decimal("0.1"),
            lot_sz=Decimal("0.00001"),
            min_sz=Decimal("0.001"),
            ct_val=Decimal("1"),
            state="live",
            list_time=0,
        )
        checker = PreflightChecker(FakeClient({"BTC-USDT": tiny}))

        result = await checker.check(_signal(qty="0.0005"), Decimal("0.0005"))

        assert result.ok is False
        assert result.rule == RULE_MIN_SIZE

    async def test_contract_rounds_to_zero_contracts(self) -> None:
        checker = PreflightChecker(FakeClient({"BTC-USDT-SWAP": SWAP}))

        # 0.005 BTC / ctVal 0.01 = 0.5 张，按 lotSz=1 取整为 0 张
        result = await checker.check(
            _signal(inst_id="BTC-USDT-SWAP", qty="0.005"),
            Decimal("0.005"),
            Decimal("60000"),
        )

        assert result.ok is False
        assert result.rule == RULE_LOT_SIZE

    async def test_contract_qty_expressed_in_contracts(self) -> None:
        checker = PreflightChecker(FakeClient({"BTC-USDT-SWAP": SWAP}))

        # 0.035 / 0.01 = 3.5 张 → 3 张 → 0.03 BTC
        result = await checker.check(
            _signal(inst_id="BTC-USDT-SWAP", qty="0.035"),
            Decimal("0.035"),
            Decimal("60000"),
        )

        assert result.ok is True
        assert result.qty == Decimal("0.03")

    async def test_contract_below_min_contracts_rejected(self) -> None:
        strict = Instrument(
            inst_id="BTC-USDT-SWAP",
            inst_type="SWAP",
            base_ccy="BTC",
            quote_ccy="USDT",
            settle_ccy="USDT",
            tick_sz=Decimal("0.1"),
            lot_sz=Decimal("1"),
            min_sz=Decimal("5"),
            ct_val=Decimal("0.01"),
            state="live",
            list_time=0,
        )
        checker = PreflightChecker(FakeClient({"BTC-USDT-SWAP": strict}))

        result = await checker.check(
            _signal(inst_id="BTC-USDT-SWAP", qty="0.03"),
            Decimal("0.03"),
            Decimal("60000"),
        )

        assert result.ok is False
        assert result.rule == RULE_MIN_SIZE
        assert result.actual_value == Decimal("3")


class TestNotional:
    async def test_below_configured_min_notional_rejected(self) -> None:
        checker = PreflightChecker(
            FakeClient({"BTC-USDT": SPOT}),
            min_notional=Decimal("100"),
        )

        result = await checker.check(_signal(qty="0.001"), Decimal("0.001"), Decimal("100"))

        assert result.ok is False
        assert result.rule == RULE_MIN_NOTIONAL
        assert result.actual_value == Decimal("0.1")

    async def test_above_min_notional_passes(self) -> None:
        checker = PreflightChecker(
            FakeClient({"BTC-USDT": SPOT}), min_notional=Decimal("10")
        )

        result = await checker.check(
            _signal(qty="0.5"), Decimal("0.5"), Decimal("100")
        )

        assert result.ok is True

    async def test_zero_min_notional_disables_check(self) -> None:
        checker = PreflightChecker(FakeClient({"BTC-USDT": SPOT}))

        result = await checker.check(
            _signal(qty="0.0001"), Decimal("0.0001"), Decimal("100")
        )

        assert result.ok is True

    async def test_limit_order_uses_signal_price(self) -> None:
        checker = PreflightChecker(
            FakeClient({"BTC-USDT": SPOT}), min_notional=Decimal("50")
        )

        result = await checker.check(
            _signal(qty="0.1", price="600"), Decimal("0.1"), None
        )

        assert result.ok is True


class TestBalance:
    async def test_spot_buy_insufficient_balance(self) -> None:
        client = FakeClient(
            {"BTC-USDT": SPOT}, balance=_balance("USDT", "10")
        )
        checker = PreflightChecker(client)

        result = await checker.check(
            _signal(qty="0.5"), Decimal("0.5"), Decimal("100")
        )

        assert result.ok is False
        assert result.rule == RULE_BALANCE
        assert result.actual_value == Decimal("10")

    async def test_spot_buy_sufficient_balance(self) -> None:
        client = FakeClient(
            {"BTC-USDT": SPOT}, balance=_balance("USDT", "1000")
        )
        checker = PreflightChecker(client)

        result = await checker.check(
            _signal(qty="0.5"), Decimal("0.5"), Decimal("100")
        )

        assert result.ok is True
        assert result.balance_checked is True

    async def test_spot_sell_checks_base_balance(self) -> None:
        client = FakeClient(
            {"BTC-USDT": SPOT}, balance=_balance("BTC", "0.1")
        )
        checker = PreflightChecker(client)

        result = await checker.check(
            _signal(side="sell", qty="0.5"), Decimal("0.5"), Decimal("100")
        )

        assert result.ok is False
        assert result.rule == RULE_BALANCE

    async def test_contract_skips_balance_check(self) -> None:
        """合约可用保证金取决于杠杆，只看 availBal 会误杀正常订单。"""
        client = FakeClient(
            {"BTC-USDT-SWAP": SWAP}, balance=_balance("USDT", "0")
        )
        checker = PreflightChecker(client)

        result = await checker.check(
            _signal(inst_id="BTC-USDT-SWAP", qty="0.03"),
            Decimal("0.03"),
            Decimal("60000"),
        )

        assert result.ok is True
        assert result.balance_checked is False

    async def test_balance_is_cached(self) -> None:
        client = FakeClient(
            {"BTC-USDT": SPOT}, balance=_balance("USDT", "1000")
        )
        checker = PreflightChecker(client, balance_cache_seconds=60)

        await checker.check(_signal(), Decimal("0.001"), Decimal("100"))
        await checker.check(_signal(), Decimal("0.001"), Decimal("100"))

        assert client.balance_calls == 1

    async def test_balance_error_fails_open(self) -> None:
        """余额读不到时放行：预检是保护不是闸门。"""
        client = FakeClient(
            {"BTC-USDT": SPOT}, balance_error=RuntimeError("network down")
        )
        checker = PreflightChecker(client)

        result = await checker.check(
            _signal(qty="0.5"), Decimal("0.5"), Decimal("100")
        )

        assert result.ok is True
        assert result.balance_checked is False

    async def test_balance_without_currency_detail_fails_open(self) -> None:
        client = FakeClient(
            {"BTC-USDT": SPOT}, balance={"details": [{"ccy": "ETH", "availBal": "1"}]}
        )
        checker = PreflightChecker(client)

        result = await checker.check(
            _signal(qty="0.5"), Decimal("0.5"), Decimal("100")
        )

        assert result.ok is True
        assert result.balance_checked is False


class TestWorkerIntegration:
    @pytest.fixture
    def env(self, tmp_path):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        queue = SignalQueue(redis)
        risk = RiskChecker(
            RiskConfig(
                strategy_id="sma_cross",
                max_position_pct=Decimal("0.9"),
                max_total_pct=Decimal("0.95"),
                max_order_qty=Decimal("100000"),
                daily_loss_limit=Decimal("0.5"),
                consecutive_loss_limit=3,
            ),
            store,
        )
        yield store, queue, redis, risk
        store.close()

    async def test_preflight_rejection_recorded(self, env) -> None:
        store, queue, redis, risk = env
        worker = Worker(
            queue=queue,
            risk=risk,
            executor=Executor(),
            store=store,
            redis=redis,
            price_provider=lambda inst_id: Decimal("100"),
            heartbeat_interval=1,
            preflight=PreflightChecker(FakeClient()),  # 没有产品配置 → 必拒
        )
        await queue.push(_signal())

        await worker.run_once(timeout=1)

        assert store.get_signal_status("sig-1") == "rejected"
        events = store.list_risk_events()
        assert len(events) == 1
        assert events[0]["decision"] == "rejected"
        assert events[0]["rule"] == RULE_INSTRUMENT
        assert store.get_recent_trades(10) == []

    async def test_preflight_normalizes_qty_before_execution(self, env) -> None:
        store, queue, redis, risk = env
        worker = Worker(
            queue=queue,
            risk=risk,
            executor=Executor(),
            store=store,
            redis=redis,
            price_provider=lambda inst_id: Decimal("100"),
            heartbeat_interval=1,
            preflight=PreflightChecker(FakeClient({"BTC-USDT": SPOT})),
        )
        # 0.00123 → lotSz=0.0001 → 0.0012
        await queue.push(_signal(qty="0.00123"))

        await worker.run_once(timeout=1)

        assert store.get_signal_status("sig-1") == "executed"
        position = store.get_position("BTC-USDT")
        assert position.qty == Decimal("0.0012")

    async def test_without_preflight_behavior_unchanged(self, env) -> None:
        store, queue, redis, risk = env
        worker = Worker(
            queue=queue,
            risk=risk,
            executor=Executor(),
            store=store,
            redis=redis,
            price_provider=lambda inst_id: Decimal("100"),
            heartbeat_interval=1,
        )
        await queue.push(_signal(qty="0.00123"))

        await worker.run_once(timeout=1)

        assert store.get_signal_status("sig-1") == "executed"
        assert store.get_position("BTC-USDT").qty == Decimal("0.00123")
