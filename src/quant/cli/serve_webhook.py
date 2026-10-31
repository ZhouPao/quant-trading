"""serve-webhook 命令：启动 FastAPI webhook server（spec §6）。

用法：
  quant serve-webhook --config config/config.toml
"""
from __future__ import annotations

from redis.asyncio import Redis
from uvicorn import run as uvicorn_run

from quant.foundation.config import Settings
from quant.foundation.heartbeat import Heartbeat
from quant.foundation.logger import get_logger, setup_logger
from quant.queue.signal_queue import Deduper, SignalQueue
from quant.state.sqlite_store import SQLiteStore
from quant.webhook.server import WebhookServer

#: 这些地址只在本机可达，不需要鉴权；其余（0.0.0.0/公网 IP）必须配置鉴权
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def needs_auth(host: str, auth_token: str, hmac_secret: str) -> bool:
    """对外可达却没有鉴权 = 任何人可推送下单信号（M8-8）。"""
    return host not in _LOOPBACK_HOSTS and not (auth_token or hmac_secret)


def main(args) -> int:
    """启动 webhook server（阻塞）。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.serve_webhook")

    webhook = settings.webhook
    if needs_auth(webhook.host, webhook.auth_token, webhook.hmac_secret):
        log.error(
            f"拒绝启动：host={webhook.host} 对外可达，但 [webhook] "
            "auth_token 与 hmac_secret 都为空。请先配置共享 token"
        )
        return 1

    redis = Redis.from_url(settings.redis.url, decode_responses=True)
    # 解析失败的死信也要落 SQLite，否则看板只能看到计数、看不到原始 body
    store = (
        SQLiteStore(settings.state.db_path)
        if settings.storage.dead_letter_persist
        else None
    )
    queue = SignalQueue(
        redis,
        pending_key=settings.redis.queue_key,
        inflight_key=settings.redis.inflight_key,
        dead_key=settings.redis.dead_key,
        store=store,
    )
    deduper = Deduper(
        redis,
        prefix=settings.redis.dedup_prefix,
        ttl_seconds=settings.redis.dedup_ttl_seconds,
    )
    server = WebhookServer(
        queue,
        deduper,
        auth_token=settings.webhook.auth_token,
        hmac_secret=settings.webhook.hmac_secret,
        path=settings.webhook.path,
        # 心跳周期 30s，TTL 由 max(2×周期, 60s) 派生
        heartbeat=Heartbeat(redis, "webhook", interval=30),
    )

    log.info(
        f"webhook listening on http://{settings.webhook.host}:"
        f"{settings.webhook.port}{settings.webhook.path}"
    )
    uvicorn_run(
        server.create_app(),
        host=settings.webhook.host,
        port=settings.webhook.port,
        log_level=settings.logging.level.lower(),
    )
    if store is not None:
        store.close()
    return 0
