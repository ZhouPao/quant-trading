"""AlertService 测试（MS4-A §5）：落库 / 静音 / 状态机 / 降级。"""
from __future__ import annotations

import json
import time

import fakeredis.aioredis
import pytest

from quant.foundation.config import MonitorSettings
from quant.monitor.alert_service import (
    AlertEvent,
    AlertService,
    build_alert_id,
    fingerprint,
)
from quant.monitor.alerter import EmailAlerter
from quant.state.sqlite_store import SQLiteStore


class RecordingAlerter(EmailAlerter):
    """记录发送内容、不真的发信。"""

    def __init__(self) -> None:
        super().__init__(settings=_smtp())
        self.sent: list[tuple[str, str]] = []

    def enabled(self) -> bool:
        return True

    def send(self, subject: str, body: str) -> bool:
        self.sent.append((subject, body))
        return True


def _smtp():
    from quant.foundation.config import SmtpSettings

    return SmtpSettings(host="smtp.example.com", username="u", to_addrs=["a@b.c"])


def _event(rule: str = "margin_ratio", level: str = "ERROR", ts: int = 1) -> AlertEvent:
    return AlertEvent(rule=rule, level=level, message="保证金率过低", ts=ts)


@pytest.fixture
def env(tmp_path):
    store = SQLiteStore(tmp_path / "quant.db")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    alerter = RecordingAlerter()
    settings = MonitorSettings()
    service = AlertService(store, redis, alerter, settings)
    yield service, store, redis, alerter, settings
    store.close()


class TestPublish:
    async def test_persists_and_caches(self, env) -> None:
        service, store, redis, alerter, settings = env

        alert_id = await service.publish(_event())

        rows, total = store.list_alerts(10)
        assert total == 1
        assert rows[0]["alert_id"] == alert_id
        assert rows[0]["state"] == "open"
        assert rows[0]["suppressed"] == 0
        # Redis 缓存仍在（看板首屏）
        assert await redis.llen(settings.dashboard_alerts_key) == 1
        assert len(alerter.sent) == 1

    async def test_same_event_is_idempotent(self, env) -> None:
        service, store, _redis, _alerter, _settings = env
        event = _event()

        first = await service.publish(event)
        second = await service.publish(event)

        assert first == second
        assert store.list_alerts(10)[1] == 1

    async def test_info_level_is_not_emailed(self, env) -> None:
        service, _store, _redis, alerter, _settings = env

        await service.publish(_event(level="INFO"))

        assert alerter.sent == []

    async def test_persist_failure_does_not_raise(self, env) -> None:
        service, store, redis, _alerter, settings = env
        store.insert_alert = _boom

        alert_id = await service.publish(_event())

        assert alert_id
        assert await redis.llen(settings.dashboard_alerts_key) == 1

    async def test_persist_disabled_skips_sqlite(self, env) -> None:
        service, store, redis, _alerter, settings = env
        settings.alerts_persist = False

        await service.publish(_event())

        assert store.list_alerts(10)[1] == 0
        assert await redis.llen(settings.dashboard_alerts_key) == 1

    async def test_redis_failure_does_not_block_persist(self, env) -> None:
        _service, store, redis, _alerter, _settings = env
        service = AlertService(
            store, _BrokenRedis(), RecordingAlerter(), MonitorSettings()
        )

        await service.publish(_event())

        assert store.list_alerts(10)[1] == 1


