"""InstrumentFetcher: 产品配置拉取（spec §5.6）。

从 OKX /api/v5/public/instruments 拉某 inst_type 的全部产品，全量覆盖落盘。

特点：
- 全量覆盖：OKX 列表会变动（新品上线/老品下架），每次拉取替换最新版
- 不分页：OKX /public/instruments 一次返回该 inst_type 全部产品（最多 50 条/页，
  实际 SWAP/SPOT 数千条需翻页）。这里采用最简策略：分页拉完。
"""
from __future__ import annotations

from pathlib import Path

from quant.foundation.logger import get_logger
from quant.models.instrument import Instrument
from quant.okx.client import OKXClient
from quant.storage.instrument_storage import InstrumentStorage

_log = get_logger("fetcher.instrument")


class InstrumentFetcher:
    """拉 OKX 产品配置并全量覆盖落盘。

    Args:
        client: OKXClient 实例
        data_dir: 数据根目录（落盘到 data/instruments/{inst_type}.json）
    """

    def __init__(self, client: OKXClient, data_dir: Path) -> None:
        self._client = client
        self._storage = InstrumentStorage(data_dir)

    def fetch(self, inst_type: str = "SPOT") -> int:
        """拉取某 inst_type 全部产品，全量覆盖落盘，返回条数。

        Args:
            inst_type: SPOT/SWAP/FUTURES/OPTION
        """
        if inst_type not in ("SPOT", "SWAP", "FUTURES", "OPTION"):
            raise ValueError(f"unknown inst_type: {inst_type}")

        _log.info(f"fetch start inst_type={inst_type}")
        instruments = self._client.get_instruments(inst_type=inst_type)
        # 只保留 live 状态的（OKX 列表含 canceled/expired）
        live = [i for i in instruments if i.is_live()]
        # 按 inst_id 排序，方便 diff
        live.sort(key=lambda i: i.inst_id)
        saved = self._storage.save_instruments(inst_type, live)
        _log.info(
            f"fetch done inst_type={inst_type} total={len(instruments)} "
            f"live={saved} saved"
        )
        return saved

    def load(self, inst_type: str = "SPOT") -> list[Instrument]:
        """读取已落盘的某 inst_type 全部产品。"""
        return self._storage.load_instruments(inst_type)


__all__ = ["InstrumentFetcher"]
