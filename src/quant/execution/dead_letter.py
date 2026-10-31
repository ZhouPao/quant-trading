"""死信查看与重放（MS4-B §10）。

设计要点：
- 死信权威记录在 SQLite（`dead_letters`），Redis LIST 只保留给 monitor 计数。
- `stage` 只有 `parse` / `submit` / `reconcile` 三类；**风控拒绝不进死信**，
  那是 `risk_events` 里的正常业务结果。
- 重放前查 `signals.status`：已 `executed` / `rejected` 的信号不再入队
  （`already_processed`），避免重复下单。这两个字面量必须与 worker
  实际写入的值一致，写错会让幂等判定永远为假。
"""
from __future__ import annotations

import asyncio
import json
import time

from quant.backtest.signal import Signal
from quant.foundation.logger import get_logger
from quant.queue.signal_queue import SignalQueue
from quant.state.sqlite_store import SQLiteStore

log = get_logger("execution.dead_letter")

#: worker 只写这两个终态（`save_signal(..., "rejected"/"executed")`）
_SETTLED_SIGNAL_STATUS = frozenset({"executed", "rejected"})

#: 重放结果，与 `dead_letters.replay_result` 取值一致
REQUEUED = "requeued"
ALREADY_PROCESSED = "already_processed"
ALREADY_REPLAYED = "already_replayed"
DISCARDED = "discarded"
FAILED = "failed"
NOT_FOUND = "not_found"
NOT_REPLAYABLE = "not_replayable"


class DeadLetterService:
    """单条/批量重放、丢弃。

    `queue` 必须指向 worker 消费的同一个 pending key，否则重放不会被执行。
    """

    def __init__(self, store: SQLiteStore, queue: SignalQueue) -> None:
        self._store = store
        self._queue = queue

    async def replay(self, dead_id: str) -> dict[str, str]:
        """重放一条死信。返回 `{dead_id, result}`。"""
        dl = await asyncio.to_thread(self._store.get_dead_letter, dead_id)
        if dl is None:
            return {"dead_id": dead_id, "result": NOT_FOUND}
        if dl["replayed_at"]:
            return {"dead_id": dead_id, "result": _settled_result(dl)}

        payload = _load_payload(dl["payload"])
        signal_payload = payload.get("signal")
        if not isinstance(signal_payload, dict):
            # 解析死信只有原始 body，无法还原成 Signal；只能人工看过再丢弃
            return {"dead_id": dead_id, "result": NOT_REPLAYABLE}

        signal_id = signal_payload.get("signal_id")
        if signal_id:
            status = await asyncio.to_thread(
                self._store.get_signal_status, str(signal_id)
            )
            if status in _SETTLED_SIGNAL_STATUS:
                await self._finish(dead_id, ALREADY_PROCESSED, mark=True)
                return {"dead_id": dead_id, "result": ALREADY_PROCESSED}

        # 先原子占用再入队：并发双击只有一个能把信号放回队列
        claimed = await asyncio.to_thread(
            self._store.claim_dead_letter, dead_id, _now_ms()
        )
        if not claimed:
            return {"dead_id": dead_id, "result": ALREADY_REPLAYED}

        try:
            await self._queue.push(Signal.from_json(signal_payload))
        except Exception as e:  # noqa: BLE001
            log.warning(f"replay enqueue failed dead_id={dead_id}: {e}")
            await asyncio.to_thread(self._store.release_dead_letter, dead_id)
            return {"dead_id": dead_id, "result": FAILED}

        await self._finish(dead_id, REQUEUED)
        log.info(f"dead letter requeued dead_id={dead_id}")
        return {"dead_id": dead_id, "result": REQUEUED}

    async def replay_many(self, ids: list[str]) -> list[dict[str, str]]:
        """批量重放：逐条独立，单条失败不影响其余。"""
        return [await self.replay(dead_id) for dead_id in ids]

    async def discard(self, dead_id: str) -> dict[str, str]:
        """标记丢弃：不再重放，但保留记录供审计。"""
        dl = await asyncio.to_thread(self._store.get_dead_letter, dead_id)
        if dl is None:
            return {"dead_id": dead_id, "result": NOT_FOUND}
        if dl["replayed_at"]:
            result = dl["replay_result"] or ALREADY_REPLAYED
            return {"dead_id": dead_id, "result": str(result)}

        claimed = await asyncio.to_thread(
            self._store.claim_dead_letter, dead_id, _now_ms()
        )
        if not claimed:
            return {"dead_id": dead_id, "result": ALREADY_REPLAYED}
        await self._finish(dead_id, DISCARDED)
        return {"dead_id": dead_id, "result": DISCARDED}

    async def _finish(
        self, dead_id: str, result: str, *, mark: bool = False
    ) -> None:
        """`mark=True` 用于未占用就得出结论的分支（already_processed）。"""
        if mark:
            await asyncio.to_thread(
                self._store.claim_dead_letter, dead_id, _now_ms()
            )
        await asyncio.to_thread(self._store.finish_dead_letter, dead_id, result)


def _settled_result(row: dict) -> str:
    """已处理过的死信回显上次结果；占用中（replaying）回「已重放过」。"""
    result = str(row.get("replay_result") or "")
    if not result or result == "replaying":
        return ALREADY_REPLAYED
    return result


def _load_payload(raw: str | bytes | None) -> dict:
    """解析 `dead_letters.payload`：非 JSON / 非 dict / 空值一律返回 `{}`。"""
    if raw is None:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "DeadLetterService",
    "ALREADY_PROCESSED",
    "ALREADY_REPLAYED",
    "DISCARDED",
    "FAILED",
    "NOT_FOUND",
    "NOT_REPLAYABLE",
    "REQUEUED",
]
