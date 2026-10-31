"""三项对账差异的存储与看板接口（M8-18）。

对账算法本身在 `test_reconciler.py`；这里覆盖差异落库、分页筛选与
「标记已处理」的写守卫。
"""
from __future__ import annotations

import time

import fakeredis.aioredis
import httpx
import pytest

from quant.dashboard.app import create_app
from quant.dashboard.service import DashboardService
from quant.foundation.config import OKXSettings, Settings
from quant.state.sqlite_store import SQLiteStore

NOW = int(time.time() * 1000)
LOCAL_HEADERS = {"host": "127.0.0.1:8080", "X-Dashboard-Request": "1"}


@pytest.fixture
def env(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SQLiteStore(tmp_path / "quant.db")
    settings = Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True)
    )
    service = DashboardService(store, redis, settings)
    yield service, store, settings
    store.close()


def _seed(store: SQLiteStore, *, kind: str = "position", resolved: bool = False) -> str:
    return store.record_reconciliation(
        kind=kind,
        key="BTC-USDT" if kind == "position" else "total_equity",
        local_value="0.5",
        remote_value="0.2",
        diff="0.3",
        severity="manual",
        note="本地多于交易所",
        now_ms=NOW,
        resolved=resolved,
    )


async def test_list_returns_normalized_rows(env) -> None:
    service, store, _settings = env
    _seed(store)

    body = await service.reconciliations(only_open=True)

    assert body["degraded"] == []
    assert body["total"] == 1
    row = body["items"][0]
    assert row["kind"] == "position"
    assert row["key"] == "BTC-USDT"
    assert row["severity"] == "manual"
    assert row["resolved"] is False


async def test_route_filters_by_kind(env) -> None:
    service, store, _settings = env
    _seed(store, kind="position")
    _seed(store, kind="balance")
    app = create_app(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/reconciliations?kind=balance")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["kind"] == "balance"


async def test_route_only_open_filters_resolved(env) -> None:
    service, store, _settings = env
    _seed(store, resolved=True)
    app = create_app(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        all_rows = await client.get("/api/reconciliations")
        open_rows = await client.get("/api/reconciliations?only_open=true")

    assert all_rows.json()["total"] == 1
    assert open_rows.json()["total"] == 0


async def test_resolve_requires_guard(env) -> None:
    service, store, _settings = env
    rec_id = _seed(store)
    app = create_app(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post(
            f"/api/reconciliations/{rec_id}/resolve",
            headers={"host": "127.0.0.1:8080"},
        )
        ok = await client.post(
            f"/api/reconciliations/{rec_id}/resolve", headers=LOCAL_HEADERS
        )

    assert denied.status_code == 400
    assert ok.json()["result"] == "resolved"
    rows, _total = store.list_reconciliations(only_open=True)
    assert rows == []


async def test_resolve_is_idempotent(env) -> None:
    service, store, _settings = env
    rec_id = _seed(store)

    first = await service.resolve_reconciliation(rec_id)
    second = await service.resolve_reconciliation(rec_id)

    assert first["result"] == "resolved"
    assert second["result"] == "already_resolved"


async def test_open_discrepancy_is_upserted_not_duplicated(env) -> None:
    """同一差异每轮对账只刷新一行，不随轮询膨胀。"""
    _service, store, _settings = env
    for offset in (0, 60_000, 120_000):
        store.upsert_open_reconciliation(
            kind="balance",
            key="total_equity",
            local_value="10000",
            remote_value="9000",
            diff="1000",
            severity="manual",
            note=None,
            now_ms=NOW + offset,
        )

    rows, total = store.list_reconciliations(kind="balance")

    assert total == 1
    assert rows[0]["ts"] == NOW + 120_000


async def test_purge_keeps_open_discrepancies(env) -> None:
    """历史清理只删已处理的差异，未处理的是待办不能随过期消失。"""
    _service, store, _settings = env
    open_id = _seed(store, kind="position")
    resolved_id = _seed(store, kind="balance", resolved=True)

    store.purge_before(NOW + 1000)

    rows, _total = store.list_reconciliations()
    remaining = {row["rec_id"] for row in rows}
    assert open_id in remaining
    assert resolved_id not in remaining
