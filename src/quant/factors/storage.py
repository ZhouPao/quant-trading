"""因子值缓存（M5-4）。

目录与 K 线同构：`data/factors/{factor_id}/{param_hash}/{inst}/{bar}/{year}.parquet`
——按年分片、原子写（tmp + replace），读取方不会看到半截文件。
`value` 列保持 Decimal 文本；缺失写 null。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from quant.factors.params import param_hash

_PARQUET_WRITE = {
    "index": False,
    "engine": "pyarrow",
    "compression": "zstd",
    "row_group_size": 8192,
}


def _year_of(ts_ms: int) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).year


class FactorStore:
    """因子值的读写（按 因子 × 参数哈希 × 品种 × 周期 × 年份 分片）。"""

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir) / "factors"

    def path(
        self,
        factor_id: str,
        params: dict,
        inst_id: str,
        bar: str,
        year: int,
    ) -> Path:
        return (
            self._root
            / factor_id
            / param_hash(params)
            / inst_id
            / bar
            / f"{year}.parquet"
        )

    def save(
        self,
        factor_id: str,
        params: dict,
        inst_id: str,
        bar: str,
        rows: list[tuple[int, Decimal | None]],
    ) -> int:
        """写入/合并因子值，返回写入条数（同 ts 以新值为准）。"""
        if not rows:
            return 0
        years = sorted({_year_of(ts) for ts, _ in rows})
        frames: list[pd.DataFrame] = []
        for year in years:
            path = self.path(factor_id, params, inst_id, bar, year)
            if path.is_file():
                frames.append(pd.read_parquet(path))
        frames.append(
            pd.DataFrame(
                {
                    "ts": [ts for ts, _ in rows],
                    "value": [
                        None if value is None else str(value) for _, value in rows
                    ],
                }
            )
        )
        merged = (
            pd.concat(frames, ignore_index=True)
            .drop_duplicates("ts", keep="last")
            .sort_values("ts")
        )

        written = 0
        for year in years:
            subset = merged[
                merged["ts"].map(_year_of) == year
            ].reset_index(drop=True)
            path = self.path(factor_id, params, inst_id, bar, year)
            path.parent.mkdir(parents=True, exist_ok=True)
            # tmp 必须每次唯一：values 与 correlations 可能并发计算同一个因子，
            # 固定 tmp 名会互相覆盖导致写失败/接口 500。
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                subset.to_parquet(tmp, **_PARQUET_WRITE)
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
            written += len(subset)
        return written

    def load(
        self,
        factor_id: str,
        params: dict,
        inst_id: str,
        bar: str,
        start_ts: int,
        end_ts: int,
    ) -> list[tuple[int, Decimal | None]]:
        """读取 [start_ts, end_ts] 内的因子值，按 ts 升序。"""
        years = range(_year_of(start_ts), _year_of(end_ts) + 1)
        frames = [
            pd.read_parquet(self.path(factor_id, params, inst_id, bar, year))
            for year in years
            if self.path(factor_id, params, inst_id, bar, year).is_file()
        ]
        if not frames:
            return []
        df = pd.concat(frames, ignore_index=True)
        df = df[(df["ts"] >= start_ts) & (df["ts"] <= end_ts)]
        df = df.sort_values("ts")
        result: list[tuple[int, Decimal | None]] = []
        for row in df.itertuples(index=False):
            raw = row.value
            value = None if pd.isna(raw) else Decimal(str(raw))
            result.append((int(row.ts), value))
        return result


__all__ = ["FactorStore"]
