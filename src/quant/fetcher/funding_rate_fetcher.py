"""FundingRateFetcher: 永续合约资金费率拉取（spec §5.7）。

两条数据来源：
- `/public/funding-rate`：仅返回最近一条结算记录，供定时轮询增量写入；
- `/public/funding-rate-history`：可按 `after` 向前翻页，用于回补历史区间
  （M2-4；M4-15 的资金费率成本回测依赖它）。

两者都按 ts 去重后合并到 data/funding_rates/{inst_id}.json。历史端点的响应
不含 `nextFundingTime`，该字段回补时为 0，后续实时轮询会把同一条记录补齐。

设计简化说明：
- 每条记录按 ts 唯一；已存在的 ts 跳过（去重）
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

#: 回补时最多翻多少页（每页 100 条 ≈ 33 天）。默认 100 页 ≈ 9 年，
#: 足以覆盖任何合约的全生命周期，同时给「参数写错导致无限翻页」兜底。
DEFAULT_MAX_PAGES = 100


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
        return self._merge_and_save(inst_id, [rate])

    def fetch_history(
        self,
        inst_id: str,
        start_ts: int,
        end_ts: int | None = None,
        page_limit: int = 100,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> int:
        """回补 [start_ts, end_ts] 的历史资金费率，返回净增条数。

        OKX 从新到旧返回，因此按「最旧一条的时间戳」作为 `after` 逐页向
        前翻：`after=T` 返回 fundingTime 早于 T 的记录。

        Args:
            inst_id: 永续合约 ID
            start_ts: 起始时间（含），UTC ms
            end_ts: 结束时间（含），UTC ms；None 表示到最新
            page_limit: 每页条数，OKX 上限 100
            max_pages: 最多翻页数，防止参数写错时无限请求

        Returns:
            净增条数；重复的 ts 不计入。
        """
        if not inst_id:
            raise ValueError("inst_id must not be empty")
        if end_ts is not None and start_ts >= end_ts:
            raise ValueError("start_ts must be before end_ts")

        collected: list[FundingRate] = []
        cursor: int | None = None
        pages = 0
        reached_start = False
        while pages < max_pages:
            batch = self._client.get_funding_rate_history(
                inst_id, after=cursor, limit=page_limit
            )
            pages += 1
            if not batch:
                break
            page_oldest = min(r.ts for r in batch)
            for r in batch:
                if r.ts < start_ts:
                    reached_start = True
                    continue
                if end_ts is not None and r.ts > end_ts:
                    continue
                collected.append(r)
            # 已经翻到区间起点；或这一页不满（到最早一条）；或分页没推进
            if reached_start or len(batch) < page_limit:
                break
            if cursor is not None and page_oldest >= cursor:
                _log.warning(
                    f"history pagination stalled inst={inst_id} cursor={cursor}"
                )
                break
            cursor = page_oldest

        if pages >= max_pages:
            _log.warning(
                f"history hit max_pages={max_pages} inst={inst_id}; "
                "区间可能未回补完整，可调大 --max-pages"
            )

        _log.info(
            f"history fetched inst={inst_id} pages={pages} "
            f"records={len(collected)}"
        )
        if not collected:
            return 0
        return self._merge_and_save(inst_id, collected)

    def load(self, inst_id: str) -> list[FundingRate]:
        """读取已落盘的某产品全部资金费率记录。"""
        return self._load_all(inst_id)

    def _merge_and_save(self, inst_id: str, incoming: list[FundingRate]) -> int:
        """按 ts 合并去重后落盘，返回净增条数。

        同一 ts 已存在时保留 `next_funding_ts` 非 0 的那条：历史端点不带
        该字段，实时端点带，不该因为回补把已有信息清掉。
        """
        merged: dict[int, FundingRate] = {r.ts: r for r in self._load_all(inst_id)}
        added = 0
        upgraded = False
        for rate in incoming:
            current = merged.get(rate.ts)
            if current is None:
                added += 1
                merged[rate.ts] = rate
            elif current.next_funding_ts == 0 < rate.next_funding_ts:
                merged[rate.ts] = rate
                upgraded = True
        if added == 0 and not upgraded:
            return 0
        rates = sorted(merged.values(), key=lambda r: r.ts)
        self._save(inst_id, rates)
        _log.info(
            f"merge done inst={inst_id} added={added} total={len(rates)}"
        )
        return added

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
