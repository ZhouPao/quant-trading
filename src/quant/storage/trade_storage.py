"""Trade（逐笔成交）存储层（M2-14 订单流足迹图）。

parquet 按 UTC 日期分文件存储，同 trade_id 去重（保留最新）。
目录结构：data/trades/{inst_type}/{inst_id}/{YYYY-MM-DD}.parquet

trades 量远大于 K 线，故按日而非按年分区。Decimal 以 string 落盘保精度。
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from quant.models.trade import Trade

#: inst_id 会直接拼进文件路径，且可能来自 CLI 参数：只允许安全字符（P2）
_INST_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")


def validate_inst_id(inst_id: str) -> None:
    """校验 inst_id 可安全用作路径段；非法抛 ValueError。"""
    if not _INST_ID_RE.fullmatch(inst_id):
        raise ValueError(f"invalid inst_id: {inst_id!r}")


class TradeStorage:
    """逐笔成交 parquet 持久化。

    同一 (inst_id, date) 落到同一文件；写入时若文件已存在，merge + 按
    trade_id 去重（保留新值）；读取按时间区间返回 Trade 列表。

    写入为原子替换（tmp + rename），读取方不会读到半截文件。
    """

    def __init__(self, data_dir: Path) -> None:
        self._root = Path(data_dir)

    def save_trades(self, trades: list[Trade]) -> int:
        """批量保存逐笔成交，返回净新增/覆盖的条数（保存后 - 保存前）。

        同 trade_id 重复时，后出现的覆盖先出现的。
        """
        if not trades:
            return 0

        # 按 (inst_id, date) 分组
        groups: dict[tuple[str, str], list[Trade]] = {}
        for t in trades:
            groups.setdefault((t.inst_id, self._date_of(t.ts)), []).append(t)

        written = 0
        for (inst_id, date), group in groups.items():
            path = self._path_for(inst_id, date)
            path.parent.mkdir(parents=True, exist_ok=True)
            df_new = self._to_df(group)

            if path.exists():
                df_old = pd.read_parquet(path)
                old_count = len(df_old)
                df = pd.concat([df_old, df_new], ignore_index=True)
                df = df.drop_duplicates(subset=["trade_id"], keep="last")
                df = df.sort_values("ts").reset_index(drop=True)
                new_count = len(df)
            else:
                old_count = 0
                df = df_new.drop_duplicates(subset=["trade_id"], keep="last")
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

    def load_trades(
        self, inst_id: str, start_ts: int, end_ts: int
    ) -> list[Trade]:
        """按时间区间 [start_ts, end_ts] 读取逐笔成交（含端点）。"""
        validate_inst_id(inst_id)
        dfs: list[pd.DataFrame] = []
        for d in self._days_range(start_ts, end_ts):
            path = self._path_for(inst_id, d)
            if not path.exists():
                continue
            # 谓词下推：只解码命中区间所在的 row group
            df = pd.read_parquet(
                path,
                engine="pyarrow",
                filters=[("ts", ">=", start_ts), ("ts", "<=", end_ts)],
            )
            df = df[(df["ts"] >= start_ts) & (df["ts"] <= end_ts)]
            if not df.empty:
                dfs.append(df)
        if not dfs:
            return []
        df = pd.concat(dfs, ignore_index=True).sort_values("ts").reset_index(drop=True)
        return [self._row_to_trade(row, inst_id) for _, row in df.iterrows()]

    def latest_trade_id(self, inst_id: str) -> str | None:
        """返回该品种已存的最大（最新）trade_id，供回补续拉起点。

        OKX history-trades 的 after 游标取更旧的 trade_id；从已存最新处往旧
        翻页即可只补尚未落盘的更旧数据（避免重复拉取全量）。
        """
        validate_inst_id(inst_id)
        inst_type = self._infer_type(inst_id)
        dir_ = self._root / "trades" / inst_type / inst_id
        if not dir_.exists():
            return None
        best_ts = -1
        best_id: str | None = None
        for p in sorted(dir_.glob("*.parquet")):
            try:
                df = pd.read_parquet(p, engine="pyarrow", columns=["ts", "trade_id"])
            except Exception:  # noqa: BLE001
                continue
            if df.empty:
                continue
            idx = df["ts"].idxmax()
            row = df.loc[idx]
            if int(row["ts"]) > best_ts:
                best_ts = int(row["ts"])
                best_id = str(row["trade_id"])
        return best_id

    # -------------------- 内部 --------------------

    def _path_for(self, inst_id: str, date: str) -> Path:
        validate_inst_id(inst_id)
        inst_type = self._infer_type(inst_id)
        return self._root / "trades" / inst_type / inst_id / f"{date}.parquet"

    @staticmethod
    def _date_of(ts_ms: int) -> str:
        """UTC 日期 YYYY-MM-DD。"""
        return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.UTC).strftime("%Y-%m-%d")

    @staticmethod
    def _infer_type(inst_id: str) -> str:
        """从 inst_id 推断 inst_type（spec §6.1 目录结构）。"""
        parts = inst_id.split("-")
        if len(parts) >= 2 and parts[-1] == "SWAP":
            return "swap"
        if len(parts) >= 3 and parts[-1].isdigit():
            return "futures"
        if len(parts) >= 5 and parts[-1] in ("C", "P"):
            return "option"
        return "spot"

    @staticmethod
    def _days_range(start_ts: int, end_ts: int) -> list[str]:
        """[start_ts, end_ts] 覆盖的 UTC 日期列表（升序）。"""
        out: list[str] = []
        cur = dt.datetime.fromtimestamp(start_ts / 1000, tz=dt.UTC).date()
        end = dt.datetime.fromtimestamp(end_ts / 1000, tz=dt.UTC).date()
        while cur <= end:
            out.append(cur.strftime("%Y-%m-%d"))
            cur += dt.timedelta(days=1)
        return out

    @staticmethod
    def _to_df(trades: list[Trade]) -> pd.DataFrame:
        """Trade 列表转 DataFrame（Decimal 存为 string 以保精度）。"""
        return pd.DataFrame(
            [
                {
                    "ts": t.ts,
                    "inst_id": t.inst_id,
                    "trade_id": t.trade_id,
                    "px": str(t.px),
                    "sz": str(t.sz),
                    "side": t.side,
                }
                for t in trades
            ]
        )

    @staticmethod
    def _row_to_trade(row: Any, inst_id: str) -> Trade:
        """行转 Trade（Decimal 从 string 还原）。"""
        return Trade(
            ts=int(row["ts"]),
            inst_id=inst_id,
            trade_id=str(row["trade_id"]),
            px=Decimal(str(row["px"])),
            sz=Decimal(str(row["sz"])),
            side=str(row["side"]),
        )


__all__ = ["TradeStorage", "validate_inst_id"]
