"""Redis 队列与去重单元测试（spec §7-§8，使用 fakeredis）。"""
from __future__ import annotations

import json
from decimal import Decimal

import fakeredis.aioredis
import pytest

from quant.backtest.signal import Signal
from quant.queue.signal_queue import Deduper, SignalQueue


def _signal(
    strategy_id: str = "sma_cross",
    side: str = "buy",
    inst_id: str = "BTC-USDT",
    qty: str = "1",
    ts: int = 1_700_000_000_000,
    signal_id: str = "sig-1",
) -> Signal:
    return Signal(
        strategy_id=strategy_id,
        side=side,  # type: ignore[arg-type]
        order_type="market",
        inst_id=inst_id,
        qty=Decimal(qty),
        ts=ts,
        signal_id=signal_id,
    )


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


# =====================================================================
# SignalQueue
# =====================================================================


class TestSignalQueue:
    async def test_push_and_pop(self, redis) -> None:
        queue = SignalQueue(redis)
        await queue.push(_signal())

        assert await queue.pending_length() == 1
        signal = await queue.pop(timeout=1)

        assert signal is not None
        assert signal.signal_id == "sig-1"
        assert str(signal.qty) == "1"
        assert await queue.pending_length() == 0
        # pop 是原子搬移：pending → inflight
        assert await queue.inflight_length() == 1

    async def test_pop_empty_returns_none(self, redis) -> None:
        queue = SignalQueue(redis)
        assert await queue.pop(timeout=1) is None

    async def test_remove_inflight(self, redis) -> None:
        queue = SignalQueue(redis)
        signal = _signal()
        await queue.push(signal)
        await queue.pop(timeout=1)

        await queue.remove_inflight(signal)
        assert await queue.inflight_length() == 0

    async def test_remove_inflight_other_signal_keeps(self, redis) -> None:
        queue = SignalQueue(redis)
        await queue.push(_signal(signal_id="a"))
        popped = await queue.pop(timeout=1)

        await queue.remove_inflight(_signal(signal_id="b"))
        assert await queue.inflight_length() == 1
        assert popped is not None

    async def test_push_dead(self, redis) -> None:
        queue = SignalQueue(redis)
        await queue.push_dead(_signal(), reason="risk rejected")

        assert await queue.dead_length() == 1
        raw = await redis.lindex(queue.dead_key, 0)
        payload = json.loads(raw)
        assert payload["reason"] == "risk rejected"
        assert payload["signal"]["signal_id"] == "sig-1"

    async def test_push_dead_raw(self, redis) -> None:
        queue = SignalQueue(redis)
        await queue.push_dead_raw("{bad json", reason="parse error")

        raw = await redis.lindex(queue.dead_key, 0)
        payload = json.loads(raw)
        assert payload["raw"] == "{bad json"

    async def test_recover_inflight_restores_order(self, redis) -> None:
        queue = SignalQueue(redis)
        await queue.push(_signal(signal_id="a", ts=1))
        await queue.push(_signal(signal_id="b", ts=2))
        # 模拟 worker 取出两条后崩溃
        await queue.pop(timeout=1)
        await queue.pop(timeout=1)
        assert await queue.pending_length() == 0
        assert await queue.inflight_length() == 2

        recovered = await queue.recover_inflight()

        assert [s.signal_id for s in recovered] == ["a", "b"]
        assert await queue.inflight_length() == 0
        assert await queue.pending_length() == 2
        # 恢复后仍可正常消费，且顺序为 a → b
        first = await queue.pop(timeout=1)
        second = await queue.pop(timeout=1)
        assert first is not None and first.signal_id == "a"
        assert second is not None and second.signal_id == "b"

    async def test_recover_corrupt_payload_goes_dead(self, redis) -> None:
        queue = SignalQueue(redis)
        await redis.lpush(queue.inflight_key, json.dumps({"broken": True}))

        recovered = await queue.recover_inflight()

        assert recovered == []
        assert await queue.dead_length() == 1
        assert await queue.inflight_length() == 0


# =====================================================================
# Deduper
# =====================================================================


class TestDeduper:
    async def test_first_not_duplicate_second_is(self, redis) -> None:
        deduper = Deduper(redis)
        signal = _signal()

        assert await deduper.is_duplicate(signal) is False
        assert await deduper.is_duplicate(signal) is True

    async def test_signal_id_ignored_in_hash(self, redis) -> None:
        """signal_id 每次不同但内容相同 → 视为重复。"""
        deduper = Deduper(redis)

        assert await deduper.is_duplicate(_signal(signal_id="a")) is False
        assert await deduper.is_duplicate(_signal(signal_id="b")) is True

    async def test_ts_ignored_in_hash(self, redis) -> None:
        """TV 重发时服务端时间戳不同，仍应识别为重复。"""
        deduper = Deduper(redis)

        assert await deduper.is_duplicate(_signal(ts=1000)) is False
        assert await deduper.is_duplicate(_signal(ts=9999)) is True

    async def test_different_content_not_duplicate(self, redis) -> None:
        deduper = Deduper(redis)

        assert await deduper.is_duplicate(_signal(inst_id="BTC-USDT")) is False
        assert await deduper.is_duplicate(_signal(inst_id="ETH-USDT")) is False

    async def test_ttl_set(self, redis) -> None:
        deduper = Deduper(redis, ttl_seconds=300)
        signal = _signal()
        await deduper.is_duplicate(signal)

        ttl = await redis.ttl(deduper.key_for(signal))
        assert 0 < ttl <= 300

    async def test_key_prefix(self, redis) -> None:
        deduper = Deduper(redis, prefix="test:dedup:")
        key = deduper.key_for(_signal())
        assert key.startswith("test:dedup:")
        assert len(key) == len("test:dedup:") + 64
