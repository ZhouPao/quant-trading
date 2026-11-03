"""P1 + P2 + P3 全链路联调测试（A2 收尾）。

现有 test_dashboard_backtest.py 的分期单元测试各自通过，但"各期联手"仍可能出缺陷：
- P4 前端消费的 GET 返回体字段一旦发生漂移，单元测试发现不了；
- P2 把结果存内存、P3 从内存取回，在进程重启后是否优雅降级，也得显式锁住。

本文件用**真实 SQLite + 真实 HTTP（ASGI）**把 P1(store 落库) + P2(登记式提交/进度)
+ P3(GET 取回) 串成端到端场景，并逐字段校验 P4 的 OptimizeJobResponse 契约。
"""
from __future__ import annotations

import asyncio
import time

import fakeredis.aioredis
import httpx

from quant.dashboard.app import create_app
from quant.dashboard.service import DashboardService
from quant.state.sqlite_store import SQLiteStore

# 复用合成世界 helper，避免重复造轮子
from tests.test_dashboard_backtest import (  # noqa: E402
    _settings,
    _write_bars,
    _write_strategy,
)


def _build_app(tmp_path, db_path=None):
    settings = _settings(tmp_path)
    _write_strategy(tmp_path)
    _write_bars(settings)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SQLiteStore(db_path or settings.state.db_path)
    dashboard = create_app(DashboardService(store, redis, settings), tmp_path / "dist")
    dashboard._test_store = store  # 便于测试侧直接读 optimize_jobs
    return dashboard, store


def _optimize_body(**overrides):
    body = {
        "strategy": "sma_cross",
        "start": "2026-09-05",
        "end": "2026-09-10",
        "warmup_bars": 50,
        "grid": "fast_len=3,5;slow_len=20,30",
        "metric": "sharpe",
        "top": 2,
    }
    body.update(overrides)
    return body


async def _submit_and_poll(client, job_id, timeout=8.0):
    """轮询 GET /api/optimize/{job_id} 直到终态；超时报错避免挂住。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        get = await client.get(f"/api/optimize/{job_id}")
        last = get.json()
        if last["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.02)
    assert last is not None and last["status"] in ("done", "failed"), last
    return last


class TestOptimizeFullChain:
    async def test_submit_poll_fetch_contract(self, tmp_path) -> None:
        """P1+P2+P3 全链：POST 登记→轮询 GET→取回结果，逐字段对齐 P4 的 OptimizeJobResponse。"""
        app, store = _build_app(tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 1) 提交（P2）：立即返回 job_id 信封
            resp = await client.post("/api/optimize", json=_optimize_body())
            assert resp.status_code == 200
            envelope = resp.json()
            assert isinstance(envelope["job_id"], str) and envelope["job_id"]
            assert envelope["status"] == "running"
            assert envelope["total"] == 4

            # 2) 进度立即落库（P1 的 save_optimize_job，status=running）
            row = store.get_optimize_job(envelope["job_id"])
            assert row is not None and row["status"] == "running"

            # 3) 轮询 GET（P3）直到终态；逐字段校验前端依赖的契约
            body = await _submit_and_poll(client, envelope["job_id"])
            assert body["job_id"] == envelope["job_id"]
            assert body["status"] == "done"
            assert body["total"] == 4
            assert body["completed"] == 4
            assert body["metric"] == "sharpe"
            assert body["n_jobs"] == 1
            assert isinstance(body["grid"], dict)  # grid 由 JSON 文本解析回对象
            assert body["grid"] == {"fast_len": [3, 5], "slow_len": [20, 30]}
            assert body["finished_at"] is not None
            assert body["error"] is None
            # 结果取回（P3 从 P2 的内存结果取回，kind=optimize）
            assert body["result"] is not None
            assert body["result"]["kind"] == "optimize"
            assert body["result"]["combo_count"] == 4
            assert len(body["result"]["results"]) == 2  # top=2

    async def test_large_grid_full_chain(self, tmp_path) -> None:
        """大网格(3×3=9) 全链：total 正确、进度写满到 total、grid 解析无误、top 生效。"""
        app, store = _build_app(tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/optimize",
                json=_optimize_body(grid="fast_len=3,5,8;slow_len=20,30,40", top=3),
            )
            job_id = resp.json()["job_id"]
            body = await _submit_and_poll(client, job_id)
            assert body["status"] == "done"
            assert body["total"] == 9
            assert body["completed"] == 9
            assert body["grid"] == {"fast_len": [3, 5, 8], "slow_len": [20, 30, 40]}
            assert len(body["result"]["results"]) == 3  # top=3

    async def test_unknown_job_returns_404(self, tmp_path) -> None:
        app, _ = _build_app(tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            missing = await client.get("/api/optimize/does-not-exist")
            assert missing.status_code == 404

    async def test_invalid_metric_rejected_without_row(self, tmp_path) -> None:
        """非法入参（P2 校验）直接 400 且不登记僵尸任务。"""
        app, store = _build_app(tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/optimize", json=_optimize_body(metric="bogus")
            )
            assert resp.status_code == 400
            assert store._query_all("SELECT job_id FROM optimize_jobs") == []

    async def test_result_lost_after_restart(self, tmp_path) -> None:
        """已知边界（P2 内存结果 + P3 GET）：进程重启后 status 仍在库，但 result 丢失→null。

        这是设计上的优雅降级，专门用测试锁定该行为，防止后续误改语义。
        """
        db_path = tmp_path / "state.db"
        app1, store1 = _build_app(tmp_path, db_path=db_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app1), base_url="http://test"
        ) as client:
            job_id = (await client.post("/api/optimize", json=_optimize_body())).json()[
                "job_id"
            ]
            body1 = await _submit_and_poll(client, job_id)
            assert body1["status"] == "done"
            assert body1["result"] is not None  # 第一个进程内存里有结果

        store1.close()

        # 第二个进程（模拟重启）指向同一 DB，但内存结果字典是空的
        app2, store2 = _build_app(tmp_path, db_path=db_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2), base_url="http://test"
        ) as client:
            get = await client.get(f"/api/optimize/{job_id}")
            assert get.status_code == 200
            body2 = get.json()
            assert body2["status"] == "done"  # 库里状态仍在
            assert body2["completed"] == 4
            assert body2["result"] is None  # 重启后内存结果丢失
            assert body2["error"] is None
