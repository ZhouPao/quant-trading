"""Webhook 单元测试（spec §6）。

覆盖：
- AlertParser：正常/缺字段/非法 JSON/类型错误/限价无价
- auth：HMAC 与 token 校验
- server：200 入队、401 鉴权、400 解析失败入死信、重复信号、/health
"""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest

from quant.foundation.errors import ParseError
from quant.foundation.heartbeat import Heartbeat
from quant.queue.signal_queue import Deduper, SignalQueue
from quant.webhook.auth import verify_signature, verify_token
from quant.webhook.parser import AlertParser
from quant.webhook.server import WebhookServer

VALID = {
    "strategy_id": "sma_cross",
    "side": "buy",
    "order_type": "market",
    "inst_id": "BTC-USDT",
    "qty": "0.001",
}


# =====================================================================
# Parser
# =====================================================================


class TestAlertParser:
    def test_parse_market(self) -> None:
        signal = AlertParser().parse(json.dumps(VALID), now_ms=1_700_000_000_000)

        assert signal.strategy_id == "sma_cross"
        assert signal.side == "buy"
        assert signal.order_type == "market"
        assert str(signal.qty) == "0.001"
        assert signal.price is None
        assert signal.ts == 1_700_000_000_000
        assert signal.signal_id  # UUID

    def test_parse_limit_with_price(self) -> None:
        payload = {**VALID, "order_type": "limit", "price": "60000"}
        signal = AlertParser().parse(json.dumps(payload))
        assert signal.price is not None
        assert str(signal.price) == "60000"

    def test_qty_omitted_is_allowed(self) -> None:
        """qty 可省略：由 worker 按服务端仓位计算补量（M8-14）。"""
        payload = {k: v for k, v in VALID.items() if k != "qty"}
        signal = AlertParser().parse(json.dumps(payload))
        assert signal.qty is None
        assert signal.size_pct is None

    def test_size_pct_parsed(self) -> None:
        payload = {k: v for k, v in VALID.items() if k != "qty"}
        payload["size_pct"] = "0.2"
        signal = AlertParser().parse(json.dumps(payload))
        assert signal.qty is None
        assert signal.size_pct == Decimal("0.2")

    def test_missing_field_raises(self) -> None:
        payload = {k: v for k, v in VALID.items() if k != "strategy_id"}
        with pytest.raises(ParseError, match="missing field: strategy_id"):
            AlertParser().parse(json.dumps(payload))

    def test_empty_field_raises(self) -> None:
        payload = {**VALID, "inst_id": ""}
        with pytest.raises(ParseError, match="missing field: inst_id"):
            AlertParser().parse(json.dumps(payload))

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ParseError, match="invalid json"):
            AlertParser().parse("{not json")

    def test_non_object_raises(self) -> None:
        with pytest.raises(ParseError, match="json object"):
            AlertParser().parse("[1, 2]")

    def test_invalid_side_raises(self) -> None:
        with pytest.raises(ParseError, match="invalid side"):
            AlertParser().parse(json.dumps({**VALID, "side": "hold"}))

    def test_invalid_order_type_raises(self) -> None:
        with pytest.raises(ParseError, match="invalid order_type"):
            AlertParser().parse(json.dumps({**VALID, "order_type": "stop"}))

    def test_invalid_qty_raises(self) -> None:
        with pytest.raises(ParseError, match="invalid qty"):
            AlertParser().parse(json.dumps({**VALID, "qty": "abc"}))

    def test_limit_without_price_raises(self) -> None:
        with pytest.raises(ParseError):
            AlertParser().parse(json.dumps({**VALID, "order_type": "limit"}))


# =====================================================================
# Auth
# =====================================================================


class TestAuth:
    def test_signature_valid(self) -> None:
        body = b'{"a":1}'
        sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        assert verify_signature(body, sig, "secret") is True

    def test_signature_invalid(self) -> None:
        assert verify_signature(b"body", "deadbeef", "secret") is False

    def test_signature_empty_secret(self) -> None:
        assert verify_signature(b"body", "x", "") is False

    def test_token_valid(self) -> None:
        assert verify_token("abc", "abc") is True

    def test_token_invalid(self) -> None:
        assert verify_token("abc", "xyz") is False

    def test_token_empty_expected_allows(self) -> None:
        assert verify_token("", "") is True


