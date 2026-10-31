"""Redis 信号队列 + 去重（spec §7-§8）。

队列模型（可靠队列）：
- pending  LIST：待处理，webhook 侧 LPUSH
- inflight LIST：处理中，worker 用 BRPOPLPUSH 原子移入
- dead     LIST：死信（解析失败/风控异常/处理失败）

去重：每个信号 hash 一个 SET NX + EX 键，TTL 5 分钟。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from redis.asyncio import Redis

from quant.backtest.signal import Signal
from quant.foundation.logger import get_logger

if TYPE_CHECKING:
    from quant.state.sqlite_store import SQLiteStore

log = get_logger("queue.signal")


class SignalQueue:
    """Redis LIST 信号队列封装（异步）。

    传入 `store` 时死信会**双写**：Redis LIST 供 monitor 计数告警，
    SQLite 作为可查询、可重放的权威记录（MS4-B §10.1）。
    """

    def __init__(
        self,
        redis: Redis,
        pending_key: str = "quant:signals",
        inflight_key: str = "quant:signals:inflight",
        dead_key: str = "quant:signals:dead",
        store: SQLiteStore | None = None,
    ) -> None:
        self._redis = redis
        self._store = store
        self.pending_key = pending_key
        self.inflight_key = inflight_key
        self.dead_key = dead_key

    async def push(self, signal: Signal) -> None:
        """LPUSH 到 pending。"""
        await self._redis.lpush(self.pending_key, _dumps(signal.to_json()))

    async def pop(self, timeout: int = 1) -> Signal | None:
        """BRPOPLPUSH pending → inflight，返回 Signal；超时返回 None。"""
        raw = await self._redis.brpoplpush(
            self.pending_key, self.inflight_key, timeout=timeout
        )
        if raw is None:
            return None
        return _loads_signal(raw)

    async def remove_inflight(self, signal: Signal) -> None:
        """处理成功后从 inflight 精确移除该信号。"""
        await self._redis.lrem(self.inflight_key, 0, _dumps(signal.to_json()))

    async def push_dead(
        self, signal: Signal, reason: str, *, stage: str = "submit"
    ) -> None:
        """处理失败信号 → 死信队列（带 reason），并落 SQLite。"""
        payload = {"signal": signal.to_json(), "reason": reason}
        await self._redis.lpush(self.dead_key, _dumps(payload))
        await self._persist_dead(
            stage, reason, payload, signal_id=signal.signal_id
        )

    async def push_dead_raw(
        self, body: str, reason: str, *, stage: str = "parse"
    ) -> None:
        """解析失败的原始 body → 死信队列，并落 SQLite。"""
        payload = {"raw": body, "reason": reason}
        await self._redis.lpush(self.dead_key, _dumps(payload))
        await self._persist_dead(stage, reason, payload)

    async def _persist_dead(
        self,
        stage: str,
        reason: str,
        payload: dict[str, Any],
        *,
        signal_id: str | None = None,
    ) -> None:
        """死信写 SQLite。落库失败只告警——Redis 队列已写入，
        monitor 的计数告警仍然有效，不能因为权威表写失败把死信丢掉。"""
        if self._store is None:
            return
        try:
            await asyncio.to_thread(
                self._store.insert_dead_letter,
                uuid.uuid4().hex,
                stage,
                reason,
                _dumps(payload),
                created_at=int(time.time() * 1000),
                signal_id=signal_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"persist dead letter failed stage={stage}: {e}")

    async def recover_inflight(self) -> list[Signal]:
        """启动恢复：把 inflight 全部回 pending，并返回恢复的信号列表。

        用 RPOPLPUSH 逐个搬回（尾出→头入），保持原始 FIFO 消费顺序。
        """
        recovered: list[Signal] = []
        while True:
            raw = await self._redis.rpoplpush(
                self.inflight_key, self.pending_key
            )
            if raw is None:
                break
            try:
                recovered.append(_loads_signal(raw))
            except (ValueError, KeyError, json.JSONDecodeError):
                # 损坏数据直接进死信，避免阻塞恢复；stage 记为 reconcile，
                # 因为这不是 webhook 解析失败，而是状态恢复时发现异常
                await self.push_dead_raw(
                    raw, "corrupt inflight payload", stage="reconcile"
                )
        return recovered

    async def pending_length(self) -> int:
        return int(await self._redis.llen(self.pending_key))

    async def inflight_length(self) -> int:
        return int(await self._redis.llen(self.inflight_key))

    async def dead_length(self) -> int:
        return int(await self._redis.llen(self.dead_key))


class Deduper:
    """基于信号规范 JSON 的 sha256 去重（TTL 默认 5 分钟）。"""

    def __init__(
        self,
        redis: Redis,
        prefix: str = "quant:dedup:",
        ttl_seconds: int = 300,
    ) -> None:
        self._redis = redis
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds

    def key_for(self, signal: Signal) -> str:
        """去重键：prefix + sha256(规范 JSON)。"""
        import hashlib

        canonical = _dumps(_canonical(signal.to_json()))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{self.prefix}{digest}"

    async def is_duplicate(self, signal: Signal) -> bool:
        """True=重复；False=首次出现（已写入标记）。"""
        key = self.key_for(signal)
        created = await self._redis.set(
            key, "1", ex=self.ttl_seconds, nx=True
        )
        return not bool(created)


def _canonical(payload: dict[str, Any]) -> dict[str, Any]:
    """去掉服务端生成的 signal_id / ts，其余字段排序后用于 hash。

    TV 重发同一条 alert 时 ts 会不同、signal_id 每次新建，
    因此两者都不能参与去重指纹。
    """
    ignored = {"signal_id", "ts"}
    return {k: payload[k] for k in sorted(payload) if k not in ignored}


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _loads_signal(raw: str | bytes) -> Signal:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return Signal.from_json(json.loads(raw))


__all__ = ["SignalQueue", "Deduper"]
