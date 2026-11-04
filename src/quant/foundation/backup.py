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


class RestoreError(Exception):
    """恢复失败（清单缺失/校验不一致/目标库忙），消息可直接展示。"""


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """一次恢复（或 dry-run 校验）的结果。"""

    backup_dir: Path
    target: Path
    dry_run: bool
    pre_backup: Path | None
    schema_version: int


def _read_manifest(backup_dir: Path) -> dict:
    """读取并校验 `manifest.json`：文件必须存在，size 与 sha256 必须一致。"""
    manifest = backup_dir / "manifest.json"
    if not manifest.is_file():
        raise RestoreError(f"manifest not found: {manifest}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RestoreError(f"manifest unreadable: {e}") from e
    for item in data.get("files") or []:
        path = backup_dir / str(item.get("name", ""))
        if not path.is_file():
            raise RestoreError(f"backup file missing: {path}")
        size = path.stat().st_size
        if size != item.get("size"):
            raise RestoreError(
                f"size mismatch for {path.name}: {size} != {item.get('size')}"
            )
        digest = _sha256(path)
        if digest != item.get("sha256"):
            raise RestoreError(f"sha256 mismatch for {path.name}")
    return data


def _schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def restore_backup(
    settings: Settings,
    backup_dir: Path | str,
    *,
    force: bool = False,
    dry_run: bool = False,
    now_ms: int | None = None,
) -> RestoreResult:
    """从备份目录恢复 SQLite 状态库。

    恢复顺序：校验清单 → 版本检查 → 目标库忙检查 → **先自动备份当前库** →
    用 `sqlite3.backup` 在线回写。

    Args:
        backup_dir: 由 `quant backup` 生成的目录
        force: 目标库存在未合并 WAL 时仍然恢复（确认已停进程后使用）
        dry_run: 只校验清单与版本，不写任何文件

    Raises:
        RestoreError: 清单缺失/校验不一致/备份版本高于当前/目标库忙
    """
    backup = Path(backup_dir)
    if not backup.is_dir():
        raise RestoreError(f"backup dir not found: {backup}")

    # 只做校验，不消费清单内容
    _read_manifest(backup)
    db_backup = backup / "quant.db"
    if not db_backup.is_file():
        raise RestoreError("backup does not contain quant.db")

    backup_version = _schema_version(db_backup)
    # 延迟导入：foundation 不反向依赖 state（避免循环）
    from quant.state.sqlite_store import SCHEMA_VERSION

    if backup_version > SCHEMA_VERSION:
        raise RestoreError(
            f"backup schema v{backup_version} 高于当前代码 v{SCHEMA_VERSION}，"
            "请先升级代码再恢复（降级恢复有数据风险）"
        )

    target = Path(settings.state.db_path)
    if dry_run:
        return RestoreResult(
            backup_dir=backup,
            target=target,
            dry_run=True,
            pre_backup=None,
            schema_version=backup_version,
        )

    # WAL 非空说明还有进程在写：此时覆盖会有数据丢失/损坏风险
    wal = target.with_name(f"{target.name}-wal")
    if wal.exists() and wal.stat().st_size > 0 and not force:
        raise RestoreError(
            f"{wal} 非空，可能有进程正在写入；请先停止 dashboard/worker，"
            "确认可覆盖时再加 --force"
        )

    pre: Path | None = None
    if target.is_file():
        # 恢复前先备份当前状态，恢复出错还能退回
        pre = create_backup(
            settings,
            out_dir=target.parent / "backups" / "pre-restore",
            now_ms=now_ms,
        ).dir

    try:
        source = sqlite3.connect(str(db_backup))
        try:
            dest = sqlite3.connect(str(target))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
    except sqlite3.Error as e:
        raise RestoreError(f"restore failed: {e}") from e

    log.warning(f"state db restored from {backup} pre_backup={pre}")
    return RestoreResult(
        backup_dir=backup,
        target=target,
        dry_run=False,
        pre_backup=pre,
        schema_version=backup_version,
    )


__all__ = [
    "BackupError",
    "BackupResult",
    "RestoreError",
    "RestoreResult",
    "create_backup",
    "restore_backup",
]
