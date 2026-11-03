"""KillSwitch 紧急停止开关测试（spec F4.5）。"""
from __future__ import annotations

import time
from decimal import Decimal

import fakeredis.aioredis

from quant.backtest.order import Fill
from quant.backtest.signal import Signal
from quant.execution.executor import Executor
from quant.execution.kill_switch import KillSwitch
from quant.execution.worker import Worker
from quant.queue.signal_queue import SignalQueue
from quant.risk.checker import RiskChecker
from quant.risk.config import RiskConfig
from quant.state.sqlite_store import SQLiteStore


def _signal(signal_id: str = "sig-1") -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side="buy",  # type: ignore[arg-type]
        order_type="market",
        inst_id="BTC-USDT",
        qty=Decimal("1"),
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


def _fill(side: str, price: str, ts: int) -> Fill:
    return Fill(
        order_id=f"o-{side}-{ts}",
        inst_id="BTC-USDT",
        side=side,  # type: ignore[arg-type]
        qty=Decimal("1"),
        price=Decimal(price),
        fee=Decimal("0"),
        ts=ts,
    )


class TestKillSwitch:
    async def test_default_inactive(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ks = KillSwitch(redis)
        assert await ks.is_active() is False
        assert await ks.reason() is None

    async def test_enable_disable(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ks = KillSwitch(redis)
        await ks.enable("manual stop")
        assert await ks.is_active() is True
        assert await ks.reason() == "manual stop"

        await ks.disable()
        assert await ks.is_active() is False
        assert await ks.reason() is None

    async def test_reason_default_empty(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ks = KillSwitch(redis)
        await ks.enable()
        # 空原因统一返回 None 而不是空串：前端据此判断「无原因」，避免显示空白
        assert await ks.reason() is None


class TestWorkerKillSwitch:
    async def test_rejects_signal_when_active(self, tmp_path) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        queue = SignalQueue(redis)
        ks = KillSwitch(redis)
        await ks.enable("panic")

        worker = Worker(
            queue=queue,
            risk=RiskChecker(_risk_config(), store),
            executor=Executor(),
            store=store,
            redis=redis,
            kill_switch=ks,
        )
        await queue.push(_signal())

        processed = await worker.run_once(timeout=1)

        assert processed is True
        assert store.get_signal_status("sig-1") is None
        assert store.get_recent_trades(10) == []
        assert await queue.dead_length() == 1
        assert await queue.inflight_length() == 0

    async def test_processes_when_inactive(self, tmp_path) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        queue = SignalQueue(redis)
        ks = KillSwitch(redis)

        worker = Worker(
            queue=queue,
            risk=RiskChecker(_risk_config(), store),
            executor=Executor(),
            store=store,
            redis=redis,
            kill_switch=ks,
        )
        await queue.push(_signal())

        await worker.run_once(timeout=1)

        assert store.get_signal_status("sig-1") == "executed"
        assert await queue.dead_length() == 0


class TestAutoKillSwitch:
    async def test_auto_enable_on_daily_loss(self, tmp_path) -> None:
        """日亏损触顶时自动启用 kill switch。"""
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        queue = SignalQueue(redis)
        ks = KillSwitch(redis)

        # 预置今日已实现亏损 999，daily_loss_limit 设为极小值
        now = int(time.time() * 1000)
        store.save_fill(_fill("buy", "1000", ts=now - 1000))
        store.save_fill(_fill("sell", "1", ts=now))

        worker = Worker(
            queue=queue,
            risk=RiskChecker(
                _risk_config(daily_loss_limit=Decimal("0.0001")), store
            ),
            executor=Executor(),
            store=store,
            redis=redis,
            kill_switch=ks,
        )
        await queue.push(_signal())

        await worker.run_once(timeout=1)

        assert await ks.is_active() is True
        assert "auto:" in (await ks.reason() or "")
        assert store.get_signal_status("sig-1") == "rejected"

    async def test_no_auto_enable_when_no_halt(self, tmp_path) -> None:
        """非硬性上限导致的拒绝不应启用 kill switch。"""
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(tmp_path / "quant.db")
        queue = SignalQueue(redis)
        ks = KillSwitch(redis)

        worker = Worker(
            queue=queue,
            risk=RiskChecker(
                _risk_config(max_order_qty=Decimal("1")), store
            ),
            executor=Executor(),
            store=store,
            redis=redis,
            kill_switch=ks,
            price_provider=lambda inst_id: Decimal("100"),
        )
        await queue.push(_signal())

        await worker.run_once(timeout=1)

        assert await ks.is_active() is False
