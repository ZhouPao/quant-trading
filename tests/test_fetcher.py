"""Fetcher 层单元测试（spec §5.5）。

覆盖：
- Checkpoint 序列化与持久化
- CandleFetcher 断点续传 / 翻页 / 缺失补拉
- InstrumentFetcher 全量拉取
- FundingRateFetcher 历史拉取

测试策略：
- 用 unittest.mock.MagicMock 替代 OKXClient，不联网
- 覆盖边界场景：断点存在/不存在、空区间、翻页到边界、补拉循环终止
- 每个测试独立临时目录，不互相干扰
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant.fetcher.checkpoint import Checkpoint

# =====================================================================
# Checkpoint 单元测试
# =====================================================================


class TestCheckpointSerialization:
    """Checkpoint to_json/from_json 序列化测试。"""

    def test_to_json_has_required_fields(self) -> None:
        ckpt = Checkpoint(last_ts=1704067200000)
        data = json.loads(ckpt.to_json())
        assert data["last_ts"] == 1704067200000
        assert "updated_at" in data
        assert data["updated_at"]

    def test_from_json_roundtrip(self) -> None:
        ckpt = Checkpoint(last_ts=1704067200000, updated_at="2026-10-17T12:34:56Z")
        text = ckpt.to_json()
        ckpt2 = Checkpoint.from_json(text)
        assert ckpt2.last_ts == 1704067200000
        assert ckpt2.updated_at == "2026-10-17T12:34:56Z"

    def test_to_json_is_valid_json_string(self) -> None:
        ckpt = Checkpoint(last_ts=0)
        text = ckpt.to_json()
        # 必须是可解析的 JSON
        json.loads(text)

    def test_from_json_invalid_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            Checkpoint.from_json("not json")

    def test_from_json_missing_last_ts_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            Checkpoint.from_json('{"updated_at": "x"}')

    def test_from_json_negative_last_ts_still_loads(self) -> None:
        # 不应该假设 last_ts >= 0，留给上层判断
        ckpt = Checkpoint.from_json('{"last_ts": -1}')
        assert ckpt.last_ts == -1


class TestCheckpointAutoTimestamp:
    """Checkpoint updated_at 自动填充测试。"""

    def test_updated_at_auto_filled_on_construct(self) -> None:
        ckpt = Checkpoint(last_ts=0)
        assert ckpt.updated_at  # 非空

    def test_updated_at_format_is_iso8601(self) -> None:
        ckpt = Checkpoint(last_ts=0)
        # 形如 2026-10-17T12:34:56Z
        assert ckpt.updated_at.endswith("Z")
        assert "T" in ckpt.updated_at

    def test_updated_at_explicit_not_overwritten(self) -> None:
        ckpt = Checkpoint(last_ts=0, updated_at="2020-01-01T00:00:00Z")
        assert ckpt.updated_at == "2020-01-01T00:00:00Z"


class TestCheckpointPersistence:
    """Checkpoint save/load 文件持久化测试。"""

    def test_save_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "ckpt.json"
        ckpt = Checkpoint(last_ts=1704067200000)
        ckpt.save(path)
        assert path.exists()

    def test_save_creates_parent_dir(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "ckpt.json"
        Checkpoint(last_ts=0).save(path)
        assert path.exists()

    def test_save_content_is_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "ckpt.json"
        Checkpoint(last_ts=42).save(path)
        data = json.loads(path.read_text())
        assert data["last_ts"] == 42

    def test_load_or_default_returns_default_when_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.json"
        ckpt = Checkpoint.load_or_default(path, default_ts=100)
        assert ckpt.last_ts == 100

    def test_load_or_default_loads_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "ckpt.json"
        Checkpoint(last_ts=999).save(path)
        ckpt = Checkpoint.load_or_default(path, default_ts=0)
        assert ckpt.last_ts == 999

    def test_load_or_default_handles_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "ckpt.json"
        path.write_text("not valid json")
        ckpt = Checkpoint.load_or_default(path, default_ts=42)
        assert ckpt.last_ts == 42  # 文件损坏从头开始

    def test_load_or_default_handles_missing_field(self, tmp_path: Path) -> None:
        path = tmp_path / "ckpt.json"
        path.write_text('{"updated_at": "x"}')  # 缺 last_ts
        ckpt = Checkpoint.load_or_default(path, default_ts=42)
        assert ckpt.last_ts == 42

    def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "ckpt.json"
        Checkpoint(last_ts=1).save(path)
        Checkpoint(last_ts=2).save(path)
        data = json.loads(path.read_text())
        assert data["last_ts"] == 2

    def test_save_no_tmp_file_left(self, tmp_path: Path) -> None:
        """原子写入后不留 .tmp 文件。"""
        path = tmp_path / "ckpt.json"
        Checkpoint(last_ts=1).save(path)
        assert not (tmp_path / "ckpt.tmp").exists()
