"""M10-7 备份测试：在线快照、manifest 校验、配置快照与错误路径。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from quant.foundation.backup import (
    BackupError,
    RestoreError,
    create_backup,
    restore_backup,
)
from quant.foundation.config import BacktestSettings, OKXSettings, Settings, StorageSettings
from quant.state.sqlite_store import SQLiteStore

NOW = 1_700_000_000_000


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p", demo=True),
        storage=StorageSettings(data_dir=tmp_path / "data"),
        state={"db_path": tmp_path / "data" / "state" / "quant.db"},
        backtest=BacktestSettings(),
    )


def _seed_db(path: Path) -> None:
    store = SQLiteStore(path)
    try:
        store.upsert_binding("sma_cross", "BTC-USDT", "1H", True, NOW)
    finally:
        store.close()


class TestCreateBackup:
    def test_backs_up_db_and_writes_manifest(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        _seed_db(settings.state.db_path)

        result = create_backup(
            settings, out_dir=tmp_path / "backups", now_ms=NOW
        )

        assert result.dir.is_dir()
        assert result.database.is_file()
        assert result.manifest.is_file()
        manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
        assert manifest["created_at"] == NOW
        assert manifest["schema_version"] >= 1
        kinds = {item["kind"] for item in manifest["files"]}
        assert kinds == {"state"}
        assert all(len(item["sha256"]) == 64 for item in manifest["files"])

        # 备份库可独立打开且数据完整（绑定还在）
        conn = sqlite3.connect(str(result.database))
        try:
            rows = conn.execute(
                "SELECT strategy_id, inst_id FROM strategy_bindings"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [("sma_cross", "BTC-USDT")]

    def test_snapshots_explicit_config_file(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        _seed_db(settings.state.db_path)
        config = tmp_path / "config.toml"
        config.write_text("[engine]\nbar = \"1H\"\n", encoding="utf-8")

        result = create_backup(
            settings,
            out_dir=tmp_path / "backups",
            config_path=config,
            now_ms=NOW,
        )

        assert (result.dir / "config.toml").is_file()
        manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
        assert {item["kind"] for item in manifest["files"]} == {
            "state",
            "config",
        }

    def test_missing_db_raises(self, tmp_path) -> None:
        settings = _settings(tmp_path)

        with pytest.raises(BackupError, match="state db not found"):
            create_backup(settings, out_dir=tmp_path / "backups", now_ms=NOW)

    def test_schema_version_is_copied(self, tmp_path) -> None:
        """schema_version 直接读 PRAGMA，避免与 sqlite_store 循环依赖。"""
        settings = _settings(tmp_path)
        _seed_db(settings.state.db_path)

        result = create_backup(
            settings, out_dir=tmp_path / "backups", now_ms=NOW
        )

        conn = sqlite3.connect(str(result.database))
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == version


class TestRestoreBackup:
    def _make(self, tmp_path) -> tuple[Settings, object]:
        settings = _settings(tmp_path)
        _seed_db(settings.state.db_path)
        backup = create_backup(
            settings, out_dir=tmp_path / "backups", now_ms=NOW
        )
        return settings, backup

    def _drop_binding(self, settings: Settings) -> None:
        store = SQLiteStore(settings.state.db_path)
        try:
            store.delete_binding("sma_cross", "BTC-USDT")
            assert store.list_bindings() == []
        finally:
            store.close()

    def test_roundtrip_restores_data_and_backs_up_first(self, tmp_path) -> None:
        settings, backup = self._make(tmp_path)
        self._drop_binding(settings)

        result = restore_backup(settings, backup.dir, now_ms=NOW + 1_000)

        assert result.dry_run is False
        # 恢复前必须先备份当前（空）状态，出错可退回
        assert result.pre_backup is not None
        assert (result.pre_backup / "quant.db").is_file()

        store = SQLiteStore(settings.state.db_path)
        try:
            rows = store.list_bindings()
        finally:
            store.close()
        assert [(r["strategy_id"], r["inst_id"]) for r in rows] == [
            ("sma_cross", "BTC-USDT")
        ]

    def test_dry_run_does_not_touch_db(self, tmp_path) -> None:
        settings, backup = self._make(tmp_path)
        self._drop_binding(settings)
        before = settings.state.db_path.read_bytes()

        result = restore_backup(settings, backup.dir, dry_run=True)

        assert result.dry_run is True
        assert result.pre_backup is None
        assert settings.state.db_path.read_bytes() == before

    def test_tampered_backup_is_rejected(self, tmp_path) -> None:
        settings, backup = self._make(tmp_path)
        (backup.dir / "quant.db").write_bytes(b"tampered")

        with pytest.raises(RestoreError, match="mismatch"):
            restore_backup(settings, backup.dir, dry_run=True)

    def test_busy_wal_requires_force(self, tmp_path) -> None:
        settings, backup = self._make(tmp_path)
        wal = settings.state.db_path.with_name(
            f"{settings.state.db_path.name}-wal"
        )
        wal.write_bytes(b"pending")

        with pytest.raises(RestoreError, match="--force"):
            restore_backup(settings, backup.dir, now_ms=NOW + 1_000)

        # 确认已停进程后 --force 才能通过
        result = restore_backup(
            settings, backup.dir, force=True, now_ms=NOW + 2_000
        )
        assert result.dry_run is False

    def test_missing_manifest_is_rejected(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        empty = tmp_path / "empty-backup"
        empty.mkdir()

        with pytest.raises(RestoreError, match="manifest not found"):
            restore_backup(settings, empty, dry_run=True)
        """schema_version 直接读 PRAGMA，避免与 sqlite_store 循环依赖。"""
        settings = _settings(tmp_path)
        _seed_db(settings.state.db_path)

        result = create_backup(
            settings, out_dir=tmp_path / "backups", now_ms=NOW
        )

        conn = sqlite3.connect(str(result.database))
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == version
