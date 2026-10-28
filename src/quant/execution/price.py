"""实时价格簿（M3）。

WS tickers 推送 → 内存价格表；worker 用它作为市价来源。
"""
from __future__ import annotations

from decimal import Decimal

from quant.foundation.logger import get_logger
from quant.models import Tick

log = get_logger("execution.price")


class LivePriceBook:
    """按 inst_id 保存最新成交价 + 盘口。"""

    def __init__(self) -> None:
        self._ticks: dict[str, Tick] = {}

    def update(self, tick: Tick) -> None:
        self._ticks[tick.inst_id] = tick

    async def update_from_payload(self, item: dict) -> None:
        """WS tickers 回调入口（dict → Tick）。"""
        from quant.okx.ws_client import parse_ticker

        tick = parse_ticker(item)
        if tick is None:
            log.debug(f"skip invalid ticker: {item.get('instId')}")
            return
        self.update(tick)

    def get_tick(self, inst_id: str) -> Tick | None:
        return self._ticks.get(inst_id)

    def get_price(self, inst_id: str) -> Decimal | None:
        """最新成交价；无 last 时退化到中间价。"""
        tick = self._ticks.get(inst_id)
        if tick is None:
            return None
        if tick.last_px > 0:
            return tick.last_px
        return (tick.bid_px + tick.ask_px) / 2

    def as_provider(self):
        """返回 worker 需要的 price_provider(inst_id) -> Decimal | None。"""

        def provider(inst_id: str) -> Decimal | None:
            return self.get_price(inst_id)

        return provider


class RestPriceProvider:
    """OKX 公开 ticker REST 报价（无需鉴权）。

    WS 不可用时作为实时价来源；每次调用一次 REST 请求。
    """

    def __init__(self, client) -> None:
        self._client = client

    def __call__(self, inst_id: str) -> Decimal | None:
        try:
            rows = self._client.get("/api/v5/market/ticker", {"instId": inst_id})
        except Exception as e:  # noqa: BLE001
            log.warning(f"rest ticker failed inst={inst_id}: {e}")
            return None
        if not rows:
            return None
        raw = rows[0].get("last")
        if not raw:
            return None
        return Decimal(str(raw))


__all__ = ["LivePriceBook", "RestPriceProvider"]
