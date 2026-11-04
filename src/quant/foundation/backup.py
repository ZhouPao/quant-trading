"""SQLite 状态库 + 配置快照的本地备份（M10-7 / MS6）。

用 `sqlite3.Connection.backup` 做**在线备份**：WAL 模式下即使 worker 正在写入，
也能拿到一致快照；直接 copy 文件可能复制到半个事务，恢复后库损坏。

输出结构（`<data_dir>/backups/YYYYMMDD-HHMMSS/`）：

- `quant.db`        SQLite 快照
- `<config>.toml`   配置快照（存在才复制，不打包密钥）
- `manifest.json`   schema 版本、文件大小与 sha256，供 restore 校验
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from quant.foundation.config import Settings
from quant.foundation.logger import get_logger

log = get_logger("foundation.backup")


class BackupError(Exception):
    """备份失败（源库不存在、目标不可写等），消息可直接展示给用户。"""


@dataclass(frozen=True, slots=True)
class BackupResult:
    """一次成功备份的产物。"""

    dir: Path
    database: Path
    manifest: Path
    files: list[dict[str, object]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stamp(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime(
        "%Y%m%d-%H%M%S"
    )


def create_backup(
    settings: Settings,
    *,
    out_dir: Path | str | None = None,
    config_path: Path | str | None = None,
    now_ms: int | None = None,
) -> BackupResult:
    """把状态库与配置快照写入带时间戳的目录。

    Args:
        settings: 生效配置（读 `state.db_path`）
        out_dir: 输出根目录；默认 `<db 所在目录>/backups`
        config_path: 额外快照的配置文件；不存在则跳过
        now_ms: 覆盖时间戳（测试用）

    Raises:
        BackupError: 源库不存在或备份过程失败
    """
    db_path = Path(settings.state.db_path)
    if not db_path.is_file():
        raise BackupError(f"state db not found: {db_path}")

    now = int(time.time() * 1000) if now_ms is None else now_ms
    root = Path(out_dir) if out_dir else db_path.parent / "backups"
    target = root / _stamp(now)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BackupError(f"cannot create backup dir {target}: {e}") from e

    db_target = target / "quant.db"
    try:
        source = sqlite3.connect(str(db_path))
        try:
            schema_version = int(
                source.execute("PRAGMA user_version").fetchone()[0]
            )
            dest = sqlite3.connect(str(db_target))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
    except sqlite3.Error as e:
        raise BackupError(f"sqlite backup failed: {e}") from e

    files: list[dict[str, object]] = []

    def record(path: Path, kind: str) -> None:
        files.append(
            {
                "kind": kind,
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    record(db_target, "state")

    # 配置快照：只复制显式指定的文件，不递归目录（避免把密钥一起打包）
    cfg = Path(config_path) if config_path else None
    if cfg is not None and cfg.is_file():
        cfg_target = target / cfg.name
        shutil.copy2(cfg, cfg_target)
        record(cfg_target, "config")

    manifest_path = target / "manifest.json"
    manifest = {
        "created_at": now,
        "schema_version": schema_version,
        "db_path": str(db_path),
        "files": files,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"backup created dir={target} files={len(files)}")
    return BackupResult(
        dir=target,
        database=db_target,
        manifest=manifest_path,
        files=files,
    )


__all__ = ["BackupError", "BackupResult", "create_backup"]
