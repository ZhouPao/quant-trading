"""永续合约资金费率结算（M4-15）。

结算规则（OKX 永续）：
- 每 8 小时结算一次，结算时刻由资金费率历史记录的 ts 给出；
- 结算额 = rate × 仓位数量 × 结算价；rate > 0 时多头付给空头。用带符号的
  qty（空头为负）统一表示即 `cash -= rate * qty * price`；
- 只在结算时刻持有仓位才计费，空仓不计费。

回测里的结算价用「结算时刻所在 bar 的开盘价」近似（OKX 用标记价格，本地
只存 K 线）。同一根 bar 内跨多个结算点时逐个结算。
"""
from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from quant.models.funding_rate import FundingRate

#: 与 FundingRateFetcher 落盘目录保持一致（data/funding_rates/{inst}.json）
FUNDING_DIR = "funding_rates"


def swap_inst_id(inst: str) -> str:
    """把现货 inst（BTC-USDT）映射到永续（BTC-USDT-SWAP）。"""
    return inst if inst.endswith("-SWAP") else f"{inst}-SWAP"


def load_funding_rates(data_dir: Path, inst: str) -> list[FundingRate]:
    """读取本地资金费率；文件不存在返回空列表。

    Args:
        data_dir: 数据根目录（落盘在 data/funding_rates/）
        inst: 现货或永续 inst，内部统一映射到 `-SWAP`
    """
    safe = swap_inst_id(inst).replace("/", "_")
    path = Path(data_dir) / FUNDING_DIR / f"{safe}.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        FundingRate(
            inst_id=d["inst_id"],
            ts=int(d["ts"]),
            rate=Decimal(str(d["rate"])),
            next_funding_ts=int(d["next_funding_ts"]),
        )
        for d in raw
    ]


@dataclass(frozen=True, slots=True)
class FundingSchedule:
    """按时间升序的资金费率结算表（不可变，可在多个回测间复用）。"""

    timestamps: tuple[int, ...]
    rates: tuple[Decimal, ...]

    @classmethod
    def from_rates(cls, rates: list[FundingRate]) -> FundingSchedule:
        items = sorted((r.ts, r.rate) for r in rates)
        return cls(
            timestamps=tuple(ts for ts, _ in items),
            rates=tuple(rate for _, rate in items),
        )

    def index_until(self, until_ts: int) -> int:
        """返回 ts <= until_ts 的结算点数量；配合调用方的游标逐根推进。"""
        return bisect_right(self.timestamps, until_ts)

    def __len__(self) -> int:
        return len(self.timestamps)


__all__ = [
    "FUNDING_DIR",
    "FundingSchedule",
    "load_funding_rates",
    "swap_inst_id",
]
