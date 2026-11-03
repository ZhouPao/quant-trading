"""Bar 存储层（spec §6.4）。

parquet 按 UTC 年份分文件存储，同时间戳去重（保留最新）。
目录结构：data/candles/{inst_type}/{inst_id}/{bar_size}/{year}.parquet

存储优化：
- OHLC/vol 用 float64（15-17 位有效数字，价格足够）
- ts 用 int64 + delta encoding（parquet 自动）
- zstd 压缩 + row_group_size=8192 让单 row group 更紧凑
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pandas as pd

from quant.models.bar import Bar

#: inst_id / bar_size 会被直接拼进文件路径，且可能来自 HTTP 查询参数：
#: 只允许安全字符，杜绝 ../ 路径穿越（P2）
_INST_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_BAR_SIZE_RE = re.compile(r"[A-Za-z0-9]{1,8}")


def validate_inst_id(inst_id: str) -> None:
    """校验 inst_id 可安全用作路径段；非法抛 ValueError。"""
    if not _INST_ID_RE.fullmatch(inst_id):
        raise ValueError(f"invalid inst_id: {inst_id!r}")


def validate_market_identifiers(inst_id: str, bar_size: str) -> None:
    """校验 inst_id / bar_size 可安全用作路径段；非法抛 ValueError。"""
    validate_inst_id(inst_id)
    if not _BAR_SIZE_RE.fullmatch(bar_size):
        raise ValueError(f"invalid bar_size: {bar_size!r}")


class BarStorage:
    """K线 parquet 持久化。

    同一 (inst_id, bar_size, year) 落到同一文件；
    写入时若文件已存在，merge + 去重（保留新值，按 ts 覆盖）；
    读取按时间区间返回 Bar 列表。

    写入为原子替换（tmp + rename），读取方不会读到半截文件；
    两个进程同时写同一文件仍是「后写覆盖」可能丢更新——当前只有
    sync-candles 单进程写入，若未来多写者需引入文件锁。
    """

    def __init__(self, data_dir: Path) -> None:
        self._root = Path(data_dir)

    def save_bars(self, bars: list[Bar]) -> int:
        """批量保存K线，返回净新增/覆盖的条数（保存后总数 - 保存前总数）。

        同 ts 重复时，输入列表中后出现的覆盖先出现的。
        若文件已存在，与磁盘上已有数据合并后去重（保留新值）。
        """
        if not bars:
            return 0

        # 按 (inst_id, bar_size, year) 分组
        groups: dict[tuple[str, str, int], list[Bar]] = {}
        for b in bars:
            year = self._year_of(b.ts)
            groups.setdefault((b.inst_id, b.bar_size, year), []).append(b)

        written = 0
        for (inst_id, bar_size, year), group in groups.items():
            path = self._path_for(inst_id, bar_size, year)
            path.parent.mkdir(parents=True, exist_ok=True)
            df_new = self._to_df(group)

            if path.exists():
                df_old = pd.read_parquet(path)
                old_count = len(df_old)
                df = pd.concat([df_old, df_new], ignore_index=True)
                df = df.drop_duplicates(subset=["ts"], keep="last")
                df = df.sort_values("ts").reset_index(drop=True)
                new_count = len(df)
            else:
                old_count = 0
                df = df_new.drop_duplicates(subset=["ts"], keep="last")
                df = df.sort_values("ts").reset_index(drop=True)
                new_count = len(df)

            # 原子写：先写临时文件再 replace，读取方不会读到半截 parquet
            tmp = path.with_name(path.name + ".tmp")
            df.to_parquet(
                tmp, index=False, engine="pyarrow", compression="zstd",
                row_group_size=8192,
            )
            tmp.replace(path)
            written += new_count - old_count
        return written

    def load_bars(
        self,
        inst_id: str,
        bar_size: str,
        start_ts: int,
        end_ts: int,
    ) -> list[Bar]:
        """按时间区间 [start_ts, end_ts] 读取K线（含端点）。"""
        years = range(self._year_of(start_ts), self._year_of(end_ts) + 1)
        dfs: list[pd.DataFrame] = []
        for y in years:
            path = self._path_for(inst_id, bar_size, y)
            if path.exists():
                df = pd.read_parquet(path)
                df = df[(df["ts"] >= start_ts) & (df["ts"] <= end_ts)]
                if not df.empty:
                    dfs.append(df)
        if not dfs:
            return []
        df = pd.concat(dfs, ignore_index=True).sort_values("ts").reset_index(drop=True)
        return [self._row_to_bar(row, inst_id, bar_size) for _, row in df.iterrows()]

    def list_years(self, inst_id: str, bar_size: str) -> list[int]:
        """列出磁盘上已存在的年份文件，按升序。"""
        validate_inst_id(inst_id)
        inst_type = self._infer_type(inst_id)
        dir_ = self._root / "candles" / inst_type / inst_id / bar_size
        if not dir_.exists():
            return []
        years: list[int] = []
        for p in dir_.glob("*.parquet"):
            try:
                years.append(int(p.stem))
            except ValueError:
                continue
        return sorted(years)

    def list_bar_sizes(self, inst_id: str) -> list[str]:
        """列出磁盘上已有 K 线文件的周期目录（如 ["1H", "4H"]）。"""
        validate_inst_id(inst_id)
        inst_type = self._infer_type(inst_id)
        dir_ = self._root / "candles" / inst_type / inst_id
        if not dir_.exists():
            return []
        return sorted(
            p.name
            for p in dir_.iterdir()
            if p.is_dir() and any(p.glob("*.parquet"))
        )

    def _path_for(self, inst_id: str, bar_size: str, year: int) -> Path:
        validate_market_identifiers(inst_id, bar_size)
        inst_type = self._infer_type(inst_id)
        return self._root / "candles" / inst_type / inst_id / bar_size / f"{year}.parquet"

    @staticmethod
    def _year_of(ts_ms: int) -> int:
        """UTC 年份。"""
        return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.UTC).year

    @staticmethod
    def _infer_type(inst_id: str) -> str:
        """从 inst_id 推断 inst_type（spec §6.1 目录结构）。

        OKX inst_id 命名规则：
        - SPOT: BTC-USDT
        - SWAP: BTC-USDT-SWAP
        - FUTURES: BTC-USDT-240928（末段为 6 位交割日期）
        - OPTION: BTC-USD-240928-50000-C（末段 C/P）
        """
        parts = inst_id.split("-")
        if len(parts) >= 2 and parts[-1] == "SWAP":
            return "swap"
        if len(parts) >= 3 and parts[-1].isdigit():
            return "futures"
        if len(parts) >= 5 and parts[-1] in ("C", "P"):
            return "option"
        return "spot"

    @staticmethod
    def _to_df(bars: list[Bar]) -> pd.DataFrame:
        """Bar 列表转 DataFrame（Decimal 存为 string 以保精度）。"""
        return pd.DataFrame(
            [
                {
                    "ts": b.ts,
                    "open": str(b.open),
                    "high": str(b.high),
                    "low": str(b.low),
                    "close": str(b.close),
                    "vol": str(b.vol),
                    "vol_quote": str(b.vol_quote),
                    "confirm": b.confirm,
                }
                for b in bars
            ]
        )

    @staticmethod
    def _row_to_bar(row: pd.Series, inst_id: str, bar_size: str) -> Bar:
        """行转 Bar（Decimal 从 string 还原）。"""
        return Bar(
            ts=int(row["ts"]),
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            vol=Decimal(str(row["vol"])),
            vol_quote=Decimal(str(row["vol_quote"])),
            confirm=int(row["confirm"]),
            inst_id=inst_id,
            bar_size=bar_size,
        )


__all__ = ["BarStorage", "validate_inst_id", "validate_market_identifiers"]
