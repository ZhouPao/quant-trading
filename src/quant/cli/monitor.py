"""monitor 命令：启动监控进程（spec §12.4，M4）。

用法：
  quant monitor

流程：每分钟采集账户/持仓快照，评估告警规则（worker 心跳丢失、
Redis 队列堆积、死信新增、保证金率过低、余额异常波动），命中即记日志
并（若配置 SMTP）发邮件；同时写监控自身心跳。
"""
from __future__ import annotations

import asyncio

from redis.asyncio import Redis

from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.monitor.alert_service import AlertService
from quant.monitor.alerter import EmailAlerter
from quant.monitor.monitor import Monitor
from quant.okx.client import OKXClient
from quant.report.daily import DailyReporter
from quant.state.sqlite_store import SQLiteStore
from quant.storage.quality import QualityReporter
from quant.storage.retention import RetentionPurger


async def _run(args, settings: Settings) -> int:
    log = get_logger("cli.monitor")

    client = OKXClient(settings.okx)
    redis = Redis.from_url(settings.redis.url, decode_responses=True)
    store = SQLiteStore(settings.state.db_path)
    alerter = EmailAlerter(settings.smtp)
    # engine 状态键带 :{strategy}:{inst} 后缀，与 engine.publish_status 保持一致
    engine_key = (
        f"{settings.engine.status_key}:"
        f"{settings.engine.strategy}:{settings.engine.inst}"
        if settings.engine.enabled
        else ""
    )
    monitor_settings = settings.monitor.model_copy(
        update={
            "engine_status_key": engine_key,
            "engine_timeout_seconds": settings.engine.status_timeout_seconds,
        }
    )
    # 日报 FAIL 也走同一告警体系（M4-8）：与 Monitor 共用同一个 AlertService
    alert_service = AlertService(store, redis, alerter, monitor_settings)
    daily = (
        DailyReporter(
            settings, store, redis, alerter, alert_service=alert_service
        )
        if settings.report.enabled
        else None
    )
    monitor = Monitor(
        client,
        redis,
        monitor_settings,
        queue_key=settings.redis.queue_key,
        dead_key=settings.redis.dead_key,
        alerter=alerter,
        store=store,
        daily=daily,
        retention=RetentionPurger(settings, store, redis),
        alert_service=alert_service,
        quality=(
            QualityReporter(settings.storage.data_dir)
            if settings.quality.enabled
            else None
        ),
        quality_cooldown_seconds=settings.quality.alert_cooldown_seconds,
    )

    log.info(
        f"monitor started interval={settings.monitor.interval_seconds}s "
        f"smtp={'on' if alerter.enabled() else 'off'} "
        f"alerts_persist={'on' if settings.monitor.alerts_persist else 'off'} "
        f"engine_heartbeat={'on' if engine_key else 'off'} "
        f"daily_report={'on' if daily else 'off'} "
        f"retention={'on' if settings.storage.retention_enabled else 'off'}"
    )
    try:
        await monitor.run()
        return 0
    finally:
        client.close()
        store.close()
        await redis.aclose()


def main(args) -> int:
    """启动监控（阻塞直到 SIGINT/SIGTERM）。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.monitor")
    try:
        return asyncio.run(_run(args, settings))
    except KeyboardInterrupt:
        log.info("monitor interrupted")
        return 0
