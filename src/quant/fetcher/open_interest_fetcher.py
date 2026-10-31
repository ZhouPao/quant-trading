"""OpenInterestFetcher: 永续合约持仓量拉取（M2-14）。

从 OKX /api/v5/public/open-interest 拉最近一条持仓量记录，
按 ts 去重追加到 data/open_interest/{inst_id}.json。

设计简化说明：
- OKX /public/open-interest 端点仅返回最新一条快照（不支持翻页），
  无法回补历史区间，需定时拉取积累历史
- 每条记录按 ts 唯一；新记录追加到文件，已存在的 ts 跳过（去重）
- 数据量小（定时拉取），整体加载-去重-覆盖写回可接受
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from quant.foundation.logger import get_logger
from quant.models.open_interest import OpenInterest
from quant.okx.client import OKXClient

_log = get_logger("fetcher.open_interest")


class OpenInterestFetcher:
    """拉 OKX 永续合约持仓量并落盘（追加去重）。

    Args:
        client: OKXClient 实例
        data_dir: 数据根目录（落盘到 data/open_interest/{inst_id}.json）
    """

    def __init__(self, client: OKXClient, data_dir: Path) -> None:
        self._client = client
        self._root = Path(data_dir) / "open_interest"
        self._root.mkdir(parents=True, exist_ok=True)

    def fetch(self, inst_id: str) -> int:
        """拉最近一条持仓量，去重追加落盘，返回净增条数（0 或 1）。

        Args:
            inst_id: 永续合约 ID，如 "BTC-USDT-SWAP"
        """
        if not inst_id:
            raise ValueError("inst_id must not be empty")
        _log.info(f"fetch start inst={inst_id}")
        raw = self._client.get_open_interest(inst_id=inst_id)
        if raw is None:
            _log.info(f"fetch empty inst={inst_id} (OKX returned no data)")
            return 0
        record = _dict_to_open_interest(raw)
        existing = self._load_all(inst_id)
        # 去重：按 ts 检查
        if any(r.ts == record.ts for r in existing):
            _log.info(
                f"fetch skip duplicate inst={inst_id} ts={record.ts}"
            )
            return 0
        existing.append(record)
        existing.sort(key=lambda r: r.ts)
        self._save(inst_id, existing)
        _log.info(
            f"fetch done inst={inst_id} ts={record.ts} oi={record.oi} "
            f"oi_value={record.oi_value} total={len(existing)}"
        )
        return 1

    def load(self, inst_id: str) -> list[OpenInterest]:
        """读取已落盘的某产品全部持仓量记录。"""
        return self._load_all(inst_id)

    def _path_for(self, inst_id: str) -> Path:
        # 文件名替换非法字符（如 "/"）以避免目录问题
        safe = inst_id.replace("/", "_")
        return self._root / f"{safe}.json"

    def _load_all(self, inst_id: str) -> list[OpenInterest]:
        path = self._path_for(inst_id)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return [_dict_to_open_interest(d) for d in data]

    def _save(self, inst_id: str, records: list[OpenInterest]) -> None:
        path = self._path_for(inst_id)
        payload = [_open_interest_to_dict(r) for r in records]
        # 原子写：先 tmp 再 rename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)


def _open_interest_to_dict(r: OpenInterest) -> dict:
    return {
        "inst_id": r.inst_id,
        "ts": r.ts,
        "oi": str(r.oi),
        "oi_value": str(r.oi_value),
    }


def _dict_to_open_interest(d: dict) -> OpenInterest:
    return OpenInterest(
        inst_id=d["instId"] if "instId" in d else d["inst_id"],
        ts=int(d["ts"]),
        oi=Decimal(str(d["oi"])),
        oi_value=Decimal(str(d["oiValue"] if "oiValue" in d else d["oi_value"])),
    )


__all__ = ["OpenInterestFetcher"]
