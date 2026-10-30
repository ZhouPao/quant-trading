"""Worker 主循环单元测试（spec §9）。"""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import fakeredis.aioredis
import pytest

from quant.backtest.order import Fill
from quant.backtest.signal import Signal
from quant.execution.account import read_account_field
from quant.execution.executor import Executor
from quant.execution.worker import Worker
from quant.foundation.schedule import day_start_ms_utc8
from quant.queue.signal_queue import SignalQueue
from quant.risk.checker import RiskChecker
from quant.risk.config import RiskConfig
from quant.state.sqlite_store import SQLiteStore


def _signal(signal_id: str = "sig-1", qty: str = "1", side: str = "buy") -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side=side,  # type: ignore[arg-type]
        order_type="market",
        inst_id="BTC-USDT",
        qty=Decimal(qty),
        ts=1_700_000_000_000,
        signal_id=signal_id,
    )


def _risk_config(**overrides) -> RiskConfig:
    defaults = {
        "strategy_id": "sma_cross",
        "max_position_pct": Decimal("0.9"),
        "max_total_pct": Decimal("0.95"),
        "max_order_qty": Decimal("100000"),
        "daily_loss_limit": Decimal("0.5"),
        "consecutive_loss_limit": 3,
    }
    defaults.update(overrides)
    return RiskConfig(**defaults)


