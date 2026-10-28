"""run-engine 命令：本地策略实时信号引擎。

用法：
  quant run-engine                    # 常驻，每 60s 检查一次新收盘 bar
  quant run-engine --once             # 只评估一轮后退出（排查用）
  quant run-engine --strategy rsi --inst ETH-USDT --bar 4H

每根已收盘 K 线只评估一次，信号写入 Redis 队列，由 run-worker 执行。
不依赖 TradingView；与回测/日报共用同一份策略配置。
"""
from __future__ import annotations

import asyncio

from redis.asyncio import Redis

from quant.engine.live import LiveSignalEngine
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.state.sqlite_store import SQLiteStore


async def _run(args, settings: Settings) -> int:
    log = get_logger("cli.run_engine")
    redis = Redis.from_url(settings.redis.url, decode_responses=True)
    store = SQLiteStore(settings.state.db_path)
    try:
        engine = LiveSignalEngine(settings, store, redis)
        if args.once:
            tick = await engine.run_once()
            await engine.publish_status(tick)
            log.info(
                f"engine tick status={tick.status} bar_ts={tick.bar_ts} "
                f"reason={tick.reason or '-'}"
            )
            return 0 if tick.status != "error" else 1
        await engine.run()
        return 0
    finally:
        store.close()
        await redis.aclose()


def main(args) -> int:
    """启动实时信号引擎（阻塞直到 SIGINT/SIGTERM）。"""
    settings = Settings.load(args.config)
    # 命令行参数覆盖配置，便于临时排查
    if args.strategy:
        settings.engine.strategy = args.strategy
    if args.inst:
        settings.engine.inst = args.inst
    if args.bar:
        settings.engine.bar = args.bar
    if args.interval is not None:
        settings.engine.interval_seconds = args.interval

    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.run_engine")

    if not settings.engine.enabled:
        log.warning("engine disabled (config [engine] enabled=false), exiting")
        return 0

    try:
        return asyncio.run(_run(args, settings))
    except KeyboardInterrupt:
        log.info("engine interrupted")
        return 0
