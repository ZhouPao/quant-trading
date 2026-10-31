"""服务端仓位计算测试（M8-14）。"""
from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis
import pytest

from quant.backtest.signal import Signal
from quant.execution.executor import Executor
from quant.execution.sizing import (
    CAPPED_BY_LOT_ROUND,
    CAPPED_BY_MAX_ORDER,
    SIM_SPEC,
    InstrumentSpec,
    PositionSizer,
)
from quant.execution.worker import Worker
from quant.queue.signal_queue import SignalQueue
from quant.risk.checker import RiskChecker
from quant.risk.config import RiskConfig
from quant.risk.limits import RiskLimits
from quant.state.sqlite_store import SQLiteStore

SPOT = InstrumentSpec(lot_sz=Decimal("0.0001"), min_sz=Decimal("0.0001"))
SWAP = InstrumentSpec(
    lot_sz=Decimal("1"),
    min_sz=Decimal("1"),
    ct_val=Decimal("0.01"),
    inst_type="SWAP",
)


def _signal(
    qty: str | None = None,
    size_pct: str | None = None,
    signal_id: str = "sig-1",
) -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side="buy",
        order_type="market",
        inst_id="BTC-USDT",
        qty=Decimal(qty) if qty is not None else None,
        ts=1_700_000_000_000,
        signal_id=signal_id,
        size_pct=Decimal(size_pct) if size_pct is not None else None,
    )


def _limits(**overrides) -> RiskLimits:
    values = {
        "strategy_id": "sma_cross",
        "max_position_pct": Decimal("0.9"),
        "max_total_pct": Decimal("0.95"),
        "max_order_qty": Decimal("100000"),
        "daily_loss_limit": Decimal("0.5"),
        "consecutive_loss_limit": 3,
    }
    values.update(overrides)
    return RiskLimits(**values)


class TestSignalModel:
    def test_qty_optional(self) -> None:
        assert _signal().qty is None

    def test_zero_qty_rejected(self) -> None:
        with pytest.raises(ValueError, match="qty must be positive"):
            _signal(qty="0")

    def test_non_positive_size_pct_rejected(self) -> None:
        with pytest.raises(ValueError, match="size_pct must be positive"):
            _signal(size_pct="0")

    def test_json_roundtrip_without_qty(self) -> None:
        original = _signal(size_pct="0.2")
        restored = Signal.from_json(original.to_json())

        assert restored.qty is None
        assert restored.size_pct == Decimal("0.2")


class TestSizing:
    def test_spot_basic(self) -> None:
        result = PositionSizer(Decimal("0.1")).size(
            _signal(), Decimal("10000"), Decimal("100"), _limits(), spec=SPOT
        )
        assert result is not None
        assert result.qty == Decimal("10")
        assert result.raw_qty == Decimal("10")
        assert result.notional == Decimal("1000")
        assert result.capped_by is None

    def test_rounds_down_to_lot(self) -> None:
        result = PositionSizer(Decimal("0.1")).size(
            _signal(), Decimal("1000"), Decimal("3"), _limits(), spec=SPOT
        )
        assert result is not None
        assert result.qty == Decimal("33.3333")
        assert result.capped_by == CAPPED_BY_LOT_ROUND

    def test_capped_by_max_order_qty(self) -> None:
        result = PositionSizer(Decimal("0.1")).size(
            _signal(),
            Decimal("10000"),
            Decimal("100"),
            _limits(max_order_qty=Decimal("100")),
            spec=SPOT,
        )
        assert result is not None
        assert result.qty == Decimal("1")
        assert result.capped_by == CAPPED_BY_MAX_ORDER

    def test_below_min_size_rejected(self) -> None:
        spec = InstrumentSpec(lot_sz=Decimal("0.0001"), min_sz=Decimal("1"))
        assert (
            PositionSizer(Decimal("0.1")).size(
                _signal(), Decimal("100"), Decimal("100"), _limits(), spec=spec
            )
            is None
        )

    def test_contract_converts_via_ct_val(self) -> None:
        result = PositionSizer(Decimal("0.1")).size(
            _signal(), Decimal("10000"), Decimal("50000"), _limits(), spec=SWAP
        )
        assert result is not None
        # 1000 USDT / 50000 = 0.02 BTC；ctVal=0.01 → 2 张 → 折回 0.02 BTC
        assert result.qty == Decimal("0.02")

    def test_contract_rounds_to_zero_contracts_rejected(self) -> None:
        # 0.1 × 100 / 50000 = 0.0002 BTC → 不足 1 张
        assert (
            PositionSizer(Decimal("0.1")).size(
                _signal(), Decimal("100"), Decimal("50000"), _limits(), spec=SWAP
            )
            is None
        )

    def test_contract_min_size_in_contracts(self) -> None:
        spec = InstrumentSpec(
            lot_sz=Decimal("1"),
            min_sz=Decimal("3"),
            ct_val=Decimal("0.01"),
            inst_type="SWAP",
        )
        assert (
            PositionSizer(Decimal("0.1")).size(
                _signal(), Decimal("10000"), Decimal("50000"), _limits(), spec=spec
            )
            is None
        )

    def test_missing_equity_or_price(self) -> None:
        sizer = PositionSizer(Decimal("0.1"))
        assert sizer.size(_signal(), None, Decimal("100"), _limits(), spec=SPOT) is None
        assert sizer.size(_signal(), Decimal("0"), Decimal("100"), _limits(), spec=SPOT) is None
        assert sizer.size(_signal(), Decimal("10000"), None, _limits(), spec=SPOT) is None

    def test_signal_size_pct_overrides_default(self) -> None:
        result = PositionSizer(Decimal("0.1")).size(
            _signal(size_pct="0.5"),
            Decimal("10000"),
            Decimal("100"),
            _limits(max_order_qty=Decimal("100000")),
            spec=SPOT,
        )
        assert result is not None
        assert result.size_pct == Decimal("0.5")
        assert result.qty == Decimal("50")

    def test_sim_spec_allows_fractional(self) -> None:
        result = PositionSizer(Decimal("0.1")).size(
            _signal(), Decimal("1000"), Decimal("3"), None, spec=None
        )
        assert result is not None
        assert result.qty == Decimal("33.33333333")
        assert SIM_SPEC.lot_sz == Decimal("0.00000001")


