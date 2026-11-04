"""supervisor 命令：一条命令拉起全部常驻进程，崩溃自动重启。

用法：
  quant supervisor                       # candles + webhook + worker + engine + monitor + dashboard
  quant supervisor --strategy sma_cross --broker okx --td-mode cash --paper-mode
  quant supervisor --no-dashboard        # 不启动看板
  quant supervisor --dry-run             # 只打印将要执行的命令

行为：
- 子进程各自输出写到 `logs/<name>.log`（追加），共享日志文件仍是
  `config.logging.file`；
- 任一子进程退出后自动重启，连续快速失败按指数退避（2s 起，最多 60s）；
  稳定运行满 60 秒后退避清零；
- Ctrl+C / SIGTERM 时向所有子进程发 SIGTERM，超时未退出则 SIGKILL。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from redis import Redis as SyncRedis

from quant.foundation.config import Settings
from quant.foundation.control import CONTROL_AUDIT_KEY, CONTROL_KEY
from quant.foundation.heartbeat import SUPERVISOR_KEY, SUPERVISOR_TTL
from quant.foundation.logger import get_logger, setup_logger

POLL_INTERVAL_SECONDS = 1.0
STABLE_UPTIME_SECONDS = 60.0
RESTART_BASE_SECONDS = 2.0
RESTART_MAX_SECONDS = 60.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0




@dataclass(frozen=True)
class ProcessSpec:
    """一个常驻子进程的启动描述。"""

    name: str
    argv: tuple[str, ...]


@dataclass
class _Child:
    """子进程运行状态；proc 为 None 表示等待下次重启。"""

    spec: ProcessSpec
    proc: subprocess.Popen | None
    started_at: float = 0.0
    failures: int = 0
    next_start_at: float = 0.0
    #: 用户期望状态：False 表示被主动 stop，退出后不再自动重启（M10-6）
    desired: bool = True


def restart_delay(
    failures: int,
    base: float = RESTART_BASE_SECONDS,
    cap: float = RESTART_MAX_SECONDS,
) -> float:
    """连续失败次数 -> 重启等待秒数（指数退避，封顶 cap）。"""
    if failures <= 0:
        return 0.0
    return min(base * (2 ** (failures - 1)), cap)


def build_specs(args, python: str | None = None) -> list[ProcessSpec]:
    """把 CLI 参数翻译成各子进程的启动命令。"""
    py = python or sys.executable
    cfg = args.config
    specs = [
        ProcessSpec(
            "candles",
            (py, "-m", "quant", "sync-candles", "--loop", "--config", cfg),
        ),
        ProcessSpec("webhook", (py, "-m", "quant", "serve-webhook", "--config", cfg)),
        ProcessSpec(
            "worker",
            (
                py, "-m", "quant", "run-worker",
                "--strategy", args.strategy,
                "--broker", args.broker,
                "--td-mode", args.td_mode,
                *(["--paper-mode"] if args.paper_mode else []),
                *(["--ws"] if args.ws else []),
                "--config", cfg,
            ),
        ),
        ProcessSpec(
            "engine",
            (
                py, "-m", "quant", "run-engine",
                "--strategy", args.strategy,
                "--config", cfg,
            ),
        ),
        ProcessSpec("monitor", (py, "-m", "quant", "monitor", "--config", cfg)),
    ]
    if not args.no_dashboard:
        specs.append(
            ProcessSpec("dashboard", (py, "-m", "quant", "dashboard", "--config", cfg))
        )
    return specs


class Supervisor:
    """轮询子进程状态并在退出后重启。"""

    def __init__(
        self,
        specs: list[ProcessSpec],
        log_dir: Path,
        stop_event: threading.Event,
        cwd: Path | None = None,
        shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
        redis_client: SyncRedis | None = None,
    ) -> None:
        self._children = {
            spec.name: _Child(spec=spec, proc=None) for spec in specs
        }
        self._log_dir = log_dir
        self._stop = stop_event
        self._cwd = cwd or Path.cwd()
        self._shutdown_timeout = shutdown_timeout
        # 进程表写 Redis，供看板 /api/processes 读取；为 None 时静默跳过
        self._redis = redis_client
        self._log = get_logger("cli.supervisor")

    def run(self) -> int:
        for name in list(self._children):
            self._children[name] = self._spawn(self._children[name].spec, failures=0)
        self._publish_process_table()
        self._log.info(
            f"supervisor started processes={','.join(self._children)} "
            f"log_dir={self._log_dir}"
        )
        try:
            while not self._stop.wait(POLL_INTERVAL_SECONDS):
                self._poll_once(time.monotonic())
        finally:
            self._stop_all()
        return 0

    # internal

    def _poll_once(self, now: float) -> None:
        # 先消费看板下发的启停命令，再按最新期望状态推断是否重启
        self._drain_commands()
        for name, child in list(self._children.items()):
            if child.proc is None:
                if child.desired and now >= child.next_start_at:
                    self._children[name] = self._spawn(child.spec, child.failures)
                continue
            code = child.proc.poll()
            if code is None:
                continue
            if not child.desired:
                # 主动停掉的子进程：正常退出后保持停止，不进入重启退避
                self._log.info(f"process stopped name={name} code={code}")
                self._children[name] = _Child(
                    spec=child.spec, proc=None, desired=False
                )
                continue
            uptime = now - child.started_at
            failures = 0 if uptime >= STABLE_UPTIME_SECONDS else child.failures + 1
            delay = restart_delay(failures)
            self._log.error(
                f"process exited name={name} code={code} uptime={uptime:.0f}s "
                f"failures={failures} restart_in={delay:.0f}s"
            )
            self._children[name] = _Child(
                spec=child.spec,
                proc=None,
                failures=failures,
                next_start_at=now + delay,
            )
        self._publish_process_table()

    def _drain_commands(self) -> None:
        """消费看板下发的 stop/start 命令；格式非法只告警不中断轮询。"""
        if self._redis is None:
            return
        while True:
            try:
                raw = self._redis.lpop(CONTROL_KEY)
            except Exception as e:  # noqa: BLE001
                self._log.warning(f"read control command failed: {e}")
                return
            if raw is None:
                return
            try:
                command = json.loads(raw)
            except (TypeError, ValueError):
                self._log.warning(f"ignore malformed control command: {raw!r}")
                continue
            self._apply_command(command)

    def _apply_command(self, command: dict) -> None:
        name = str(command.get("name", ""))
        action = str(command.get("action", ""))
        child = self._children.get(name)
        if child is None:
            self._record_command(command, "rejected", f"unknown process {name!r}")
            return
        if action == "stop":
            if child.proc is not None and child.proc.poll() is None:
                child.proc.terminate()
            child.desired = False
            self._children[name] = child
            self._record_command(command, "accepted", "")
            return
        if action == "start":
            child.desired = True
            if child.proc is None or child.proc.poll() is not None:
                self._children[name] = self._spawn(child.spec, failures=0)
            else:
                self._children[name] = child
            self._record_command(command, "accepted", "")
            return
        self._record_command(command, "rejected", f"unknown action {action!r}")

    def _record_command(
        self, command: dict, status: str, error: str
    ) -> None:
        """把命令处理结果写审计队列（定长 1000，含操作人与时间）。"""
        if self._redis is None:
            return
        entry = {
            "command_id": command.get("command_id"),
            "action": command.get("action"),
            "name": command.get("name"),
            "operator": command.get("operator", ""),
            "issued_at": command.get("issued_at"),
            "status": status,
            "error": error,
            "ts": int(time.time() * 1000),
        }
        try:
            self._redis.lpush(
                CONTROL_AUDIT_KEY, json.dumps(entry, ensure_ascii=False)
            )
            self._redis.ltrim(CONTROL_AUDIT_KEY, 0, 999)
        except Exception as e:  # noqa: BLE001
            self._log.warning(f"record control command failed: {e}")

    def _publish_process_table(self) -> None:
        """把子系统 pid/重启次数写到 Redis（TTL 15s）。

        这张表本身就是 supervisor 的判活信号：它挂了键就过期，不需要再
        单独写一个心跳键——同一进程写两个键只会让「哪个才是真的」变成
        一个额外问题。
        """
        if self._redis is None:
            return
        now_ms = int(time.time() * 1000)
        children: dict[str, dict[str, object]] = {}
        for name, child in self._children.items():
            proc = child.proc
            children[name] = {
                "pid": proc.pid if proc else None,
                "started_at": (
                    int(now_ms - (time.monotonic() - child.started_at) * 1000)
                    if proc
                    else None
                ),
                "restarts": child.failures,
                "last_exit": proc.poll() if proc else None,
                # false = 被用户主动 stop（看板显示「已停止」而非「离线」）
                "desired": child.desired,
            }
        payload = {"ts": now_ms, "pid": os.getpid(), "children": children}
        try:
            self._redis.set(
                SUPERVISOR_KEY,
                json.dumps(payload, ensure_ascii=False),
                ex=SUPERVISOR_TTL,
            )
        except Exception as e:  # noqa: BLE001
            self._log.warning(f"publish process table failed: {e}")

    def _spawn(self, spec: ProcessSpec, failures: int) -> _Child:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / f"{spec.name}.log"
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        with open(log_path, "ab", buffering=0) as out:
            proc = subprocess.Popen(
                list(spec.argv),
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                cwd=self._cwd,
            )
        self._log.info(f"process started name={spec.name} pid={proc.pid} log={log_path}")
        return _Child(spec=spec, proc=proc, started_at=time.monotonic(), failures=failures)

    def _stop_all(self) -> None:
        alive = [c for c in self._children.values() if c.proc and c.proc.poll() is None]
        if not alive:
            return
        self._log.info(f"stopping children count={len(alive)}")
        for child in alive:
            try:
                child.proc.terminate()
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + self._shutdown_timeout
        while time.monotonic() < deadline:
            if all(c.proc.poll() is not None for c in alive):
                return
            time.sleep(0.1)
        for child in alive:
            if child.proc.poll() is None:
                self._log.warning(f"process kill timeout name={child.spec.name}")
                try:
                    child.proc.kill()
                except ProcessLookupError:
                    pass


def main(args) -> int:
    """前台运行 supervisor（阻塞直到 SIGINT/SIGTERM）。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.supervisor")
    specs = build_specs(args)

    if args.dry_run:
        for spec in specs:
            print(f"{spec.name}: {' '.join(spec.argv)}")
        return 0

    stop = threading.Event()

    def _handle(signum, _frame) -> None:
        log.info(f"received signal {signum}, shutting down")
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle)

    log_dir = Path(settings.logging.file).parent
    redis_client = _connect_redis(settings, log)
    try:
        return Supervisor(
            specs, log_dir, stop, redis_client=redis_client
        ).run()
    finally:
        if redis_client is not None:
            try:
                redis_client.close()
            except Exception as e:  # noqa: BLE001
                log.warning(f"close redis failed: {e}")


def _connect_redis(settings: Settings, log) -> SyncRedis | None:
    """进程表写不进去不该阻塞托管；连不上就退化为不发布。"""
    try:
        client = SyncRedis.from_url(settings.redis.url, decode_responses=True)
        client.ping()
        return client
    except Exception as e:  # noqa: BLE001
        log.warning(f"redis unavailable, process table disabled: {e}")
        return None
