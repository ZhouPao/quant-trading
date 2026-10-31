"""极端行情熔断（M7-6）。

与 kill switch 的职责边界（M7-13）：

- **熔断按品种**：只停 `inst_id` 对应的信号，其它品种照常交易；
- **kill switch 全局**：停掉所有策略，只能人工解除。

因此两者用完全独立的状态键，互不影响：`quant:risk:circuit:{inst_id}`
与 `quant:kill-switch`。

判定口径：单根已收盘 K 线的振幅 `(high - low) / open` 严格大于阈值即
熔断该品种，状态带 TTL（`cooldown_seconds`）自动到期。冷却期结束后
不补评估熔断期间的 bar——那段时间的行情已经不具参考价值，引擎会把
水位推进到最新一根，从下一根重新开始。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal

from redis.asyncio import Redis

from quant.foundation.logger import get_logger
from quant.models import Bar

log = get_logger("risk.circuit")

DEFAULT_KEY_PREFIX = "quant:risk:circuit:"

STATUS_TRIPPED = "tripped"
STATUS_RESUMED = "resumed"


@dataclass(frozen=True, slots=True)
class CircuitState:
    """单品种的熔断状态。"""

    inst_id: str
    triggered_ts: int
    resume_at: int
    reason: str
    status: str = STATUS_TRIPPED

    def as_dict(self) -> dict[str, object]:
        return {
            "inst_id": self.inst_id,
            "triggered_ts": self.triggered_ts,
            "resume_at": self.resume_at,
            "reason": self.reason,
            "status": self.status,
        }


class CircuitBreaker:
    """基于 Redis TTL 的按品种熔断器。"""

    def __init__(
        self, redis: Redis, key_prefix: str = DEFAULT_KEY_PREFIX
    ) -> None:
        self._redis = redis
        self._prefix = key_prefix

    def key_for(self, inst_id: str) -> str:
        return f"{self._prefix}{inst_id}"

    @staticmethod
    def amplitude(bar: Bar) -> Decimal:
        """单根振幅 `(high-low)/open`（`Bar` 构造时已保证 open > 0）。"""
        return (bar.high - bar.low) / bar.open

    async def evaluate(
        self,
        bar: Bar,
        *,
        threshold: Decimal,
        cooldown_seconds: int,
        now: int | None = None,
    ) -> CircuitState | None:
        """振幅超阈值则熔断并返回状态；未超返回 None。

        阈值用「严格大于」：恰好等于阈值属于正常波动边界，不该被停。
        """
        amp = self.amplitude(bar)
        if amp <= threshold:
            return None
        reason = (
            f"单根振幅 {amp:.2%} 超过阈值 {threshold:.2%} "
            f"(bar_ts={bar.ts}, high={bar.high}, low={bar.low}, open={bar.open})"
        )
        return await self.trip(
            bar.inst_id,
            reason,
            cooldown_seconds=cooldown_seconds,
            now=now,
        )

    async def trip(
        self,
        inst_id: str,
        reason: str,
        *,
        cooldown_seconds: int,
        now: int | None = None,
    ) -> CircuitState:
        """置熔断标志，TTL 到期自动失效。"""
        ts = int(time.time() * 1000) if now is None else now
        cooldown = max(int(cooldown_seconds), 0)
        state = CircuitState(
            inst_id=inst_id,
            triggered_ts=ts,
            resume_at=ts + cooldown * 1000,
            reason=reason,
        )
        await self._redis.set(
            self.key_for(inst_id),
            json.dumps(state.as_dict(), ensure_ascii=False),
            ex=cooldown or None,
        )
        log.warning(
            f"circuit breaker tripped inst={inst_id} "
            f"cooldown={cooldown}s reason={reason}"
        )
        return state

    async def state(self, inst_id: str) -> CircuitState | None:
        """当前熔断状态；键不存在（已到期/未触发）返回 None。"""
        raw = await self._redis.get(self.key_for(inst_id))
        return _parse(raw, inst_id)

    async def is_tripped(self, inst_id: str) -> bool:
        return await self.state(inst_id) is not None

    async def states(self) -> list[CircuitState]:
        """全部处于熔断状态的品种（供看板展示）。"""
        result: list[CircuitState] = []
        async for key in self._redis.scan_iter(
            match=f"{self._prefix}*", count=100
        ):
            raw = await self._redis.get(key)
            name = key.decode() if isinstance(key, bytes) else str(key)
            inst_id = name[len(self._prefix) :]
            parsed = _parse(raw, inst_id)
            if parsed is not None:
                result.append(parsed)
        result.sort(key=lambda item: item.triggered_ts, reverse=True)
        return result

    async def resume(self, inst_id: str) -> bool:
        """人工解除熔断；返回是否真的删掉了状态键。"""
        deleted = await self._redis.delete(self.key_for(inst_id))
        if deleted:
            log.warning(f"circuit breaker resumed manually inst={inst_id}")
        return bool(deleted)


def _parse(raw: object, inst_id: str) -> CircuitState | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # 键存在但内容坏了：仍视为熔断中（宁可少交易，不可多交易）
        return CircuitState(
            inst_id=inst_id,
            triggered_ts=0,
            resume_at=0,
            reason="circuit state unreadable",
        )
    if not isinstance(data, dict):
        return CircuitState(
            inst_id=inst_id,
            triggered_ts=0,
            resume_at=0,
            reason="circuit state unreadable",
        )
    return CircuitState(
        inst_id=str(data.get("inst_id") or inst_id),
        triggered_ts=int(data.get("triggered_ts") or 0),
        resume_at=int(data.get("resume_at") or 0),
        reason=str(data.get("reason") or ""),
        status=str(data.get("status") or STATUS_TRIPPED),
    )


__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "DEFAULT_KEY_PREFIX",
    "STATUS_TRIPPED",
]
