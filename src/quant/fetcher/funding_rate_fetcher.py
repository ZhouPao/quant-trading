"""FundingRateFetcher: 永续合约资金费率拉取（spec §5.7）。

从 OKX /api/v5/public/funding-rate 拉最近一条资金费率记录，
按 ts 去重追加到 data/funding_rates/{inst_id}.json。

设计简化说明：
- OKX /public/funding-rate 端点仅返回最近一条结算记录（不支持翻页），
  无需断点续传/翻页/校验循环
- 每条记录按 ts 唯一；新记录追加到文件，已存在的 ts 跳过（去重）
- 数据量小（每 8h 一条），整体加载-去重-覆盖写回可接受
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from quant.foundation.logger import get_logger
from quant.models.funding_rate import FundingRate
from quant.okx.client import OKXClient

_log = get_logger("fetcher.funding")


class FundingRateFetcher:
    """拉 OKX 永续合约资金费率并落盘（追加去重）。

    Args:
        client: OKXClient 实例
        data_dir: 数据根目录（落盘到 data/funding_rates/{inst_id}.json）
    """

    def __init__(self, client: OKXClient, data_dir: Path) -> None:
        self._client = client
        self._root = Path(data_dir) / "funding_rates"
        self._root.mkdir(parents=True, exist_ok=True)

    def fetch(self, inst_id: str) -> int:
        """拉最近一条资金费率，去重追加落盘，返回净增条数（0 或 1）。

        Args:
            inst_id: 永续合约 ID，如 "BTC-USDT-SWAP"
        """
        if not inst_id:
            raise ValueError("inst_id must not be empty")
        _log.info(f"fetch start inst={inst_id}")
        rate = self._client.get_funding_rate(inst_id=inst_id)
        if rate is None:
            _log.info(f"fetch empty inst={inst_id} (OKX returned no data)")
            return 0
        existing = self._load_all(inst_id)
        # 去重：按 ts 检查
        if any(r.ts == rate.ts for r in existing):
            _log.info(
                f"fetch skip duplicate inst={inst_id} ts={rate.ts}"
            )
            return 0
        existing.append(rate)
        existing.sort(key=lambda r: r.ts)
        self._save(inst_id, existing)
        _log.info(
            f"fetch done inst={inst_id} ts={rate.ts} rate={rate.rate} "
            f"total={len(existing)}"
        )
        return 1

    def load(self, inst_id: str) -> list[FundingRate]:
        """读取已落盘的某产品全部资金费率记录。"""
        return self._load_all(inst_id)

    def _path_for(self, inst_id: str) -> Path:
        # 文件名替换非法字符（如 "/"）以避免目录问题
        safe = inst_id.replace("/", "_")
        return self._root / f"{safe}.json"

    def _load_all(self, inst_id: str) -> list[FundingRate]:
        path = self._path_for(inst_id)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return [_dict_to_funding_rate(d) for d in data]

    def _save(self, inst_id: str, rates: list[FundingRate]) -> None:
        path = self._path_for(inst_id)
        payload = [_funding_rate_to_dict(r) for r in rates]
        # 原子写：先 tmp 再 rename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)


def _funding_rate_to_dict(r: FundingRate) -> dict:
    return {
        "inst_id": r.inst_id,
        "ts": r.ts,
        "rate": str(r.rate),
        "next_funding_ts": r.next_funding_ts,
    }


def _dict_to_funding_rate(d: dict) -> FundingRate:
    return FundingRate(
        inst_id=d["inst_id"],
        ts=int(d["ts"]),
        rate=Decimal(str(d["rate"])),
        next_funding_ts=int(d["next_funding_ts"]),
    )


__all__ = ["FundingRateFetcher"]