@pytest.fixture
def env(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SQLiteStore(tmp_path / "quant.db")
    queue = SignalQueue(redis)
    risk = RiskChecker(_risk_config(), store)
    worker = Worker(
        queue=queue,
        risk=risk,
        executor=Executor(),
        store=store,
        redis=redis,
        price_provider=lambda inst_id: Decimal("100"),
        heartbeat_interval=1,
    )
    yield worker, queue, store, redis
    store.close()


class TestWorkerProcess:
    async def test_processes_signal(self, env) -> None:
        worker, queue, store, _ = env
        signal = _signal()
        await queue.push(signal)

        processed = await worker.run_once(timeout=1)

        assert processed is True
        assert store.get_signal_status("sig-1") == "executed"
        position = store.get_position("BTC-USDT")
        assert position.qty == Decimal("1")
        assert position.avg_price == Decimal("100")
        assert await queue.pending_length() == 0
        assert await queue.inflight_length() == 0

    async def test_records_fill(self, env) -> None:
        worker, queue, store, _ = env
        await queue.push(_signal())

        await worker.run_once(timeout=1)

        trades = store.get_recent_trades(10)
        assert len(trades) == 1
        assert trades[0].price == Decimal("100")
        assert trades[0].fee == Decimal("0.1")

    async def test_no_signal_returns_false(self, env) -> None:
        worker, _, _, _ = env
        assert await worker.run_once(timeout=1) is False

    async def test_rejected_signal_recorded(self, env) -> None:
        worker, queue, store, _ = env
        # 构造今日已实现亏损，触发日亏损上限
        now = int(time.time() * 1000)
        store.save_fill(
            Fill(
                order_id="o-loss-buy",
                inst_id="BTC-USDT",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("1000"),
                fee=Decimal("0"),
                ts=now - 1000,
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
                ts=now,
            )
        )
        worker.risk = RiskChecker(
            _risk_config(daily_loss_limit=Decimal("0.0001")), store
        )
        await queue.push(_signal())

        await worker.run_once(timeout=1)

        assert store.get_signal_status("sig-1") == "rejected"
        assert store.get_recent_trades(10)  # 只有预置的两笔，无新成交
        assert len(store.get_recent_trades(10)) == 2

    async def test_rejected_signal_writes_risk_event(self, env) -> None:
        """拒绝必须留痕，否则事后无法回答「为什么拒了这单」。"""
        worker, queue, store, _ = env
        worker.risk = RiskChecker(
            _risk_config(max_position_pct=Decimal("0")), store
        )
        await queue.push(_signal())

        await worker.run_once(timeout=1)

        assert store.get_signal_status("sig-1") == "rejected"
        events = store.list_risk_events()
        assert len(events) == 1
        assert events[0]["decision"] == "rejected"
        assert events[0]["rule"] == "max_position_pct"
        assert events[0]["signal_id"] == "sig-1"

    async def test_adjusted_signal_writes_risk_event(self, env) -> None:
        worker, queue, store, _ = env
        worker.risk = RiskChecker(
            _risk_config(max_order_qty=Decimal("50")), store
        )
        await queue.push(_signal(qty="10"))  # 10 * 100 = 1000 > 50

        await worker.run_once(timeout=1)

        events = store.list_risk_events()
        assert len(events) == 1
        assert events[0]["decision"] == "adjusted"
        assert events[0]["qty_after"] == "0.5"

    async def test_daily_loss_resets_at_utc8_midnight(self, env) -> None:
        """昨日亏损不计入今日额度：worker 传的是 UTC+8 当日零点。"""
        worker, queue, store, _ = env
        day_start = day_start_ms_utc8()
        store.save_fill(
            Fill(
                order_id="o-old-buy",
                inst_id="BTC-USDT",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("1000"),
                fee=Decimal("0"),
                ts=day_start - 2000,
            )
        )
        store.save_fill(
            Fill(
                order_id="o-old-sell",
                inst_id="BTC-USDT",
                side="sell",
                qty=Decimal("1"),
                price=Decimal("1"),
                fee=Decimal("0"),
                ts=day_start - 1000,
            )
        )
        worker.risk = RiskChecker(
            _risk_config(daily_loss_limit=Decimal("0.0001")), store
        )
        await queue.push(_signal())

        await worker.run_once(timeout=1)

        assert store.get_signal_status("sig-1") == "executed"

    async def test_publishes_effective_risk_limits(self, env) -> None:
        """worker 发布生效值（含 paper-mode 覆盖），看板据此展示。"""
        worker, _, _, redis = env
        worker.risk_limits_key = "quant:risk:limits:sma_cross"
        worker.risk_limits = {
            "strategy": "sma_cross",
            "paper_mode": True,
            "rules": {"max_position_pct": "1.0"},
        }

        await worker._publish_risk_limits()

        raw = await redis.get("quant:risk:limits:sma_cross")
        payload = json.loads(raw)
        assert payload["paper_mode"] is True
        assert payload["rules"]["max_position_pct"] == "1.0"
        assert payload["ts"] > 0

    async def test_idempotent_skip_executed(self, env) -> None:
        worker, queue, store, _ = env
        signal = _signal()
        store.save_signal(signal, "executed")
        await queue.push(signal)

        await worker.run_once(timeout=1)

        assert store.get_recent_trades(10) == []
        assert await queue.inflight_length() == 0

    async def test_equity_provider_sets_risk_base(self, env) -> None:
        """风控基数取账户快照权益：下单量不再被配置初始资金截断。"""
        worker, queue, store, _ = env
        worker.risk = RiskChecker(
            _risk_config(max_position_pct=Decimal("0.001")), store
        )

        async def equity() -> Decimal:
            return Decimal("50000")

        worker.equity_provider = equity
        await queue.push(_signal(qty="1"))

        await worker.run_once(timeout=1)

        # 50000 * 0.001 = 50 USDT，price=100 → 0.5
        assert store.get_position("BTC-USDT").qty == Decimal("0.5")

    async def test_equity_provider_none_keeps_initial_equity(self, env) -> None:
        """快照不可用时退回 initial_equity（默认 10000）。"""
        worker, queue, store, _ = env
        worker.risk = RiskChecker(
            _risk_config(max_position_pct=Decimal("0.001")), store
        )

        async def equity() -> None:
            return None

        worker.equity_provider = equity
        await queue.push(_signal(qty="1"))

        await worker.run_once(timeout=1)

        # 10000 * 0.001 = 10 USDT，price=100 → 0.1
        assert store.get_position("BTC-USDT").qty == Decimal("0.1")

    async def test_executor_error_goes_dead(self, env) -> None:
        worker, queue, store, _ = env

        class BoomExecutor(Executor):
            async def execute(self, signal, qty, market_price=None):
                raise RuntimeError("exchange down")

        worker.executor = BoomExecutor()
        await queue.push(_signal())

        await worker.run_once(timeout=1)

        assert await queue.dead_length() == 1
        assert await queue.inflight_length() == 0
        # 未标记 executed，恢复后可重试
        assert store.get_signal_status("sig-1") is None

    async def test_pending_order_without_price(self, env) -> None:
        worker, queue, store, _ = env
        worker.price_provider = lambda inst_id: None
        await queue.push(_signal())

        await worker.run_once(timeout=1)

        assert store.get_signal_status("sig-1") == "executed"
        # 无成交 → 持仓不变
        assert store.get_position("BTC-USDT").is_flat


class TestWorkerRecovery:
    async def test_run_recovers_inflight(self, env) -> None:
        worker, queue, store, _ = env
        await queue.push(_signal())
        # 模拟上次进程崩溃在 inflight
        await queue.pop(timeout=1)
        assert await queue.inflight_length() == 1

        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.2)
        worker.stop()
        await asyncio.wait_for(task, timeout=2)

        assert store.get_signal_status("sig-1") == "executed"
        assert await queue.inflight_length() == 0

    async def test_heartbeat_written(self, env) -> None:
        worker, _, _, redis = env
        worker._running = True
        task = asyncio.create_task(worker._heartbeat_loop())
        await asyncio.sleep(0.1)
        worker._running = False
        await asyncio.wait_for(task, timeout=2)

        assert await redis.get(Worker.HEARTBEAT_KEY) is not None


