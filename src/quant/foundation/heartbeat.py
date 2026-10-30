"""统一心跳工具（MS4-A §7）。

约定：每个进程写 `quant:heartbeat:{name}`，值带 `ts` 与 `pid`，TTL 取
`max(2 × 周期, 60s)`，最短 60 秒容忍。两个显式例外写在 `PROCESS_SPECS`：

- supervisor 取 15s：它 5 秒一轮，按 60s 判活等于把「最快发现托管进程
  超时」的能力作废；
- monitor 保持 300s：每轮要发多次 OKX 请求，耗时波动大，改小会误报离线。

`pid` 不是装饰：看板要能回答「这个进程到底还活着吗，还是 Redis 里留着
上一轮写进去的旧值」。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass

from redis.asyncio import Redis

from quant.foundation.logger import get_logger

log = get_logger("foundation.heartbeat")

HEARTBEAT_PREFIX = "quant:heartbeat"
#: supervisor 用独立键名：它写的进程表本身就是判活信号。
SUPERVISOR_KEY = "quant:process:supervisor"
SUPERVISOR_TTL = 15


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """一个进程的判活参数（周期与 TTL 必须与实际写入方一致）。"""

    name: str
    interval_seconds: int
    ttl_seconds: int


def default_ttl(interval: int) -> int:
    """TTL 派生规则：max(2 × 周期, 60s)。"""
    return max(interval * 2, 60)


#: 7 个常驻进程。candles 的周期来自配置，其余为固定值；
#: 看板用这里的 TTL 判活，写入方用同一派生规则，避免「键还在却显示离线」。
PROCESS_SPECS: tuple[ProcessSpec, ...] = (
    ProcessSpec("supervisor", 5, SUPERVISOR_TTL),
    ProcessSpec("candles", 300, default_ttl(300)),
    ProcessSpec("webhook", 30, default_ttl(30)),
    ProcessSpec("worker", 60, default_ttl(60)),
    ProcessSpec("engine", 60, default_ttl(60)),
    ProcessSpec("monitor", 60, 300),
    ProcessSpec("dashboard", 30, default_ttl(30)),
)


class Heartbeat:
    """定时写带 TTL 的心跳键，供看板与 monitor 判活。"""

    def __init__(
        self,
        redis: Redis,
        name: str,
        interval: int = 30,
        ttl: int | None = None,
    ) -> None:
        self._redis = redis
        self._name = name
        self._interval = interval
        self._ttl = ttl if ttl is not None else default_ttl(interval)
        self._task: asyncio.Task | None = None

    @property
    def key(self) -> str:
        return f"{HEARTBEAT_PREFIX}:{self._name}"

    async def beat(self, extra: dict | None = None) -> None:
        """写一次心跳；失败只记日志，不影响业务循环。"""
        payload: dict[str, object] = {
            "ts": int(time.time() * 1000),
            "pid": os.getpid(),
        }
        if extra:
            payload.update(extra)
        try:
            await self._redis.set(
                self.key, json.dumps(payload, ensure_ascii=False), ex=self._ttl
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"heartbeat write failed name={self._name}: {e}")

    def start(self) -> None:
        """启动后台周期任务（webhook / dashboard 这类长驻服务用）。"""
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await self.beat()
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def parse_heartbeat(raw: object) -> tuple[int | None, int | None]:
    """解析心跳值为 (ts, pid)。

    兼容两种写法：新工具写 JSON，worker/monitor 等存量代码写裸时间戳。
    解析不出来时返回 (None, None)——调用方据此判离线，而不是把非法值
    当成「很久以前」。
    """
    if raw is None:
        return None, None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    text = text.strip()
    if not text:
        return None, None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None, None
        if not isinstance(payload, dict):
            return None, None
        ts = payload.get("ts")
        pid = payload.get("pid")
        try:
            ts_int = int(ts) if ts is not None else None
        except (TypeError, ValueError):
            ts_int = None
        try:
            pid_int = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid_int = None
        return ts_int, pid_int
    try:
        return int(text), None
    except (TypeError, ValueError):
        return None, None


__all__ = [
    "HEARTBEAT_PREFIX",
    "PROCESS_SPECS",
    "SUPERVISOR_KEY",
    "SUPERVISOR_TTL",
    "Heartbeat",
    "ProcessSpec",
    "default_ttl",
    "parse_heartbeat",
]
