"""OKX WebSocket 客户端（spec §4.4，M3 起使用）。

功能：
- 公开/私有频道连接
- 按 (channel, instId) 注册回调
- 断线指数退避重连（带 ±20% 抖动，5s → 60s），重连后自动恢复订阅
- 连接状态、连续失败次数与本轮退避时长可查（供 M2-2 上报 Redis）
- tickers 频道 → Tick 模型

使用：
    ws = WSClient()
    book = LivePriceBook()
    await ws.subscribe("tickers", "BTC-USDT", book.update_from_payload)
    await ws.run()          # 阻塞
    await ws.close()        # 优雅退出
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from quant.foundation.logger import get_logger
from quant.models import Tick
from quant.models.trade import Trade

Callback = Callable[[dict[str, Any]], Awaitable[None]]
ConnectFn = Callable[..., Any]

#: WS 连接状态键前缀；worker 每次心跳把它写到 `quant:ws:status:worker`
WS_STATUS_PREFIX = "quant:ws:status"
#: 退避抖动比例：实际等待落在 base 的 80% ~ 120%
JITTER_RATIO = 0.2


def ws_status_key(name: str = "worker") -> str:
    return f"{WS_STATUS_PREFIX}:{name}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def parse_ticker(item: dict[str, Any]) -> Tick | None:
    """OKX tickers 频道单条数据 → Tick。

    字段缺失或价格为 0（刚上线/极端行情）时返回 None，不抛错。
    """
    try:
        bid_px = Decimal(str(item["bidPx"]))
        ask_px = Decimal(str(item["askPx"]))
        last_px = Decimal(str(item.get("last") or "0"))
        ts = int(item["ts"])
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None

    if bid_px <= 0 or ask_px <= 0 or bid_px > ask_px:
        return None

    return Tick(
        ts=ts,
        inst_id=str(item["instId"]),
        bid_px=bid_px,
        bid_sz=Decimal(str(item.get("bidSz") or "0")),
        ask_px=ask_px,
        ask_sz=Decimal(str(item.get("askSz") or "0")),
        last_px=last_px,
        last_sz=Decimal(str(item.get("lastSz") or "0")),
    )


class WSClient:
    """OKX WebSocket 客户端（带自动重连）。"""

    # OKX 已通知 8443 端口即将弃用，统一走 443（不带端口）
    PUBLIC_URL = "wss://ws.okx.com/ws/v5/public"
    PRIVATE_URL = "wss://ws.okx.com/ws/v5/private"
    # 模拟盘是独立虚拟市场，公共频道用 wspap；行情与实盘不可互作参考
    DEMO_PUBLIC_URL = "wss://wspap.okx.com/ws/v5/public"
    DEMO_PRIVATE_URL = "wss://wspap.okx.com/ws/v5/private"

    def __init__(
        self,
        url: str | None = None,
        reconnect_delay: float = 5.0,
        max_reconnect_delay: float = 60.0,
        connect_fn: ConnectFn | None = None,
        proxy: str | None = None,
        keepalive_interval: float = 25.0,
        demo: bool = False,
    ) -> None:
        # demo 必须与交易/行情口径一致：模拟盘用独立 WS 频道，避免 worker
        # 拿实盘 ticker 去算模拟盘下单数量。显式 url 优先（测试/特殊部署）。
        self._url = url or (self.DEMO_PUBLIC_URL if demo else self.PUBLIC_URL)
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._connect_fn = connect_fn or _default_connect
        # None = 不走代理；如系统需代理，传 "http://host:port"
        self._proxy = proxy
        # 应用层心跳间隔：OKX 要求空闲约 30s 内发文本 "ping"，超时被断连
        self._keepalive_interval = keepalive_interval
        self._log = get_logger("okx.ws")

        self._ws: Any = None
        self._running = False
        self._subscriptions: list[dict[str, Any]] = []
        # (channel, inst_id) → callback；inst_id 为空表示该频道全部
        self._callbacks: dict[tuple[str, str], Callback] = {}
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()

        # M2-2 状态快照：连续失败次数、最后一次错误、连接起点
        self._failures = 0
        self._last_error: str | None = None
        self._last_error_ts: int | None = None
        self._connected_since: int | None = None
        self._next_delay: float | None = None
        # 下次重连的墙钟时刻（UTC ms）：看板据此显示真倒计时，而不是把
        # 退避总时长当静态快照反复显示（P1 口径）
        self._next_retry_at: int | None = None
        # 当前退避基数（每失败一次翻倍，封顶 max_reconnect_delay）
        self._current_delay = reconnect_delay

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def status(self) -> dict[str, Any]:
        """当前连接状态快照（供 worker 心跳写入 Redis）。"""
        return {
            "connected": self.is_connected,
            "failures": self._failures,
            "last_error": self._last_error,
            "last_error_ts": self._last_error_ts,
            "connected_since": self._connected_since,
            "next_delay": None if self._next_delay is None else round(self._next_delay, 1),
            "next_retry_at": self._next_retry_at,
        }

    def _jitter(self, delay: float) -> float:
        """给退避加 ±20% 抖动：多进程同时断线时避免同一秒一起重连。"""
        if delay <= 0:
            return 0.0
        spread = delay * JITTER_RATIO
        return min(random.uniform(delay - spread, delay + spread), self._max_reconnect_delay)

    def _record_failure(self, error: str) -> float:
        """记录一次断线并算出下一次等待时长（调用方负责 sleep）。"""
        self._failures += 1
        self._last_error = error[:200]
        self._last_error_ts = _now_ms()
        wait = self._jitter(self._current_delay)
        self._next_retry_at = _now_ms() + int(wait * 1000)
        return wait

    async def subscribe(
        self,
        channel: str,
        inst_id: str,
        callback: Callback,
    ) -> None:
        """注册订阅并登记回调。连接中则立即发送订阅请求。"""
        key = (channel, inst_id)
        if key not in self._callbacks:
            self._subscriptions.append(
                {"op": "subscribe", "args": [{"channel": channel, "instId": inst_id}]}
            )
        self._callbacks[key] = callback

        if self._ws is not None:
            await self._ws.send(
                json.dumps(
                    {"op": "subscribe", "args": [{"channel": channel, "instId": inst_id}]}
                )
            )

    async def run(self) -> None:
        """连接并持续接收消息，直到 close()。断线自动重连。"""
        self._running = True
        self._stop.clear()
        self._current_delay = self._reconnect_delay

        while self._running:
            try:
                async with self._connect_fn(
                    self._url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=1000,
                    proxy=self._proxy,
                ) as ws:
                    self._ws = ws
                    # 连上即视为恢复：失败计数清零，退避回到起点
                    self._current_delay = self._reconnect_delay
                    self._next_delay = None
                    self._next_retry_at = None
                    self._failures = 0
                    self._connected_since = _now_ms()
                    self._connected.set()
                    self._log.info(f"ws connected url={self._url}")
                    await self._restore_subscriptions(ws)
                    # 应用层心跳与消息循环并行，断开时取消
                    keepalive = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for raw in ws:
                            await self._handle_message(raw)
                    finally:
                        keepalive.cancel()

                # 连接被对端正常关闭：退避后重连，避免空转
                self._connected.clear()
                self._connected_since = None
                self._ws = None
                wait = self._record_failure("connection closed by peer")
                self._next_delay = wait
                if await self._sleep_or_stop(wait):
                    break
                self._current_delay = min(
                    self._current_delay * 2, self._max_reconnect_delay
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._connected.clear()
                self._connected_since = None
                self._ws = None
                wait = self._record_failure(f"{type(e).__name__}: {e}")
                self._next_delay = wait
                self._log.warning(
                    f"ws disconnected: {e}, retry in {wait:.1f}s "
                    f"(failures={self._failures})"
                )
                if await self._sleep_or_stop(wait):
                    break
                self._current_delay = min(
                    self._current_delay * 2, self._max_reconnect_delay
                )

        self._connected.clear()
        self._connected_since = None
        self._ws = None

    async def _sleep_or_stop(self, delay: float) -> bool:
        """等待 delay 秒；期间 close() 可立即打断。返回是否应停止。"""
        if delay > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
        return not self._running

    async def close(self) -> None:
        """停止重连循环并关闭连接。"""
        self._running = False
        self._stop.set()
        ws = self._ws
        if ws is not None:
            await ws.close()

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        """等待连接建立（测试与启动编排用）。"""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def _restore_subscriptions(self, ws: Any) -> None:
        for sub in self._subscriptions:
            await ws.send(json.dumps(sub))

    async def _keepalive(self, ws: Any) -> None:
        """应用层心跳：OKX 要求空闲约 30s 内发文本 "ping"，否则服务端断连。

        协议层 ping_interval=20 只保 websocket 帧；OKX 网关认的是文本
        ping/pong——冷门品种长时间无行情会被周期性踢线、反复重连（P2）。
        """
        try:
            while True:
                await asyncio.sleep(self._keepalive_interval)
                await ws.send("ping")
                self._log.debug("ws keepalive ping sent")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._log.warning(f"ws keepalive failed: {e}")

    async def _handle_message(self, raw: str | bytes) -> None:
        """分发一条 WS 消息。"""
        # 应用层心跳回应：OKX 对文本 "ping" 回文本 "pong"，无 JSON 载荷
        if isinstance(raw, str) and raw.strip().lower() == "pong":
            return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._log.warning(f"invalid ws message: {raw!r}")
            return

        event = msg.get("event")
        if event == "error":
            self._log.error(
                f"ws error code={msg.get('code')} msg={msg.get('msg')}"
            )
            return
        if event:
            self._log.debug(f"ws event={event}")
            return

        arg = msg.get("arg", {})
        channel = arg.get("channel", "")
        inst_id = arg.get("instId", "")
        callback = self._callbacks.get((channel, inst_id)) or self._callbacks.get(
            (channel, "")
        )
        if callback is None:
            self._log.debug(f"no callback for channel={channel} inst={inst_id}")
            return

        for item in msg.get("data", []):
            await callback(item)


def _default_connect(*args: Any, **kwargs: Any) -> Any:
    """延迟导入 websockets，避免未安装时影响其他模块。"""
    import websockets

    return websockets.connect(*args, **kwargs)


def parse_trade(item: dict[str, Any]) -> Trade | None:
    """OKX trades 频道单条数据 → Trade。

    字段缺失或非法时返回 None，不抛错（与 parse_ticker 一致）。
    """
    try:
        return Trade(
            ts=int(item["ts"]),
            inst_id=str(item["instId"]),
            trade_id=str(item["tradeId"]),
            px=Decimal(str(item["px"])),
            sz=Decimal(str(item["sz"])),
            side=str(item["side"]),
        )
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None


__all__ = ["WSClient", "parse_ticker", "parse_trade", "ws_status_key"]