class TestSilence:
    async def test_suppresses_warning_but_not_info(self, env) -> None:
        service, store, _redis, alerter, _settings = env
        await service.silence(
            "margin_ratio", inst_id=None, hours=2, reason="维护", by="owner"
        )

        await service.publish(_event(ts=10))
        await service.publish(_event(level="INFO", ts=11))

        rows, _ = store.list_alerts(10)
        by_level = {r["level"]: r for r in rows}
        assert by_level["ERROR"]["suppressed"] == 1
        assert by_level["INFO"]["suppressed"] == 0
        # 被静音的 ERROR 不发邮件，INFO 本来也不发
        assert alerter.sent == []

    async def test_silence_scoped_to_inst(self, env) -> None:
        service, store, _redis, _alerter, _settings = env
        await service.silence(
            "position_drift", inst_id="ETH-USDT", hours=1, reason="", by="owner"
        )

        await service.publish(
            AlertEvent("position_drift", "ERROR", "eth", 1, inst_id="ETH-USDT")
        )
        await service.publish(
            AlertEvent("position_drift", "ERROR", "btc", 2, inst_id="BTC-USDT")
        )

        rows, _ = store.list_alerts(10)
        suppressed = {r["message"]: r["suppressed"] for r in rows}
        assert suppressed == {"eth": 1, "btc": 0}

    async def test_other_rule_not_suppressed(self, env) -> None:
        service, store, _redis, _alerter, _settings = env
        await service.silence(
            "margin_ratio", inst_id=None, hours=1, reason="", by="owner"
        )

        await service.publish(_event(rule="queue_backlog"))

        rows, _ = store.list_alerts(10)
        assert rows[0]["suppressed"] == 0

    async def test_active_silences_excludes_expired_and_revoked(self, env) -> None:
        service, store, _redis, _alerter, _settings = env
        now = int(time.time() * 1000)
        store.insert_silence(
            "s-old", "margin_ratio", inst_id=None,
            until_ts=now - 1000, reason="", created_by="owner", created_at=now,
        )
        live = await service.silence(
            "queue_backlog", inst_id=None, hours=1, reason="", by="owner"
        )
        revoked = await service.silence(
            "dead_letter", inst_id=None, hours=1, reason="", by="owner"
        )
        assert await service.revoke_silence(revoked)

        active = service.active_silences(now)

        assert [s["silence_id"] for s in active] == [live]

    async def test_revoke_twice_returns_false(self, env) -> None:
        service, _store, _redis, _alerter, _settings = env
        sid = await service.silence(
            "margin_ratio", inst_id=None, hours=1, reason="", by="owner"
        )

        assert await service.revoke_silence(sid)
        assert not await service.revoke_silence(sid)


class TestAckAndResolve:
    async def test_ack_records_operator_and_is_idempotent(self, env) -> None:
        service, store, _redis, _alerter, _settings = env
        alert_id = await service.publish(_event())

        assert await service.ack(alert_id, "alice")
        first = store.list_alerts(10)[0][0]

        assert await service.ack(alert_id, "bob")
        second = store.list_alerts(10)[0][0]

        assert first["state"] == "acked"
        assert first["acked_by"] == "alice"
        # 第二次不覆盖首次确认信息
        assert second["acked_by"] == "alice"
        assert second["acked_at"] == first["acked_at"]

    async def test_ack_unknown_id(self, env) -> None:
        service, _store, _redis, _alerter, _settings = env
        assert not await service.ack("nope", "alice")

    async def test_resolve_closes_open_and_acked(self, env) -> None:
        service, store, _redis, _alerter, _settings = env
        await service.publish(_event(ts=1))
        second = await service.publish(_event(ts=2))
        await service.ack(second, "alice")

        count = await service.resolve("margin_ratio", inst_id=None)

        assert count == 2
        rows, _ = store.list_alerts(10)
        assert {r["state"] for r in rows} == {"resolved"}
        assert all(r["resolved_at"] for r in rows)

    async def test_resolve_only_matching_rule(self, env) -> None:
        service, store, _redis, _alerter, _settings = env
        await service.publish(_event(rule="margin_ratio"))
        await service.publish(_event(rule="queue_backlog"))

        assert await service.resolve("margin_ratio", inst_id=None) == 1

        states = {r["rule"]: r["state"] for r in store.list_alerts(10)[0]}
        assert states == {"margin_ratio": "resolved", "queue_backlog": "open"}


class TestHelpers:
    def test_alert_id_depends_on_ts(self) -> None:
        fp = fingerprint("margin_ratio", "msg")
        assert build_alert_id("margin_ratio", fp, 1) != build_alert_id(
            "margin_ratio", fp, 2
        )

    def test_fingerprint_depends_on_message(self) -> None:
        assert fingerprint("r", "a") != fingerprint("r", "b")


class _BrokenRedis:
    async def lpush(self, *_args, **_kwargs):
        raise RuntimeError("redis down")

    async def ltrim(self, *_args, **_kwargs):
        raise RuntimeError("redis down")


def _boom(*_args, **_kwargs):
    raise RuntimeError("disk full")


async def test_cache_payload_is_json(env) -> None:
    """Redis 缓存必须是 JSON（看板直接 json.loads）。"""
    service, _store, redis, _alerter, settings = env

    await service.publish(_event())

    raw = await redis.lindex(settings.dashboard_alerts_key, 0)
    payload = json.loads(raw)
    assert payload["rule"] == "margin_ratio"
    assert payload["inst_id"] is None
