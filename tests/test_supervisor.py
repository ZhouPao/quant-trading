"""M10-6 supervisor 启停命令：stop 不重启、start 拉起、审计与坏命令。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import fakeredis
import pytest

from quant.cli.supervisor import (
    CONTROL_AUDIT_KEY,
    CONTROL_KEY,
    ProcessSpec,
    Supervisor,
    _Child,
    restart_delay,
)


class _FakeProc:
    pid = 4321

    def __init__(self) -> None:
        self._code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        self._code = 0


def _supervisor() -> tuple[Supervisor, fakeredis.FakeRedis]:
    redis = fakeredis.FakeRedis(decode_responses=True)
    sup = Supervisor(
        [ProcessSpec("worker", ("true",))],
        Path("/tmp"),
        threading.Event(),
        redis_client=redis,
    )
    return sup, redis


def _enqueue(redis, **command) -> None:
    payload = {
        "command_id": "c1",
        "action": "stop",
        "name": "worker",
        "operator": "tester",
        "issued_at": 1000,
        **command,
    }
    redis.lpush(CONTROL_KEY, json.dumps(payload))


def _audit(redis) -> list[dict]:
    return [json.loads(item) for item in redis.lrange(CONTROL_AUDIT_KEY, 0, -1)]


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(0, 0.0), (1, 2.0), (2, 4.0), (3, 8.0), (10, 60.0)],
)
def test_restart_delay_backs_off(failures: int, expected: float) -> None:
    assert restart_delay(failures) == expected


def test_stop_terminates_and_prevents_restart() -> None:
    sup, redis = _supervisor()
    fake = _FakeProc()
    sup._children["worker"].proc = fake
    sup._children["worker"].started_at = time.monotonic()

    _enqueue(redis, action="stop")
    sup._poll_once(time.monotonic())

    assert fake.terminated is True
    assert sup._children["worker"].desired is False

    # 下一轮发现进程已退出：保持停止，不进入重启
    sup._poll_once(time.monotonic())

    assert sup._children["worker"].proc is None
    assert sup._children["worker"].desired is False
    assert _audit(redis)[0]["status"] == "accepted"
    assert _audit(redis)[0]["operator"] == "tester"


def test_start_spawns_stopped_process(monkeypatch) -> None:
    sup, redis = _supervisor()
    sup._children["worker"].proc = None
    sup._children["worker"].desired = False
    spawned = _FakeProc()
    monkeypatch.setattr(
        sup,
        "_spawn",
        lambda spec, failures: _Child(
            spec=spec, proc=spawned, failures=failures
        ),
    )

    _enqueue(redis, action="start")
    sup._poll_once(time.monotonic())

    assert sup._children["worker"].proc is spawned
    assert sup._children["worker"].desired is True
    assert _audit(redis)[0]["status"] == "accepted"


def test_unknown_process_is_rejected() -> None:
    sup, redis = _supervisor()

    _enqueue(redis, action="stop", name="nope")
    sup._poll_once(time.monotonic())

    entry = _audit(redis)[0]
    assert entry["status"] == "rejected"
    assert "unknown process" in entry["error"]


def test_unknown_action_is_rejected() -> None:
    sup, redis = _supervisor()

    _enqueue(redis, action="restart")
    sup._poll_once(time.monotonic())

    assert _audit(redis)[0]["status"] == "rejected"


def test_malformed_command_is_ignored_without_crash() -> None:
    sup, redis = _supervisor()
    redis.lpush(CONTROL_KEY, "{not-json")

    sup._poll_once(time.monotonic())

    assert _audit(redis) == []


def test_crash_still_restarts_when_desired() -> None:
    """正常崩溃（未被 stop）仍走指数退避重启，M10-6 不能破坏原有自愈。"""
    sup, _redis = _supervisor()
    fake = _FakeProc()
    fake._code = 1
    sup._children["worker"].proc = fake
    sup._children["worker"].started_at = time.monotonic()

    sup._poll_once(time.monotonic())

    child = sup._children["worker"]
    assert child.proc is None
    assert child.desired is True
    assert child.failures == 1
    assert child.next_start_at > 0
