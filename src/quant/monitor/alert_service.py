"""告警服务（MS4-A §5）。

三条刻意的边界：

1. **冷却不在本模块**：`publish` 收到事件就落库并通知；「要不要产生这个
   事件」属于 monitor 的上游决策。两处各判一次会让冷却窗口翻倍。
2. **INFO 不受静音**：「已恢复」是状态跃迁，静音期间更需要知道故障结束。
3. **落库/推送失败都不抛**：调用方是 monitor 主循环，告警系统的问题不能
   让监控停摆。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass

from redis.asyncio import Redis

from quant.foundation.config import MonitorSettings
from quant.foundation.logger import get_logger
from quant.monitor.alerter import EmailAlerter
from quant.state.sqlite_store import SQLiteStore

log = get_logger("monitor.alerts")


def fingerprint(rule: str, message: str) -> str:
    """规则 + 消息的内容指纹，用于同类事件归并。"""
    return hashlib.sha1(f"{rule}|{message}".encode()).hexdigest()[:16]


def build_alert_id(rule: str, fp: str, ts: int) -> str:
    """告警 ID：同一规则同一消息同一毫秒视为同一条（重复发布幂等）。"""
    return hashlib.sha1(f"{rule}|{fp}|{ts}".encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """待发布的一条告警。"""

    rule: str
    level: str  # INFO / WARNING / ERROR
    message: str
    ts: int
    inst_id: str | None = None
    strategy_id: str | None = None


class AlertService:
    """告警落库 + 实时缓存 + 邮件通知。"""

    def __init__(
        self,
        store: SQLiteStore,
        redis: Redis,
        alerter: EmailAlerter,
        settings: MonitorSettings,
    ) -> None:
        self._store = store
        self._redis = redis
        self._alerter = alerter
        self._settings = settings

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def publish(self, event: AlertEvent) -> str:
        """落库 → 推 Redis → 按静音决定是否发邮件，返回 alert_id。"""
        fp = fingerprint(event.rule, event.message)
        alert_id = build_alert_id(event.rule, fp, event.ts)

        silence = self._match_silence(event.rule, event.inst_id, event.ts)
        # INFO（恢复通知）不受静音约束
        suppressed = silence is not None and event.level != "INFO"

        if self._settings.alerts_persist:
            try:
                await asyncio.to_thread(
                    self._store.insert_alert,
                    alert_id,
                    event.rule,
                    event.level,
                    event.message,
                    fp,
                    created_at=event.ts,
                    inst_id=event.inst_id,
                    strategy_id=event.strategy_id,
                    suppressed=suppressed,
                )
            except Exception as e:  # noqa: BLE001
                log.error(f"alert persist failed id={alert_id}: {e}")

        try:
            await self._redis.lpush(
                self._settings.dashboard_alerts_key,
                json.dumps(
                    {
                        "alert_id": alert_id,
                        "rule": event.rule,
                        "level": event.level,
                        "message": event.message,
                        "ts": event.ts,
                        "inst_id": event.inst_id,
                    },
                    ensure_ascii=False,
                ),
            )
            await self._redis.ltrim(
                self._settings.dashboard_alerts_key,
                0,
                self._settings.dashboard_alert_keep - 1,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"alert cache push failed: {e}")

        if (
            not suppressed
            and event.level != "INFO"
            and self._alerter.enabled()
        ):
            await asyncio.to_thread(
                self._alerter.send,
                f"[{event.level}] {event.rule}",
                f"{event.message}\n\nts={event.ts} inst={event.inst_id}",
            )
        return alert_id

    async def ack(self, alert_id: str, by: str) -> bool:
        """标记已读；幂等，已确认的再调不覆盖首次时间。"""
        return await asyncio.to_thread(
            self._store.ack_alert, alert_id, by, int(time.time() * 1000)
        )

    async def resolve(self, rule: str, *, inst_id: str | None = None) -> int:
        """把该规则（+品种）下所有未关闭记录置为 resolved。"""
        return await asyncio.to_thread(
            self._store.resolve_alerts,
            rule,
            inst_id=inst_id,
            now_ms=int(time.time() * 1000),
        )

    async def silence(
        self,
        rule: str,
        *,
        inst_id: str | None,
        hours: float,
        reason: str,
        by: str,
    ) -> str:
        """创建静音记录，返回 silence_id。"""
        now = int(time.time() * 1000)
        silence_id = hashlib.sha1(
            f"{rule}|{inst_id or '-'}|{now}".encode()
        ).hexdigest()[:16]
        await asyncio.to_thread(
            self._store.insert_silence,
            silence_id,
            rule,
            inst_id=inst_id,
            until_ts=now + int(hours * 3600 * 1000),
            reason=reason,
            created_by=by,
            created_at=now,
        )
        return silence_id

    async def revoke_silence(self, silence_id: str) -> bool:
        return await asyncio.to_thread(
            self._store.revoke_silence, silence_id, int(time.time() * 1000)
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def list(
        self,
        limit: int = 50,
        *,
        level: str | None = None,
        rule: str | None = None,
        state: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """分页查询，返回 (结果, 命中总数)。"""
        return self._store.list_alerts(
            limit,
            offset,
            level=level,
            rule=rule,
            state=state,
            since_ms=since_ms,
            until_ms=until_ms,
        )

    def active_silences(self, now_ms: int) -> list[dict]:
        return self._store.list_active_silences(now_ms)

    def _match_silence(
        self, rule: str, inst_id: str | None, now_ms: int
    ) -> dict | None:
        """找到命中的静音：同规则，且静音不限品种或品种一致。"""
        for silence in self.active_silences(now_ms):
            if silence["rule"] != rule:
                continue
            hit_inst = silence.get("inst_id")
            if hit_inst and hit_inst != inst_id:
                continue
            return silence
        return None


__all__ = ["AlertEvent", "AlertService", "build_alert_id", "fingerprint"]
