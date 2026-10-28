"""Instrument 存储层（spec §6.1）。

JSON 文件按 inst_type 分文件存储全部产品配置。
目录结构：data/instruments/{inst_type}.json
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from quant.models.instrument import Instrument


class InstrumentStorage:
    """产品配置 JSON 持久化。

    每个 inst_type 一个文件（spot.json / swap.json / futures.json / option.json）。
    save_instruments 全量覆盖（OKX 列表会变动，每次拉取替换最新版）。
    """

    def __init__(self, data_dir: Path) -> None:
        self._root = Path(data_dir)

    def save_instruments(self, inst_type: str, instruments: list[Instrument]) -> int:
        """全量覆盖某 inst_type 的产品列表，返回写入条数。"""
        inst_type = inst_type.lower()
        path = self._path_for(inst_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [self._instrument_to_dict(i) for i in instruments]
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return len(data)

    def load_instruments(self, inst_type: str) -> list[Instrument]:
        """读取某 inst_type 的产品列表。"""
        path = self._path_for(inst_type.lower())
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return [self._dict_to_instrument(d) for d in data]

    def load_one(self, inst_id: str, inst_type: str) -> Instrument | None:
        """按 inst_id 查单个产品配置。"""
        for i in self.load_instruments(inst_type):
            if i.inst_id == inst_id:
                return i
        return None

    def _path_for(self, inst_type: str) -> Path:
        return self._root / "instruments" / f"{inst_type}.json"

    @staticmethod
    def _instrument_to_dict(i: Instrument) -> dict:
        """Instrument → JSON dict（Decimal 转 str）。"""
        return {
            "inst_id": i.inst_id,
            "inst_type": i.inst_type,
            "base_ccy": i.base_ccy,
            "quote_ccy": i.quote_ccy,
            "settle_ccy": i.settle_ccy,
            "tick_sz": str(i.tick_sz),
            "lot_sz": str(i.lot_sz),
            "min_sz": str(i.min_sz),
            "ct_val": str(i.ct_val),
            "state": i.state,
            "list_time": i.list_time,
        }

    @staticmethod
    def _dict_to_instrument(d: dict) -> Instrument:
        """JSON dict → Instrument。"""
        return Instrument(
            inst_id=d["inst_id"],
            inst_type=d["inst_type"],
            base_ccy=d["base_ccy"],
            quote_ccy=d["quote_ccy"],
            settle_ccy=d.get("settle_ccy", ""),
            tick_sz=Decimal(str(d["tick_sz"])),
            lot_sz=Decimal(str(d["lot_sz"])),
            min_sz=Decimal(str(d["min_sz"])),
            ct_val=Decimal(str(d["ct_val"])),
            state=d["state"],
            list_time=int(d["list_time"]),
        )


__all__ = ["InstrumentStorage"]
