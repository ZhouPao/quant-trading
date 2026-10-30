"""Webhook FastAPI server（spec §6）。

端点：
- POST {path}：接收 TV alert，鉴权 → 解析 → 去重 → 入队
- GET /health：健康检查

状态码：
- 401 鉴权失败
- 400 解析失败（原始 body 入死信）
- 200 {"status": "ok" | "duplicate"}
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from quant.foundation.heartbeat import Heartbeat
from quant.foundation.logger import get_logger
from quant.queue.signal_queue import Deduper, SignalQueue
from quant.webhook.auth import verify_signature, verify_token
from quant.webhook.parser import AlertParser

log = get_logger("webhook.server")


class WebhookServer:
    """FastAPI 应用工厂。"""

    def __init__(
        self,
        queue: SignalQueue,
        deduper: Deduper,
        parser: AlertParser | None = None,
        auth_token: str = "",
        hmac_secret: str = "",
        path: str = "/webhook/tv",
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self._queue = queue
        self._deduper = deduper
        self._parser = parser or AlertParser()
        self._auth_token = auth_token
        self._hmac_secret = hmac_secret
        self._path = path
        self._heartbeat = heartbeat

    def create_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            if self._heartbeat is not None:
                await self._heartbeat.beat()
                self._heartbeat.start()
            try:
                yield
            finally:
                if self._heartbeat is not None:
                    await self._heartbeat.stop()

        app = FastAPI(title="quant-trading webhook", lifespan=lifespan)

        @app.post(self._path)
        async def receive_alert(request: Request) -> dict:
            body = await request.body()

            # 1. 鉴权：token 优先，其次 HMAC
            if self._auth_token:
                if not verify_token(
                    request.headers.get("X-Auth-Token", ""), self._auth_token
                ):
                    raise HTTPException(status_code=401, detail="invalid token")
            elif self._hmac_secret:
                if not verify_signature(
                    body,
                    request.headers.get("X-Signature", ""),
                    self._hmac_secret,
                ):
                    raise HTTPException(
                        status_code=401, detail="invalid signature"
                    )

            # 2. 解析
            try:
                signal = self._parser.parse(body.decode("utf-8"))
            except UnicodeDecodeError as e:
                await self._queue.push_dead_raw(
                    body.decode("utf-8", errors="replace"),
                    reason=f"decode error: {e}",
                )
                raise HTTPException(status_code=400, detail="invalid encoding") from e
            except Exception as e:
                await self._queue.push_dead_raw(
                    body.decode("utf-8", errors="replace"), reason=str(e)
                )
                raise HTTPException(
                    status_code=400, detail=f"parse error: {e}"
                ) from e

            # 3. 去重
            if await self._deduper.is_duplicate(signal):
                log.info(f"duplicate signal ignored strategy={signal.strategy_id}")
                return {"status": "duplicate", "signal_id": signal.signal_id}

            # 4. 入队
            await self._queue.push(signal)
            log.info(
                f"signal enqueued strategy={signal.strategy_id} "
                f"side={signal.side} inst={signal.inst_id} id={signal.signal_id}"
            )
            return {"status": "ok", "signal_id": signal.signal_id}

        @app.get("/health")
        async def health() -> dict:
            return {
                "status": "ok",
                "pending": await self._queue.pending_length(),
                "inflight": await self._queue.inflight_length(),
                "dead": await self._queue.dead_length(),
            }

        return app


__all__ = ["WebhookServer"]
