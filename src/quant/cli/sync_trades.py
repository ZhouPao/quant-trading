"""sync-trades 命令：采集 OKX 逐笔成交（M2-14 订单流足迹图）。

用法：
  quant sync-trades                                  # 回补默认 1 天后退出
  quant sync-trades --inst BTC-USDT-SWAP --days 1
  quant sync-trades --inst BTC-USDT-SWAP --loop       # 回补 + 常驻实时采集

回补走 REST history-trades（往旧翻页），实时走 WS trades 频道（异步落 parquet）。
历史区间 [now-days, now] 由回补覆盖，now 之后由实时流覆盖，无数据空隙。
"""
from __future__ import annotations

import asyncio

from quant.fetcher.trade_fetcher import TradeFetcher
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.okx.client import OKXClient
from quant.storage.trade_storage import TradeStorage


async def _run_realtime(fetcher: TradeFetcher, inst_id: str) -> None:
    """常驻实时采集（ws.run 内部断线重连，task 不主动结束）。"""
    await fetcher.run(inst_id)


def main(args) -> int:
    """采集 OKX 逐笔成交。返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.sync_trades")

    insts = (
        [s.strip() for s in args.inst.split(",") if s.strip()]
        if args.inst
        else list(settings.candles.insts)
    )
    if not insts:
        log.error("no inst configured (set [candles] insts or pass --inst)")
        return 1

    days = args.days if args.days is not None else 1

    client = OKXClient(settings.okx)
    try:
        storage = TradeStorage(settings.storage.data_dir)
        fetcher = TradeFetcher(
            client, storage, demo=settings.okx.demo,
            buffer_size=500, flush_interval=1.0,
        )
        for inst in insts:
            try:
                saved = fetcher.backfill(inst, days=days)
                log.info(f"backfill done inst={inst} days={days} new={saved}")
            except Exception as e:  # noqa: BLE001
                log.error(f"backfill failed inst={inst}: {e}")

        if not args.loop:
            return 0

        log.info(f"realtime trade stream started insts={insts}")
        async def _main() -> None:
            await asyncio.gather(
                *(_run_realtime(fetcher, inst) for inst in insts)
            )
        try:
            asyncio.run(_main())
        except (KeyboardInterrupt, SystemExit):
            log.info("sync-trades stopped")
        return 0
    finally:
        client.close()
