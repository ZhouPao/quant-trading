"""逐笔成交采集器（M2-14 订单流足迹图）。

- backfill: 用 REST history-trades 从已存最新 trade 往旧翻页，补到 days 天前/无更多
- run: 订阅 WS trades 频道，内存缓冲批量落盘（高频流严禁每笔写）

实时流（BTC 每秒几条~几十条）必须缓冲后批量写，否则单笔 INSERT 会拖垮
进程并放大 parquet 重写开销；缓冲满或定时 flush 双触发。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from quant.foundation.logger import get_logger
from quant.models.trade import Trade
from quant.okx.client import OKXClient
from quant.okx.ws_client import WSClient, parse_trade
from quant.storage.trade_storage import TradeStorage


class TradeFetcher:
    """OKX 逐笔成交的回补 + 实时采集编排。"""

    def __init__(
        self,
        client: OKXClient,
        storage: TradeStorage,
        demo: bool = False,
        buffer_size: int = 500,
        flush_interval: float = 1.0,
    ) -> None:
        self._client = client
        self._storage = storage
        self._demo = demo
        self._buffer_size = buffer_size
        self._flush_interval = flush_interval
        self._buffer: list[Trade] = []
        self._lock = asyncio.Lock()
        self._log = get_logger("fetcher.trade")

    def backfill(self, inst_id: str, days: int = 1) -> int:
        """历史回补：从 latest_trade_id（或最新）往旧翻页 days 天。

        返回净新增条数。遵守 RateLimiter（history 桶）。
        """
        if days <= 0:
            return 0
        cutoff_ms = int(time.time() * 1000) - days * 24 * 3600 * 1000
        after = self._storage.latest_trade_id(inst_id)
        total = 0
        page = 0
        self._log.info(
            f"backfill start inst={inst_id} days={days} from={after or 'latest'}"
        )
        while True:
            page += 1
            rows = self._client.get_history_trades(inst_id, after=after, limit=100)
            if not rows:
                break
            saved_page = self._storage.save_trades(rows)
            total += saved_page
            if page % 10 == 0:
                self._log.info(
                    f"backfill progress inst={inst_id} page={page} total={total}"
                )
            oldest_ts = min(int(t.ts) for t in rows)
            # 已翻到 days 边界之前，本页可能含边界内数据已落盘，停止继续往旧
            if oldest_ts < cutoff_ms:
                self._log.info(f"backfill hit days cutoff inst={inst_id} page={page}")
                break
            prev_after = after
            after = rows[-1].trade_id
            # 游标未推进（异常时 OKX 返回相同页）—— 防止死循环
            if after == prev_after:
                self._log.warning(f"backfill cursor stuck inst={inst_id}; stop")
                break
            if page >= 10000:  # 安全上限，避免异常时死循环
                self._log.warning(f"backfill page cap reached inst={inst_id}; stop")
                break
        self._log.info(f"backfill done inst={inst_id} days={days} new={total}")
        return total

    async def run(self, inst_id: str) -> None:
        """实时采集主循环：订阅 trades 频道 + 后台定时 flush，直到取消/断开。

        与 ws.run() 同 task 树：ws.run 断线自动重连，run() 不主动结束；
        进程退出时由调用方取消 task，finally 冲刷剩余缓冲并关闭 ws。
        """
        ws = WSClient(demo=self._demo)
        await ws.subscribe("trades", inst_id, self._on_trade)
        flush = asyncio.create_task(self._flush_loop())
        try:
            await ws.run()
        finally:
            flush.cancel()
            async with self._lock:
                await self._flush_locked()
            await ws.close()

    async def _on_trade(self, item: dict[str, Any]) -> None:
        """WS trades 频道回调：解析入缓冲，满则冲刷。"""
        trade = parse_trade(item)
        if trade is None:
            return
        async with self._lock:
            self._buffer.append(trade)
            if len(self._buffer) >= self._buffer_size:
                await self._flush_locked()

    async def _flush_loop(self) -> None:
        """后台定时 flush，避免低频品种数据滞留内存或进程崩溃丢数据。"""
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                async with self._lock:
                    await self._flush_locked()
        except asyncio.CancelledError:
            raise

    async def _flush_locked(self) -> None:
        """在持锁前提下把缓冲批量落盘（调用方负责加锁）。"""
        if not self._buffer:
            return
        batch = self._buffer
        self._buffer = []
        try:
            saved = self._storage.save_trades(batch)
            self._log.debug(f"flush trades batch={len(batch)} saved={saved}")
        except Exception as e:  # noqa: BLE001
            self._log.warning(f"trade flush failed: {e}")


# 占位以满足类型检查（Callback 签名为 (dict) -> Awaitable[None]）
_ = Callable
