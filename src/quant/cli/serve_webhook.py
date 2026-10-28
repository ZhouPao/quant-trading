"""serve-webhook 命令：启动 FastAPI webhook server（spec §6）。

用法：
  quant serve-webhook --config config/config.toml
"""
from __future__ import annotations

from redis.asyncio import Redis
from uvicorn import run as uvicorn_run

from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.queue.signal_queue import Deduper, SignalQueue
from quant.webhook.server import WebhookServer


def main(args) -> int:
    """启动 webhook server（阻塞）。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.serve_webhook")

    redis = Redis.from_url(settings.redis.url, decode_responses=True)
    queue = SignalQueue(
        redis,
        pending_key=settings.redis.queue_key,
        inflight_key=settings.redis.inflight_key,
        dead_key=settings.redis.dead_key,
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
        path=settings.webhook.path,
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
    return 0