class _StubReconciler:
    def __init__(self, changed: int = 0, error: Exception | None = None) -> None:
        self.changed = changed
        self.error = error
        self.calls = 0

    async def reconcile(self) -> int:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.changed


class TestWorkerReconcileLoop:
    async def test_reconcile_once_calls_reconciler(self, env) -> None:
        worker, _, _, _ = env
        stub = _StubReconciler(changed=2)
        worker.reconciler = stub

        await worker._reconcile_once()

        assert stub.calls == 1

    async def test_reconcile_once_swallows_error(self, env) -> None:
        worker, _, _, _ = env
        worker.reconciler = _StubReconciler(error=RuntimeError("boom"))

        # 对账异常不能冒泡到 worker 主循环
        await worker._reconcile_once()

    async def test_reconcile_loop_runs_until_stopped(self, env) -> None:
        worker, _, _, _ = env
        stub = _StubReconciler(changed=1)
        worker.reconciler = stub
        worker.reconcile_interval = 0.01
        worker._running = True

        task = asyncio.create_task(worker._reconcile_loop())
        await asyncio.sleep(0.05)
        worker._running = False
        await asyncio.wait_for(task, timeout=2)

        assert stub.calls >= 1


class TestReadAccountField:
    """engine 与 worker 共用的快照读取，任何异常都返回 None 交给调用方兜底。"""

    async def test_reads_value(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(
            "snap", json.dumps({"account": {"total_equity": "97945.5"}})
        )

        assert await read_account_field(
            redis, "snap", "total_equity"
        ) == Decimal("97945.5")
        await redis.aclose()

    async def test_missing_key_returns_none(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

        assert await read_account_field(redis, "snap", "avail_usdt") is None
        await redis.aclose()

    @pytest.mark.parametrize(
        "payload",
        [
            "not-json",
            json.dumps({"account": {}}),
            json.dumps({"account": {"avail_usdt": "0"}}),
            json.dumps({"account": {"avail_usdt": "abc"}}),
        ],
    )
    async def test_malformed_returns_none(self, payload: str) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set("snap", payload)

        assert await read_account_field(redis, "snap", "avail_usdt") is None
        await redis.aclose()

    async def test_fresh_snapshot_passes_age_check(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(
            "snap",
            json.dumps(
                {"ts": 10_000, "account": {"total_equity": "97945.5"}}
            ),
        )

        assert await read_account_field(
            redis,
            "snap",
            "total_equity",
            now_ms=10_000 + 1_000,
            max_age_ms=180_000,
        ) == Decimal("97945.5")
        await redis.aclose()

    async def test_stale_snapshot_returns_none(self) -> None:
        """monitor 卡死后键仍在 TTL 内，但数值已过时（MS4-B §5.3）。"""
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(
            "snap",
            json.dumps(
                {"ts": 10_000, "account": {"total_equity": "97945.5"}}
            ),
        )

        assert (
            await read_account_field(
                redis,
                "snap",
                "total_equity",
                now_ms=10_000 + 180_001,
                max_age_ms=180_000,
            )
            is None
        )
        await redis.aclose()

    async def test_missing_ts_returns_none_when_age_check_enabled(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(
            "snap", json.dumps({"account": {"total_equity": "100"}})
        )

        assert (
            await read_account_field(
                redis,
                "snap",
                "total_equity",
                now_ms=10_000,
                max_age_ms=180_000,
            )
            is None
        )
        await redis.aclose()

    async def test_age_check_off_keeps_legacy_behavior(self) -> None:
        """engine 侧的 avail_usdt 不走新鲜度校验，行为保持不变。"""
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(
            "snap",
            json.dumps({"ts": 1, "account": {"avail_usdt": "100"}}),
        )

        assert await read_account_field(
            redis, "snap", "avail_usdt"
        ) == Decimal("100")
        await redis.aclose()
