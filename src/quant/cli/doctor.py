"""`quant doctor`：部署自检（M10-7 / 3-3）。

检查项：配置文件可加载、SQLite 完整性/schema、Redis 连通、OKX 凭证是否配置、
SMTP 是否可用、本地 K 线新鲜度、产物目录可写、磁盘余量。

退出码：有 `fail` 返回 1；`--strict` 时 `warn` 也返回 1。
`--json` 输出机器可读结果（字段脱敏，不含密钥）。
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.storage.bar_storage import BarStorage

HOUR_MS = 3_600_000
WARN_AGE_MS = 48 * HOUR_MS
FAIL_AGE_MS = 7 * 24 * HOUR_MS
MIN_FREE_PCT = 10.0


@dataclass(frozen=True, slots=True)
class Check:
    """单项检查结果。"""

    name: str
    status: str  # ok / warn / fail
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        """没有 fail 即视为可用。"""
        return all(item.status != "fail" for item in self.checks)

    @property
    def strict_ok(self) -> bool:
        """全部 ok（warn 也算不合格）。"""
        return all(item.status == "ok" for item in self.checks)


def _safe_url(url: str) -> str:
    """脱敏 URL 里的密码：redis://:pass@host -> redis://***@host。"""
    return re.sub(r"://[^@/]*@", "://***@", url)


def _check_sqlite(settings: Settings) -> Check:
    path = Path(settings.state.db_path)
    if not path.is_file():
        return Check("sqlite", "fail", f"db not found: {path}")
    try:
        conn = sqlite3.connect(str(path))
        try:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as e:
        return Check("sqlite", "fail", f"open failed: {e}")
    if integrity != "ok":
        return Check("sqlite", "fail", f"quick_check: {integrity}")

    from quant.state.sqlite_store import SCHEMA_VERSION

    if version > SCHEMA_VERSION:
        return Check(
            "sqlite",
            "fail",
            f"schema v{version} > code v{SCHEMA_VERSION}，请先升级代码",
        )
    if version < SCHEMA_VERSION:
        return Check(
            "sqlite",
            "warn",
            f"schema v{version} < code v{SCHEMA_VERSION}，下次启动自动迁移",
        )
    return Check("sqlite", "ok", f"schema v{version}, quick_check ok")


def _check_redis(settings: Settings) -> Check:
    url = _safe_url(settings.redis.url)
    try:
        import redis
    except ImportError:
        return Check("redis", "warn", "redis package not installed")
    try:
        client = redis.Redis.from_url(settings.redis.url, socket_timeout=2)
        try:
            client.ping()
        finally:
            client.close()
    except Exception as e:  # noqa: BLE001 - 网络/认证失败都要展示原因
        return Check("redis", "fail", f"{url}: {e}")
    return Check("redis", "ok", url)


def _check_okx(settings: Settings) -> Check:
    missing = [
        name
        for name in ("api_key", "secret", "passphrase")
        if not getattr(settings.okx, name)
    ]
    if missing:
        return Check("okx", "fail", f"缺凭证：{', '.join(missing)}")
    if settings.okx.demo:
        return Check("okx", "ok", "凭证已配置，mode=demo")
    return Check("okx", "warn", "凭证已配置，mode=LIVE（实盘）")


def _check_smtp(settings: Settings) -> Check:
    smtp = settings.smtp
    if not smtp.host or not smtp.to_addrs:
        return Check("smtp", "warn", "未配置 host/to_addrs，告警降级为仅日志")
    if not smtp.username:
        return Check("smtp", "warn", f"{smtp.host}:{smtp.port} 未配置用户名")
    return Check(
        "smtp",
        "ok",
        f"{smtp.host}:{smtp.port} -> {len(smtp.to_addrs)} 个收件人",
    )


def _check_data(settings: Settings, now_ms: int) -> Check:
    inst = settings.engine.inst
    bar = settings.engine.bar
    try:
        bars = BarStorage(settings.storage.data_dir).load_bars(
            inst, bar, now_ms - 7 * 24 * HOUR_MS, now_ms
        )
    except Exception as e:  # noqa: BLE001 - 读盘失败只降级
        return Check("data", "warn", f"读取失败：{e}")
    if not bars:
        return Check("data", "warn", f"无本地 K 线：{inst} {bar}")
    age = max(0, now_ms - bars[-1].ts)
    if age > FAIL_AGE_MS:
        status = "fail"
    elif age > WARN_AGE_MS:
        status = "warn"
    else:
        status = "ok"
    return Check(
        "data", status, f"{inst} {bar} 最后一根 {age / HOUR_MS:.1f}h 前"
    )


def _check_disk(settings: Settings) -> Check:
    probe = Path(settings.storage.data_dir)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as e:
        return Check("disk", "warn", f"无法读取磁盘信息：{e}")
    free_pct = usage.free / usage.total * 100 if usage.total else 0.0
    if free_pct < 5:
        status = "fail"
    elif free_pct < MIN_FREE_PCT:
        status = "warn"
    else:
        status = "ok"
    return Check(
        "disk",
        status,
        f"{probe} 可用 {free_pct:.1f}%（{usage.free / 2**30:.1f} GiB）",
    )


def _check_artifacts(settings: Settings) -> Check:
    root = Path(settings.backtest.artifacts_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return Check("artifacts", "fail", f"{root}: {e}")
    return Check("artifacts", "ok", f"可写：{root}")


def run_doctor(
    settings: Settings,
    *,
    now_ms: int | None = None,
    skip_redis: bool = False,
) -> DoctorReport:
    """逐项自检，返回结构化报告（不抛异常，失败写进 Check）。"""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    checks = [_check_sqlite(settings)]
    if not skip_redis:
        checks.append(_check_redis(settings))
    checks.extend(
        [
            _check_okx(settings),
            _check_smtp(settings),
            _check_data(settings, now),
            _check_disk(settings),
            _check_artifacts(settings),
        ]
    )
    return DoctorReport(checks)


def main(args) -> int:
    """执行自检，返回退出码。"""
    try:
        settings = Settings.load(args.config)
    except Exception as e:  # noqa: BLE001 - 配置坏了也要给出可读结论
        print(f"[FAIL] config     {e}")
        return 1
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.doctor")

    report = run_doctor(settings, skip_redis=args.skip_redis)
    if args.json:
        print(
            json.dumps(
                {"ok": report.ok, "checks": [asdict(c) for c in report.checks]},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for item in report.checks:
            print(f"[{item.status.upper():<4}] {item.name:<10} {item.detail}")
        print(
            "结论："
            + ("可用" if report.ok else "存在 fail 项，请先处理")
            + ("；有 warn 项" if not report.strict_ok and report.ok else "")
        )

    if not report.ok:
        return 1
    if args.strict and not report.strict_ok:
        log.warning("doctor strict: 存在 warn 项")
        return 1
    return 0
