"""统一心跳工具与进程判活测试（MS4-A §7）。"""
from __future__ import annotations

import asyncio
import json

import fakeredis.aioredis

from quant.foundation.heartbeat import (
    PROCESS_SPECS,
    SUPERVISOR_KEY,
    Heartbeat,
    default_ttl,
    parse_heartbeat,
)


class TestDefaultTtl:
    def test_doubles_interval(self) -> None:
        assert default_ttl(30) == 60
        assert default_ttl(60) == 120

    def test_floor_is_sixty_seconds(self) -> None:
        assert default_ttl(5) == 60

    def test_monitor_exception_keeps_300(self) -> None:
        spec = {s.name: s for s in PROCESS_SPECS}["monitor"]
        assert spec.ttl_seconds == 300

    def test_supervisor_exception_is_15(self) -> None:
        spec = {s.name: s for s in PROCESS_SPECS}["supervisor"]
        assert spec.ttl_seconds == 15

    def test_has_seven_processes(self) -> None:
        assert {s.name for s in PROCESS_SPECS} == {
            "supervisor",
            "candles",
            "webhook",
            "worker",
            "engine",
            "monitor",
            "dashboard",
        }


class TestParseHeartbeat:
    def test_json_payload(self) -> None:
        raw = json.dumps({"ts": 123, "pid": 456})
        assert parse_heartbeat(raw) == (123, 456)

    def test_legacy_raw_timestamp(self) -> None:
        """worker/monitor 存量代码写裸时间戳。"""
        assert parse_heartbeat("123") == (123, None)

    def test_bytes(self) -> None:
        assert parse_heartbeat(b"123") == (123, None)

    def test_missing_key(self) -> None:
        assert parse_heartbeat(None) == (None, None)

    def test_garbage(self) -> None:
        assert parse_heartbeat("not-a-ts") == (None, None)
        assert parse_heartbeat("{bad json") == (None, None)

    def test_empty_string(self) -> None:
        assert parse_heartbeat("") == (None, None)


class TestHeartbeatBeat:
    async def test_writes_json_with_ttl(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        hb = Heartbeat(redis, "webhook", interval=30)

        await hb.beat()

        raw = await redis.get("quant:heartbeat:webhook")
        payload = json.loads(raw)
        assert payload["ts"] > 0
        assert payload["pid"] > 0
        ttl = await redis.ttl("quant:heartbeat:webhook")
        assert 0 < ttl <= 60

    async def test_extra_fields_merged(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        hb = Heartbeat(redis, "engine", interval=60)

        await hb.beat({"strategy": "sma_cross", "inst": "BTC-USDT"})

        payload = json.loads(await redis.get("quant:heartbeat:engine"))
        assert payload["strategy"] == "sma_cross"

    async def test_write_failure_does_not_raise(self) -> None:
        hb = Heartbeat(_BrokenRedis(), "webhook", interval=30)
        await hb.beat()  # 不应抛出

    async def test_explicit_ttl_overrides_default(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        hb = Heartbeat(redis, "supervisor", interval=5, ttl=15)

        await hb.beat()

        assert 0 < await redis.ttl("quant:heartbeat:supervisor") <= 15


class TestHeartbeatLoop:
    async def test_start_and_stop(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        hb = Heartbeat(redis, "dashboard", interval=1)

        hb.start()
        await asyncio.sleep(0.05)
        await hb.stop()

        assert await redis.exists("quant:heartbeat:dashboard")

    async def test_start_is_idempotent(self) -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        hb = Heartbeat(redis, "dashboard", interval=1)
        try:
            hb.start()
            first = hb._task
            hb.start()
            assert hb._task is first
        finally:
            await hb.stop()

    async def test_stop_without_start(self) -> None:
        hb = Heartbeat(_BrokenRedis(), "dashboard")
        await hb.stop()  # 不应抛出


class TestProcessTable:
    def test_supervisor_key_is_separate(self) -> None:
        """supervisor 不另写心跳键：进程表本身就是判活信号。"""
        assert SUPERVISOR_KEY == "quant:process:supervisor"


class TestSyncCandlesHeartbeat:
    """candles 是同步 CLI，单独用同步客户端逐轮写心跳。"""

    def test_beat_writes_key(self) -> None:
        from quant.cli.sync_candles import _beat

        fake = _RecordingSyncRedis()

        _beat(fake, 300, _LogStub())

        key, raw, ex = fake.calls[0]
        assert key == "quant:heartbeat:candles"
        assert ex == 600  # default_ttl(300)
        assert json.loads(raw)["pid"] > 0

    def test_beat_without_client_is_noop(self) -> None:
        from quant.cli.sync_candles import _beat

        _beat(None, 300, _LogStub())

    def test_beat_failure_does_not_raise(self) -> None:
        from quant.cli.sync_candles import _beat

        _beat(_BrokenSyncRedis(), 300, _LogStub())

    def test_connect_redis_returns_none_on_failure(self, monkeypatch) -> None:
        from quant.cli import sync_candles

        def boom(*_args, **_kwargs):
            raise RuntimeError("redis down")

        monkeypatch.setattr(sync_candles.SyncRedis, "from_url", boom)

        class _Settings:
            class redis:  # noqa: N801 - 只用到 url
                url = "redis://localhost:6379/0"

        assert sync_candles._connect_redis(_Settings(), _LogStub()) is None


class _RecordingSyncRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def set(self, key: str, value: str, ex: int) -> None:
        self.calls.append((key, value, ex))


class _LogStub:
    def warning(self, *_args, **_kwargs) -> None:
        pass


class _BrokenRedis:
    async def set(self, *_args, **_kwargs):
        raise RuntimeError("redis down")

    async def get(self, *_args, **_kwargs):
        raise RuntimeError("redis down")


class _BrokenSyncRedis:
    def set(self, *_args, **_kwargs) -> None:
        raise RuntimeError("redis down")
