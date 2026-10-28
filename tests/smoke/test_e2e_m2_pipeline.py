"""M2 全链路烟测：真实 HTTP → webhook → fakeredis → worker → SQLite。

无外部依赖（不需要真实 Redis 服务）：
- uvicorn 监听 127.0.0.1:8765
- fakeredis 模拟 Redis 队列
- httpx 发真实 HTTP 请求

运行：
    python3 tests/smoke/test_e2e_m2_pipeline.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

import fakeredis.aioredis
import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.backtest.signal import Signal  # noqa: E402
from quant.execution.executor import Executor  # noqa: E402
from quant.execution.worker import Worker  # noqa: E402
from quant.foundation.logger import setup_logger  # noqa: E402
from quant.queue.signal_queue import Deduper, SignalQueue  # noqa: E402
from quant.risk.checker import RiskChecker  # noqa: E402
from quant.risk.config import RiskConfig  # noqa: E402
from quant.state.sqlite_store import SQLiteStore  # noqa: E402
from quant.webhook.server import WebhookServer  # noqa: E402

HOST = "127.0.0.1"
PORT = 8765
BASE = f"http://{HOST}:{PORT}"
TOKEN = "smoke-token"

_passed = 0
_failed = 0


def _check(label: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"[PASS] {label}{f' — {detail}' if detail else ''}")
    else:
        _failed += 1
        print(f"[FAIL] {label}{f' — {detail}' if detail else ''}")


def _payload(side: str = "buy", qty: str = "1") -> dict:
    return {
        "strategy_id": "sma_cross",
        "side": side,
        "order_type": "market",
        "inst_id": "BTC-USDT",
        "qty": qty,
    }


async def main() -> int:
    setup_logger(level="WARNING")
    print("=" * 62)
    print("M2 全链路烟测：HTTP webhook → Redis 队列 → worker → SQLite")
    print("=" * 62)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "quant.db"
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = SQLiteStore(db_path)
        queue = SignalQueue(redis)
        deduper = Deduper(redis)

        app = WebhookServer(
            queue, deduper, auth_token=TOKEN
        ).create_app()
        config = uvicorn.Config(
            app, host=HOST, port=PORT, log_level="error", access_log=False
        )
        server = uvicorn.Server(config)
        serve_task = asyncio.create_task(server.serve())

        # 等待端口就绪
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        _check("webhook 服务启动", server.started, f"{BASE}/webhook/tv")

        worker = Worker(
            queue=queue,
            risk=RiskChecker(
                RiskConfig(
                    strategy_id="sma_cross",
                    max_position_pct=Decimal("0.9"),
                    max_total_pct=Decimal("0.95"),
                    max_order_qty=Decimal("100000"),
                    daily_loss_limit=Decimal("0.5"),
                ),
                store,
            ),
            executor=Executor(),
            store=store,
            redis=redis,
            price_provider=lambda inst_id: Decimal("100"),
        )

        signal_ids: list[str] = []
        try:
            async with httpx.AsyncClient(base_url=BASE, timeout=5) as client:
                # 1. 未授权
                r = await client.post("/webhook/tv", json=_payload())
                _check("401 未授权拒绝", r.status_code == 401, f"status={r.status_code}")

                # 2. 正常入队
                r = await client.post(
                    "/webhook/tv",
                    json=_payload("buy", "1"),
                    headers={"X-Auth-Token": TOKEN},
                )
                body = r.json()
                signal_ids.append(body.get("signal_id", ""))
                _check(
                    "buy 信号入队",
                    r.status_code == 200 and body.get("status") == "ok",
                    f"id={body.get('signal_id', '')[:8]}",
                )

                # 3. 重发去重
                r = await client.post(
                    "/webhook/tv",
                    json=_payload("buy", "1"),
                    headers={"X-Auth-Token": TOKEN},
                )
                _check(
                    "重复 alert 去重",
                    r.json().get("status") == "duplicate",
                    f"pending={await queue.pending_length()}",
                )

                # 4. 卖出入队
                r = await client.post(
                    "/webhook/tv",
                    json=_payload("sell", "1"),
                    headers={"X-Auth-Token": TOKEN},
                )
                signal_ids.append(r.json().get("signal_id", ""))
                _check("sell 信号入队", r.json().get("status") == "ok")

                # 5. 健康检查
                r = await client.get("/health")
                health = r.json()
                _check(
                    "健康检查",
                    r.status_code == 200 and health["pending"] == 2,
                    f"pending={health['pending']}",
                )

                # 6. 解析失败 → 死信
                r = await client.post(
                    "/webhook/tv",
                    content=b'{"bad":"payload"}',
                    headers={
                        "X-Auth-Token": TOKEN,
                        "Content-Type": "application/json",
                    },
                )
                _check(
                    "非法 payload → 死信",
                    r.status_code == 400 and await queue.dead_length() == 1,
                    f"dead={await queue.dead_length()}",
                )
        finally:
            server.should_exit = True
            await asyncio.wait_for(serve_task, timeout=5)

        # 7. worker 处理两条信号
        processed = 0
        while await worker.run_once(timeout=1):
            processed += 1
        _check("worker 消费全部信号", processed == 2, f"processed={processed}")

        # 8. 状态 + 持仓 + 成交
        statuses = [
            store.get_signal_status(sid) for sid in signal_ids if sid
        ]
        _check(
            "信号状态 executed",
            statuses == ["executed", "executed"],
            f"statuses={statuses}",
        )
        position = store.get_position("BTC-USDT")
        _check(
            "买卖后恢复空仓",
            position.is_flat,
            f"qty={position.qty}",
        )
        trades = store.get_recent_trades(10)
        _check(
            "两笔成交落库",
            len(trades) == 2,
            f"trades={[(t.side, str(t.price)) for t in trades]}",
        )

        # 9. 崩溃恢复：inflight 回到 pending，处理不重复
        await queue.push(
            Signal(
                strategy_id="sma_cross",
                side="buy",
                order_type="market",
                inst_id="BTC-USDT",
                qty=Decimal("1"),
                ts=int(time.time() * 1000),
                signal_id="recovery-1",
            )
        )
        await queue.pop(timeout=1)  # 模拟崩溃时留在 inflight
        recovered = await queue.recover_inflight()
        _check("inflight 崩溃恢复", len(recovered) == 1, f"recovered={len(recovered)}")

        await worker.run_once(timeout=1)
        _check(
            "恢复后信号执行",
            store.get_signal_status("recovery-1") == "executed",
            f"status={store.get_signal_status('recovery-1')}",
        )
        _check(
            "恢复后 position=1",
            store.get_position("BTC-USDT").qty == Decimal("1"),
        )

        store.close()
        await redis.aclose()

    print("\n" + "=" * 62)
    print(f"总计: {_passed + _failed} 个场景, {_passed} 通过, {_failed} 失败")
    print("=" * 62)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
