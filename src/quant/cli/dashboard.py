"""dashboard 命令：启动只读运维看板（M4 扩展）。

用法：
  quant dashboard                        # http://127.0.0.1:8080
  quant dashboard --host 0.0.0.0 --port 9000

只读：不下单、不写 SQLite、不改 Redis；账户/持仓数据来自 monitor 发布的
Redis 快照，因此只启动 dashboard 时页面会显示 monitor 离线。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn
from redis.asyncio import Redis

from quant.dashboard.app import create_app
from quant.dashboard.manual import ManualTrader
from quant.dashboard.service import DashboardService
from quant.foundation.config import Settings
from quant.foundation.heartbeat import Heartbeat
from quant.foundation.logger import get_logger, setup_logger
from quant.state.sqlite_store import SQLiteStore


async def _serve(args, settings: Settings, log) -> int:
    store = SQLiteStore(settings.state.db_path)
    redis = Redis.from_url(
        settings.redis.url,
        decode_responses=True,
        # 不设超时时 Redis 挂起会让 /api/overview 永久阻塞、连降级都走不到；
        # 设了超时，读失败才能落到各数据源的独立降级上
        socket_connect_timeout=2.0,
        socket_timeout=5.0,
    )
    web_dir = Path(args.web_dir) if args.web_dir else settings.dashboard.web_dir
    # 手动减仓是唯一会写库/下单的入口，与只读的 service 分开构造
    manual = ManualTrader(settings, store, redis)
    app = create_app(
        DashboardService(store, redis, settings),
        web_dir,
        manual=manual,
        config_path=Path(args.config),
    )
    # 心跳由进程自己写而不是借 supervisor：事件循环卡死时 supervisor
    # 仍会认为子进程活着，只有自写心跳能反映出「进程在但不干活」
    heartbeat = Heartbeat(redis, "dashboard", interval=30)
    await heartbeat.beat()
    heartbeat.start()

    host = args.host or settings.dashboard.host
    port = args.port or settings.dashboard.port
    log.info(f"dashboard listening on http://{host}:{port} web_dir={web_dir}")

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
        return 0
    finally:
        await heartbeat.stop()
        await redis.aclose()
        store.close()


def main(args) -> int:
    """启动看板（阻塞直到 SIGINT/SIGTERM）。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.dashboard")
    try:
        return asyncio.run(_serve(args, settings, log))
    except KeyboardInterrupt:
        log.info("dashboard interrupted")
        return 0