def _risk_config() -> RiskConfig:
    return RiskConfig(
        strategy_id="sma_cross",
        max_position_pct=Decimal("0.9"),
        max_total_pct=Decimal("0.95"),
        max_order_qty=Decimal("100000"),
        daily_loss_limit=Decimal("0.5"),
        consecutive_loss_limit=3,
    )


@pytest.fixture
def env(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SQLiteStore(tmp_path / "quant.db")
    queue = SignalQueue(redis)
    risk = RiskChecker(_risk_config(), store)

    async def equity_provider() -> Decimal | None:
        return Decimal("10000")

    worker = Worker(
        queue=queue,
        risk=risk,
        executor=Executor(),
        store=store,
        redis=redis,
        price_provider=lambda inst_id: Decimal("100"),
        heartbeat_interval=1,
        equity_provider=equity_provider,
        sizer=PositionSizer(Decimal("0.1")),
    )
    yield worker, queue, store, redis
    store.close()


class TestWorkerSizing:
    async def test_sizes_and_places_order(self, env) -> None:
        worker, queue, store, _ = env
        await queue.push(_signal())

        assert await worker.run_once(timeout=1) is True

        assert store.get_signal_status("sig-1") == "executed"
        assert store.get_position("BTC-USDT").qty == Decimal("10")
        events = store.list_risk_events(decision="sized")
        assert len(events) == 1
        assert events[0]["rule"] in ("sized", CAPPED_BY_LOT_ROUND)
        assert Decimal(events[0]["qty_after"]) == Decimal("10")

    async def test_qty_wins_over_sizing(self, env) -> None:
        worker, queue, store, _ = env
        await queue.push(_signal(qty="2"))

        await worker.run_once(timeout=1)

        assert store.get_position("BTC-USDT").qty == Decimal("2")
        assert store.list_risk_events(decision="sized") == []

    async def test_rejects_without_sizer(self, tmp_path) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "no_sizer.db")
        queue = SignalQueue(redis)
        worker = Worker(
            queue=queue,
            risk=RiskChecker(_risk_config(), store),
            executor=Executor(),
            store=store,
            redis=redis,
            price_provider=lambda inst_id: Decimal("100"),
        )
        try:
            await queue.push(_signal())
            await worker.run_once(timeout=1)

            assert store.get_signal_status("sig-1") == "rejected"
            assert store.get_signal("sig-1")["qty"] == "0"
            assert store.get_recent_trades(10) == []
            events = store.list_risk_events(decision="rejected")
            assert events and events[0]["rule"] == "position_sizing"
        finally:
            store.close()

    async def test_rejects_when_price_missing(self, env) -> None:
        worker, queue, store, _ = env
        worker.price_provider = lambda inst_id: None

        await queue.push(_signal())
        await worker.run_once(timeout=1)

        assert store.get_signal_status("sig-1") == "rejected"
        events = store.list_risk_events(decision="rejected")
        assert events and "no market price" in events[0]["reason"]

    async def test_rejects_when_equity_missing(self, env) -> None:
        worker, queue, store, _ = env

        async def no_equity() -> Decimal | None:
            return None

        worker.equity_provider = no_equity
        await queue.push(_signal())
        await worker.run_once(timeout=1)

        assert store.get_signal_status("sig-1") == "rejected"
        events = store.list_risk_events(decision="rejected")
        assert events and "account snapshot" in events[0]["reason"]


class TestSizerGuards:
    def test_default_size_pct_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="default_size_pct"):
            PositionSizer(Decimal("0"))

    def test_execution_settings_reject_non_positive(self) -> None:
        """配置加载期就拒绝非法默认比例，而不是逐单运行期失败。"""
        from pydantic import ValidationError

        from quant.foundation.config import ExecutionSettings

        with pytest.raises(ValidationError):
            ExecutionSettings(default_size_pct=Decimal("0"))

    async def test_spec_provider_failure_falls_back(self, tmp_path) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "spec_fail.db")
        queue = SignalQueue(redis)

        async def broken_spec(inst_id: str):
            raise RuntimeError("boom")

        async def equity() -> Decimal | None:
            return Decimal("10000")

        worker = Worker(
            queue=queue,
            risk=RiskChecker(_risk_config(), store),
            executor=Executor(),
            store=store,
            redis=redis,
            price_provider=lambda inst_id: Decimal("100"),
            equity_provider=equity,
            sizer=PositionSizer(Decimal("0.1")),
            spec_provider=broken_spec,
        )
        try:
            await queue.push(_signal())
            await worker.run_once(timeout=1)
            assert store.get_signal_status("sig-1") == "executed"
            assert store.get_position("BTC-USDT").qty == Decimal("10")
        finally:
            store.close()
