"""紧急停止开关（spec F4.5，kill switch）。

通过 Redis 共享标志实现多进程（webhook/worker/monitor）间的紧急停止：
- `enable(reason)`：置标志（含时间戳与原因）
- `disable()`：清除标志，恢复正常
- `is_active()` / `reason()`：查询当前状态

worker 在消费信号前检查该标志；激活时拒绝新信号（不入场），
已提交但未成交的挂单由 kill-switch CLI 在 `--on` 时统一撤销。
"""
from __future__ import annotations

import json
import time

from redis.asyncio import Redis

DEFAULT_KEY = "quant:kill-switch"


class KillSwitch:
    """基于 Redis 的紧急停止开关。"""

    def __init__(self, redis: Redis, key: str = DEFAULT_KEY) -> None:
        self._redis = redis
        self.key = key

    async def enable(self, reason: str = "") -> None:
        """激活紧急停止。"""
        payload = json.dumps(
            {"ts": int(time.time() * 1000), "reason": reason},
            sort_keys=True,
            separators=(",", ":"),
        )
        await self._redis.set(self.key, payload)

    async def disable(self) -> None:
        """解除紧急停止。"""
        await self._redis.delete(self.key)

    async def is_active(self) -> bool:
        """是否处于紧急停止状态。"""
        return await self._redis.exists(self.key) > 0

    async def status(self) -> tuple[bool, str | None]:
        """一次读出 (是否激活, 原因)。

        overview 原先分开调 is_active + reason（EXISTS + GET 两次往返），
        两次之间标志若被切换会出现 active=true 但 reason=None 的撕裂状态
        （TOCTOU）；单次 GET 天然原子。worker/engine 用的 is_active 保持不变。
        """
        raw = await self._redis.get(self.key)
        if raw is None:
            return False, None
        return True, self._parse_reason(raw)

    async def reason(self) -> str | None:
        """激活原因；未激活或无原因返回 None。

        空原因统一返回 None 而不是空串：前端按「有没有值」判断是否展示原因，
        空串既不是 null 也不是有效文案，容易让 UI 显示成空白。
        """
        raw = await self._redis.get(self.key)
        if raw is None:
            return None
        return self._parse_reason(raw)

    @staticmethod
    def _parse_reason(raw: str) -> str | None:
        try:
            value = str(json.loads(raw).get("reason", ""))
        except (json.JSONDecodeError, AttributeError):
            value = str(raw)
        return value or None


__all__ = ["KillSwitch", "DEFAULT_KEY"]
