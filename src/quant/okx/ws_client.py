"""OKX WebSocket 客户端（spec §4.4，M3 起使用）。

功能：
- 公开/私有频道连接
- 按 (channel, instId) 注册回调
- 断线指数退避重连，重连后自动恢复订阅
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
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from quant.foundation.logger import get_logger
from quant.models import Tick

Callback = Callable[[dict[str, Any]], Awaitable[None]]
ConnectFn = Callable[..., Any]


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

    PUBLIC_URL = "wss://ws.okx.com:8443/ws/v5/public"
    PRIVATE_URL = "wss://ws.okx.com:8443/ws/v5/private"

    def __init__(
        self,
        url: str = PUBLIC_URL,
        reconnect_delay: float = 5.0,
        max_reconnect_delay: float = 60.0,
        connect_fn: ConnectFn | None = None,
        proxy: str | None = None,
    ) -> None:
        self._url = url
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._connect_fn = connect_fn or _default_connect
        # None = 不走代理；如系统需代理，传 "http://host:port"
        self._proxy = proxy
        self._log = get_logger("okx.ws")

        self._ws: Any = None
        self._running = False
        self._subscriptions: list[dict[str, Any]] = []
        # (channel, inst_id) → callback；inst_id 为空表示该频道全部
        self._callbacks: dict[tuple[str, str], Callback] = {}
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

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
        delay = self._reconnect_delay

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
                    delay = self._reconnect_delay
                    self._connected.set()
                    self._log.info(f"ws connected url={self._url}")
                    await self._restore_subscriptions(ws)
                    async for raw in ws:
                        await self._handle_message(raw)

                # 连接被对端正常关闭：退避后重连，避免空转
                self._connected.clear()
                self._ws = None
                if await self._sleep_or_stop(delay):
                    break
                delay = min(delay * 2, self._max_reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._connected.clear()
                self._ws = None
                self._log.warning(
                    f"ws disconnected: {e}, retry in {delay:.0f}s"
                )
                if await self._sleep_or_stop(delay):
                    break
                delay = min(delay * 2, self._max_reconnect_delay)

        self._connected.clear()
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

    async def _handle_message(self, raw: str | bytes) -> None:
        """分发一条 WS 消息。"""
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


__all__ = ["WSClient", "parse_ticker"]
