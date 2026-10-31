"""极端行情熔断（M7-6）。

与 kill switch 的职责边界（M7-13）：

- **熔断按品种**：只停 `inst_id` 对应的信号，其它品种照常交易；
- **kill switch 全局**：停掉所有策略，只能人工解除。

因此两者用完全独立的状态键，互不影响：`quant:risk:circuit:{inst_id}`
与 `quant:kill-switch`。

判定口径：单根已收盘 K 线的振幅 `(high - low) / open` 严格大于阈值即
熔断该品种。冷却期结束后**不自动放行**（M7-12）：状态转为
`awaiting_resume`，必须由人工在看板确认恢复。把决定权交回给人，是因为
极端行情往往伴随跳空，冷却一到就自动恢复很容易踩进第二波。

熔断期间的 bar 不补评估——那段时间的行情已经不具参考价值，引擎会把
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
#: 冷却期已结束，等待人工确认恢复
STATUS_AWAITING_RESUME = "awaiting_resume"


@dataclass(frozen=True, slots=True)
class CircuitState:
    """单品种的熔断状态。"""

    inst_id: str
    triggered_ts: int
    resume_at: int
    reason: str
    status: str = STATUS_TRIPPED
    #: 冷却结束是否必须人工确认（False 时由 TTL 自动到期）
    requires_confirm: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "inst_id": self.inst_id,
            "triggered_ts": self.triggered_ts,
            "resume_at": self.resume_at,
            "reason": self.reason,
            "status": self.status,
            "requires_confirm": self.requires_confirm,
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
        requires_confirm: bool = True,
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
            requires_confirm=requires_confirm,
        )

    async def trip(
        self,
        inst_id: str,
        reason: str,
        *,
        cooldown_seconds: int,
        now: int | None = None,
        requires_confirm: bool = True,
    ) -> CircuitState:
        """置熔断标志。

        `requires_confirm=True` 时**不设 TTL**：冷却到期只是把状态变成
        `awaiting_resume`，键必须留着等人工确认；设为 False 才退回旧的
        「TTL 到期自动恢复」行为（灰度回滚用）。
        """
        ts = int(time.time() * 1000) if now is None else now
        cooldown = max(int(cooldown_seconds), 0)
        state = CircuitState(
            inst_id=inst_id,
            triggered_ts=ts,
            resume_at=ts + cooldown * 1000,
            reason=reason,
            requires_confirm=requires_confirm,
        )
        await self._redis.set(
            self.key_for(inst_id),
            json.dumps(state.as_dict(), ensure_ascii=False),
            ex=None if requires_confirm else max(cooldown, 1),
        )
        log.warning(
            f"circuit breaker tripped inst={inst_id} cooldown={cooldown}s "
            f"confirm={'required' if requires_confirm else 'auto'} "
            f"reason={reason}"
        )
        return state

    async def state(
        self, inst_id: str, *, now: int | None = None
    ) -> CircuitState | None:
        """当前熔断状态；键不存在（未触发或已自动恢复）返回 None。

        冷却到期且需要人工确认时，返回的 `status` 是 `awaiting_resume`——
        状态仍是「熔断中」，引擎与 worker 继续拦截该品种。
        """
        raw = await self._redis.get(self.key_for(inst_id))
        return _parse(raw, inst_id, now=_now_ms(now))

    async def is_tripped(self, inst_id: str, *, now: int | None = None) -> bool:
        """是否处于熔断（含 `awaiting_resume`）。"""
        return await self.state(inst_id, now=now) is not None

    async def states(self, *, now: int | None = None) -> list[CircuitState]:
        """全部处于熔断状态的品种（供看板展示）。"""
        ts = _now_ms(now)
        result: list[CircuitState] = []
        async for key in self._redis.scan_iter(
            match=f"{self._prefix}*", count=100
        ):
            raw = await self._redis.get(key)
            name = key.decode() if isinstance(key, bytes) else str(key)
            inst_id = name[len(self._prefix) :]
            parsed = _parse(raw, inst_id, now=ts)
            if parsed is not None:
                result.append(parsed)
        result.sort(key=lambda item: item.triggered_ts, reverse=True)
        return result

    async def resume(self, inst_id: str) -> CircuitState | None:
        """人工解除熔断，返回解除前的状态（供审计）；未熔断返回 None。"""
        state = await self.state(inst_id)
        if state is None:
            return None
        await self._redis.delete(self.key_for(inst_id))
        log.warning(f"circuit breaker resumed manually inst={inst_id}")
        return state


def _now_ms(now: int | None) -> int:
    return int(time.time() * 1000) if now is None else now


def _parse(raw: object, inst_id: str, *, now: int) -> CircuitState | None:
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
    resume_at = int(data.get("resume_at") or 0)
    requires_confirm = bool(data.get("requires_confirm", True))
    # 冷却到期但需要人工确认时状态跃迁为 awaiting_resume；键仍在，
    # 引擎与 worker 继续拦截该品种，直到调用 resume()
    status = str(data.get("status") or STATUS_TRIPPED)
    if requires_confirm and resume_at and now >= resume_at:
        status = STATUS_AWAITING_RESUME
    return CircuitState(
        inst_id=str(data.get("inst_id") or inst_id),
        triggered_ts=int(data.get("triggered_ts") or 0),
        resume_at=resume_at,
        reason=str(data.get("reason") or ""),
        status=status,
        requires_confirm=requires_confirm,
    )


__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "DEFAULT_KEY_PREFIX",
    "STATUS_AWAITING_RESUME",
    "STATUS_TRIPPED",
]
