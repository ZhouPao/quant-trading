"""sync-candles 命令：把本地 K线增量同步到最近一根已收盘 bar。

用法：
  quant sync-candles                                  # 按配置同步一次后退出
  quant sync-candles --inst BTC-USDT,ETH-USDT --bar 1H,4H
  quant sync-candles --loop --interval 300            # 常驻，供 supervisor 托管

与 `fetch-candles` 的区别：不需要传日期，终点自动取「最近已收盘的 bar」，
起点取断点，适合定时轮询。未收盘的最后一根不落盘，避免污染回测与日报。
默认同步 `[candles] bars` 列出的全部周期。
"""
from __future__ import annotations

import time

from quant.fetcher.candle_fetcher import CandleFetcher
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.okx.client import OKXClient


def _sync_once(fetcher: CandleFetcher, insts: list[str], bars: list[str],
               lookback_bars: int, log) -> int:
    """同步一轮（insts × bars），返回净增根数合计。

    单个品种/周期失败只记日志，不影响其余组合继续同步。
    """
    total = 0
    for bar in bars:
        for inst in insts:
            try:
                total += fetcher.sync(inst, bar, lookback_bars=lookback_bars)
            except Exception as e:  # noqa: BLE001
                log.error(f"sync failed inst={inst} bar={bar}: {e}")
    return total


def main(args) -> int:
    """同步 K线。返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.sync_candles")

    insts = (
        [s.strip() for s in args.inst.split(",") if s.strip()]
        if args.inst
        else list(settings.candles.insts)
    )
    if not insts:
        log.error("no inst configured (set [candles] insts or pass --inst)")
        return 1

    bars = (
        [s.strip() for s in args.bar.split(",") if s.strip()]
        if args.bar
        else settings.candles.resolved_bars
    )
    if not bars:
        log.error("no bar configured (set [candles] bars or pass --bar)")
        return 1
    interval = (
        args.interval
        if args.interval is not None
        else settings.candles.interval_seconds
    )
    lookback = (
        args.lookback_bars
        if args.lookback_bars is not None
        else settings.candles.lookback_bars
    )

    client = OKXClient(settings.okx)
    try:
        fetcher = CandleFetcher(client, settings.storage.data_dir)
        if not args.loop:
            total = _sync_once(fetcher, insts, bars, lookback, log)
            log.info(f"sync done insts={insts} bars={bars} new={total}")
            return 0

        if interval <= 0:
            log.error(f"--loop requires positive interval, got {interval}")
            return 1
        log.info(
            f"candle sync loop started insts={insts} bars={bars} "
            f"interval={interval}s lookback={lookback}"
        )
        while True:
            started = time.monotonic()
            total = _sync_once(fetcher, insts, bars, lookback, log)
            log.info(f"sync round done insts={insts} bars={bars} new={total}")
            # 扣除本轮耗时，避免慢网络下间隔不断被拉长
            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))
    finally:
        client.close()
