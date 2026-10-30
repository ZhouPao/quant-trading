"""supervisor 单元测试。

覆盖：
- build_parser 的 supervisor 子命令默认值/覆盖值
- build_specs 生成的子进程命令（是否含 dashboard、worker 参数透传）
- restart_delay 指数退避与封顶
- Supervisor._poll_once 的重启调度与稳定运行后退避清零
- Supervisor._stop_all 的优雅终止

不真正 spawn 子进程：用假的 Popen 对象替代。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from quant.cli import build_parser
from quant.cli.supervisor import (
    RESTART_MAX_SECONDS,
    SUPERVISOR_KEY,
    SUPERVISOR_TTL,
    ProcessSpec,
    Supervisor,
    _Child,
    build_specs,
    restart_delay,
)


class _FakeProc:
    """最小 Popen 替身。"""

    def __init__(self, code: int | None = None) -> None:
        self._code = code
        self.pid = 4321
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        self._code = 0

    def kill(self) -> None:
        self.killed = True
        self._code = -9


def _args(argv: list[str]):
    return build_parser().parse_args(["supervisor", *argv])


class TestParser:
    def test_defaults(self) -> None:
        args = _args([])
        assert args.command == "supervisor"
        assert args.strategy == "sma_cross"
        assert args.broker == "okx"
        assert args.td_mode == "cash"
        assert args.paper_mode is False
        assert args.ws is False
        assert args.no_dashboard is False
        assert args.dry_run is False
        assert args.config == "config/config.toml"

    def test_overrides(self) -> None:
        args = _args(
            [
                "--strategy", "rsi",
                "--broker", "mock",
                "--td-mode", "cross",
                "--paper-mode",
                "--ws",
                "--no-dashboard",
                "--dry-run",
            ]
        )
        assert args.strategy == "rsi"
        assert args.broker == "mock"
        assert args.td_mode == "cross"
        assert args.paper_mode is True
        assert args.ws is True
        assert args.no_dashboard is True
        assert args.dry_run is True


class TestBuildSpecs:
    def test_four_processes_by_default(self) -> None:
        specs = build_specs(_args([]), python="/usr/bin/python3")
        assert [s.name for s in specs] == [
            "candles", "webhook", "worker", "engine", "monitor", "dashboard"
        ]
        for spec in specs:
            assert spec.argv[0] == "/usr/bin/python3"
            assert spec.argv[1:3] == ("-m", "quant")

    def test_candles_runs_in_loop(self) -> None:
        candles = next(
            s for s in build_specs(_args([]), python="py") if s.name == "candles"
        )
        assert "sync-candles" in candles.argv
        assert "--loop" in candles.argv

    def test_worker_flags_passthrough(self) -> None:
        args = _args(
            [
                "--strategy", "rsi",
                "--broker", "mock",
                "--td-mode", "cross",
                "--paper-mode",
                "--ws",
            ]
        )
        worker = next(s for s in build_specs(args, python="py") if s.name == "worker")
        argv = list(worker.argv)
        assert argv[3:5] == ["run-worker", "--strategy"]
        assert "rsi" in argv
        assert "--broker" in argv and "mock" in argv
        assert "--td-mode" in argv and "cross" in argv
        assert "--paper-mode" in argv
        assert "--ws" in argv

    def test_worker_optional_flags_absent(self) -> None:
        worker = next(
            s for s in build_specs(_args([]), python="py") if s.name == "worker"
        )
        assert "--paper-mode" not in worker.argv
        assert "--ws" not in worker.argv

    def test_no_dashboard(self) -> None:
        specs = build_specs(_args(["--no-dashboard"]), python="py")
        assert [s.name for s in specs] == [
            "candles", "webhook", "worker", "engine", "monitor"
        ]

    def test_config_passed_to_all(self) -> None:
        specs = build_specs(_args(["--config", "/tmp/x.toml"]), python="py")
        for spec in specs:
            argv = list(spec.argv)
            i = argv.index("--config")
            assert argv[i + 1] == "/tmp/x.toml"


class TestRestartDelay:
    def test_zero_failures(self) -> None:
        assert restart_delay(0) == 0.0

    def test_exponential_and_capped(self) -> None:
        assert restart_delay(1) == 2.0
        assert restart_delay(2) == 4.0
        assert restart_delay(3) == 8.0
        assert restart_delay(10) == RESTART_MAX_SECONDS


class TestPollOnce:
    def _supervisor(self, tmp_path: Path) -> Supervisor:
        return Supervisor(
            [ProcessSpec("webhook", ("py", "-m", "quant", "serve-webhook"))],
            log_dir=tmp_path,
            stop_event=threading.Event(),
        )

    def test_crash_schedules_restart(self, tmp_path: Path) -> None:
        sup = self._supervisor(tmp_path)
        sup._children["webhook"] = _Child(
            spec=sup._children["webhook"].spec,
            proc=_FakeProc(code=1),
            started_at=10.0,
        )
        sup._poll_once(now=15.0)
        child = sup._children["webhook"]
        assert child.proc is None
        assert child.failures == 1
        assert child.next_start_at == 17.0  # 15 + restart_delay(1)=2

    def test_running_process_untouched(self, tmp_path: Path) -> None:
        sup = self._supervisor(tmp_path)
        proc = _FakeProc(code=None)
        sup._children["webhook"] = _Child(
            spec=sup._children["webhook"].spec, proc=proc, started_at=0.0
        )
        sup._poll_once(now=5.0)
        assert sup._children["webhook"].proc is proc

    def test_long_uptime_resets_backoff(self, tmp_path: Path) -> None:
        sup = self._supervisor(tmp_path)
        sup._children["webhook"] = _Child(
            spec=sup._children["webhook"].spec,
            proc=_FakeProc(code=1),
            started_at=0.0,
            failures=5,
        )
        sup._poll_once(now=120.0)  # uptime >= 60s，视为稳定运行后再次崩溃
        child = sup._children["webhook"]
        assert child.failures == 0
        assert child.next_start_at == 120.0


class TestStopAll:
    def test_terminates_children(self, tmp_path: Path) -> None:
        sup = Supervisor(
            [ProcessSpec("webhook", ("py",))],
            log_dir=tmp_path,
            stop_event=threading.Event(),
            shutdown_timeout=1.0,
        )
        proc = _FakeProc(code=None)
        sup._children["webhook"] = _Child(spec=sup._children["webhook"].spec, proc=proc)
        sup._stop_all()
        assert proc.terminated is True
        assert proc.killed is False

    def test_noop_when_already_exited(self, tmp_path: Path) -> None:
        sup = Supervisor(
            [ProcessSpec("webhook", ("py",))],
            log_dir=tmp_path,
            stop_event=threading.Event(),
        )
        proc = _FakeProc(code=0)
        sup._children["webhook"] = _Child(spec=sup._children["webhook"].spec, proc=proc)
        sup._stop_all()
        assert proc.terminated is False


class TestPublishProcessTable:
    """进程表写 Redis，作为 supervisor 自己的判活信号（MS4-A §7.2）。"""

    def _supervisor(self, tmp_path: Path, redis_client) -> Supervisor:
        return Supervisor(
            [ProcessSpec("webhook", ("py",))],
            log_dir=tmp_path,
            stop_event=threading.Event(),
            redis_client=redis_client,
        )

    def test_writes_table_with_ttl(self, tmp_path: Path) -> None:
        fake = _FakeSyncRedis()
        sup = self._supervisor(tmp_path, fake)
        sup._children["webhook"] = _Child(
            spec=sup._children["webhook"].spec,
            proc=_FakeProc(code=None),
            started_at=time.monotonic() - 10,
            failures=3,
        )

        sup._publish_process_table()

        key, raw, ex = fake.calls[0]
        assert key == SUPERVISOR_KEY
        assert ex == SUPERVISOR_TTL
        payload = json.loads(raw)
        assert payload["pid"] == os.getpid()
        child = payload["children"]["webhook"]
        assert child["pid"] == 4321
        assert child["restarts"] == 3
        assert child["last_exit"] is None
        assert child["started_at"] <= payload["ts"]

    def test_skips_without_redis(self, tmp_path: Path) -> None:
        sup = self._supervisor(tmp_path, None)
        sup._publish_process_table()  # 不应抛出

    def test_redis_failure_is_logged_not_raised(self, tmp_path: Path) -> None:
        sup = self._supervisor(tmp_path, _BrokenSyncRedis())
        sup._publish_process_table()  # 写不进去不影响托管


class _FakeSyncRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | None]] = []

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.calls.append((key, value, ex))


class _BrokenSyncRedis:
    def set(self, *_args, **_kwargs) -> None:
        raise RuntimeError("redis down")
