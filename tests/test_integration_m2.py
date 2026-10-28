"""M2 集成测试：webhook → queue → worker → SQLite（spec §14.2）。

用 fakeredis + httpx.ASGITransport，全程同一事件循环。
"""
from __future__ import annotations

import json
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest

from quant.backtest.signal import Signal
from quant.execution.executor import Executor
from quant.execution.worker import Worker
from quant.queue.signal_queue import Deduper, SignalQueue
from quant.risk.checker import RiskChecker
from quant.risk.config import RiskConfig
from quant.state.sqlite_store import SQLiteStore
from quant.webhook.server import WebhookServer

VALID = {
    "strategy_id": "sma_cross",
    "side": "buy",
    "order_type": "market",
    "inst_id": "BTC-USDT",
    "qty": "1",
}


@pytest.fixture
async def stack(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SQLiteStore(tmp_path / "quant.db")
    queue = SignalQueue(redis)
    deduper = Deduper(redis)
    server = WebhookServer(queue, deduper, auth_token="tk")
    app = server.create_app()

    risk = RiskChecker(
        RiskConfig(
            strategy_id="sma_cross",
            max_position_pct=Decimal("0.9"),
            max_total_pct=Decimal("0.95"),
            max_order_qty=Decimal("100000"),
            daily_loss_limit=Decimal("0.5"),
        ),
        store,
    )
    worker = Worker(
        queue=queue,
        risk=risk,
        executor=Executor(),
        store=store,
        redis=redis,
        price_provider=lambda inst_id: Decimal("100"),
    )

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    yield client, queue, store, worker, redis
    await client.aclose()
    store.close()


class TestFullPipeline:
    async def test_alert_to_position(self, stack) -> None:
        client, queue, store, worker, _ = stack

        resp = await client.post(
            "/webhook/tv", json=VALID, headers={"X-Auth-Token": "tk"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        signal_id = resp.json()["signal_id"]
        assert await queue.pending_length() == 1

        assert await worker.run_once(timeout=1) is True

        assert store.get_signal_status(signal_id) == "executed"
        position = store.get_position("BTC-USDT")
        assert position.qty == Decimal("1")
        assert position.avg_price == Decimal("100")

        trades = store.get_recent_trades(10)
        assert len(trades) == 1
        assert trades[0].inst_id == "BTC-USDT"

    async def test_duplicate_alert_single_execution(self, stack) -> None:
        client, queue, store, worker, _ = stack

        first = await client.post(
            "/webhook/tv", json=VALID, headers={"X-Auth-Token": "tk"}
        )
        second = await client.post(
            "/webhook/tv", json=VALID, headers={"X-Auth-Token": "tk"}
        )

        assert first.json()["status"] == "ok"
        assert second.json()["status"] == "duplicate"
        assert await queue.pending_length() == 1

        await worker.run_once(timeout=1)
        assert len(store.get_recent_trades(10)) == 1

    async def test_unauthorized_not_enqueued(self, stack) -> None:
        client, queue, _, _, _ = stack
        resp = await client.post("/webhook/tv", json=VALID)

        assert resp.status_code == 401
        assert await queue.pending_length() == 0

    async def test_malformed_goes_dead(self, stack) -> None:
        client, queue, _, _, _ = stack
        resp = await client.post(
            "/webhook/tv",
            content=b'{"strategy_id":"x"}',
            headers={
                "X-Auth-Token": "tk",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 400
        assert await queue.dead_length() == 1

    async def test_two_signals_processed_in_order(self, stack) -> None:
        client, queue, store, worker, _ = stack
        buy = dict(VALID)
        sell = {**VALID, "side": "sell"}

        await client.post(
            "/webhook/tv", json=buy, headers={"X-Auth-Token": "tk"}
        )
        await client.post(
            "/webhook/tv", json=sell, headers={"X-Auth-Token": "tk"}
        )

        await worker.run_once(timeout=1)
        await worker.run_once(timeout=1)

        # 买 1 后卖 1 → 空仓
        assert store.get_position("BTC-USDT").is_flat
        assert len(store.get_recent_trades(10)) == 2

    async def test_health_reflects_queue(self, stack) -> None:
        client, _, _, _, _ = stack
        await client.post(
            "/webhook/tv", json=VALID, headers={"X-Auth-Token": "tk"}
        )

        resp = await client.get("/health")
        assert resp.json()["pending"] == 1

    async def test_recovered_inflight_not_duplicated(self, stack) -> None:
        client, queue, store, worker, _ = stack
        resp = await client.post(
            "/webhook/tv", json=VALID, headers={"X-Auth-Token": "tk"}
        )
        signal_id = resp.json()["signal_id"]

        # 模拟处理成功但清理 inflight 前崩溃
        await queue.pop(timeout=1)
        store.save_signal(
            Signal(
                strategy_id="sma_cross",
                side="buy",
                order_type="market",
                inst_id="BTC-USDT",
                qty=Decimal("1"),
                ts=0,
                signal_id=signal_id,
            ),
            "executed",
        )

        # 恢复 inflight 并处理：应跳过，不重复下单
        recovered = await queue.recover_inflight()
        assert len(recovered) == 1
        await worker.run_once(timeout=1)

        assert store.get_recent_trades(10) == []

    async def test_json_roundtrip_fields(self, stack) -> None:
        client, queue, _, _, _ = stack
        await client.post(
            "/webhook/tv",
            json={**VALID, "order_type": "limit", "price": "60000"},
            headers={"X-Auth-Token": "tk"},
        )

        signal = await queue.pop(timeout=1)
        assert signal is not None
        assert signal.order_type == "limit"
        assert str(signal.price) == "60000"

    async def test_dead_payload_reason(self, stack) -> None:
        client, queue, _, _, _ = stack
        await client.post(
            "/webhook/tv",
            content=json.dumps({"side": "buy"}).encode(),
            headers={
                "X-Auth-Token": "tk",
                "Content-Type": "application/json",
            },
        )

        raw = await queue._redis.lindex(queue.dead_key, 0)
        payload = json.loads(raw)
        assert "missing field" in payload["reason"]