# =====================================================================
# Server
# =====================================================================


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _client(fake_redis, **kwargs) -> tuple[httpx.AsyncClient, SignalQueue]:
    queue = SignalQueue(fake_redis)
    deduper = Deduper(fake_redis)
    server = WebhookServer(queue, deduper, **kwargs)
    transport = httpx.ASGITransport(app=server.create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test"), queue


class TestWebhookServer:
    async def test_lifespan_writes_heartbeat(self, fake_redis) -> None:
        """webhook 是 7 个常驻进程之一，必须能被 /api/processes 判活。"""
        heartbeat = Heartbeat(fake_redis, "webhook", interval=30)
        server = WebhookServer(
            SignalQueue(fake_redis), Deduper(fake_redis), heartbeat=heartbeat
        )
        app = server.create_app()

        # ASGITransport 默认不跑 lifespan，直接进上下文管理器
        async with app.router.lifespan_context(app):
            raw = await fake_redis.get("quant:heartbeat:webhook")
            ttl = await fake_redis.ttl("quant:heartbeat:webhook")

        assert json.loads(raw)["pid"] > 0
        assert 0 < ttl <= 60
        # 退出 lifespan 后轮询任务要停掉，否则测试进程里留孤儿
        assert heartbeat._task is None

    async def test_post_ok(self, fake_redis) -> None:
        client, queue = _client(fake_redis)
        async with client:
            resp = await client.post("/webhook/tv", json=VALID)

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["signal_id"]
        assert await queue.pending_length() == 1

    async def test_duplicate_returns_duplicate(self, fake_redis) -> None:
        client, queue = _client(fake_redis)
        async with client:
            first = await client.post("/webhook/tv", json=VALID)
            second = await client.post("/webhook/tv", json=VALID)

        assert first.json()["status"] == "ok"
        assert second.json()["status"] == "duplicate"
        assert await queue.pending_length() == 1

    async def test_parse_error_goes_dead(self, fake_redis) -> None:
        client, queue = _client(fake_redis)
        async with client:
            resp = await client.post(
                "/webhook/tv", content=b'{"side":"buy"}',
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 400
        assert await queue.dead_length() == 1

    async def test_token_required(self, fake_redis) -> None:
        client, _ = _client(fake_redis, auth_token="secret-token")
        async with client:
            assert (
                await client.post("/webhook/tv", json=VALID)
            ).status_code == 401
            ok = await client.post(
                "/webhook/tv", json=VALID,
                headers={"X-Auth-Token": "secret-token"},
            )

        assert ok.status_code == 200

    async def test_token_accepted_from_query(self, fake_redis) -> None:
        """TV 无法自定义请求头，凭据可以放在 URL 上（M8-8）。"""
        client, queue = _client(fake_redis, auth_token="secret-token")
        async with client:
            resp = await client.post(
                "/webhook/tv?token=secret-token", json=VALID
            )

        assert resp.status_code == 200
        assert await queue.pending_length() == 1

    async def test_token_accepted_from_body(self, fake_redis) -> None:
        """TV 也可以把 token 写进 alert message 正文（M8-8）。"""
        client, queue = _client(fake_redis, auth_token="secret-token")
        async with client:
            resp = await client.post(
                "/webhook/tv", json={**VALID, "token": "secret-token"}
            )

        assert resp.status_code == 200
        assert await queue.pending_length() == 1

    async def test_wrong_body_token_rejected(self, fake_redis) -> None:
        client, queue = _client(fake_redis, auth_token="secret-token")
        async with client:
            resp = await client.post(
                "/webhook/tv", json={**VALID, "token": "nope"}
            )

        assert resp.status_code == 401
        assert await queue.pending_length() == 0

    async def test_hmac_required(self, fake_redis) -> None:
        body = json.dumps(VALID).encode()
        sig = hmac.new(b"hmac-secret", body, hashlib.sha256).hexdigest()
        client, _ = _client(fake_redis, hmac_secret="hmac-secret")
        async with client:
            assert (
                await client.post("/webhook/tv", content=body)
            ).status_code == 401
            ok = await client.post(
                "/webhook/tv", content=body,
                headers={"X-Signature": sig, "Content-Type": "application/json"},
            )

        assert ok.status_code == 200

    async def test_health(self, fake_redis) -> None:
        client, _ = _client(fake_redis)
        async with client:
            resp = await client.get("/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["pending"] == 0
