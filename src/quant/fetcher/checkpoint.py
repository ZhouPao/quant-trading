"""Checkpoint: 断点续传状态（spec §5.5）。

记录某个 (inst_id, bar_size) 已下载到的 last_ts，中断后从断点续拉。

存储：
    data/.checkpoints/{inst_id}_{bar_size}.json
    {"last_ts": 1704067200000, "updated_at": "2026-10-17T12:34:56Z"}

设计：
    - 用 dataclass + 手写 to_json/from_json，与 Step 2 models 风格一致
    - 不依赖 pydantic，避免 v1/v2 差异
    - updated_at 用 UTC ISO 8601，方便人工查证
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(slots=True)
class Checkpoint:
    """断点续传状态。

    Attributes:
        last_ts: 已成功落盘的最后一根 K线 ts（UTC ms，开盘认证）
        updated_at: 上次更新时间（UTC ISO 8601 字符串），方便人工查证
    """

    last_ts: int
    updated_at: str = field(default="")

    def __post_init__(self) -> None:
        if not self.updated_at:
            self.updated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(
            {"last_ts": self.last_ts, "updated_at": self.updated_at},
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> Checkpoint:
        """从 JSON 字符串反序列化。"""
        data = json.loads(text)
        return cls(
            last_ts=int(data["last_ts"]),
            updated_at=str(data.get("updated_at", "")),
        )

    def save(self, path: Path) -> None:
        """写入文件（原子写入：先写临时文件再 rename）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.to_json())
        tmp.replace(path)

    @classmethod
    def load_or_default(cls, path: Path, default_ts: int = 0) -> Checkpoint:
        """读文件；不存在或损坏返回 default_ts 的 Checkpoint。"""
        if not path.exists():
            return cls(last_ts=default_ts)
        try:
            return cls.from_json(path.read_text())
        except (json.JSONDecodeError, KeyError, ValueError):
            # 文件损坏，从头开始
            return cls(last_ts=default_ts)
