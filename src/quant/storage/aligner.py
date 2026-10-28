"""多品种对齐器（spec §6.6）。

将多个 inst_id 的 K线列表按统一时间轴对齐，输出多级列 DataFrame。
支持三种缺失填充策略：ffill / nan / interpolate。
"""
from __future__ import annotations

import pandas as pd

from quant.models.bar import Bar


class DataAligner:
    """多品种 K线时间轴对齐器。"""

    def align(
        self,
        bars_by_inst: dict[str, list[Bar]],
        fill: str = "ffill",
    ) -> pd.DataFrame:
        """多品种 K线按统一时间轴对齐。

        Args:
            bars_by_inst: {inst_id: [Bar, ...]}
            fill: 缺失行填充策略
                - "ffill": 用前值填充（适合价格）
                - "nan": 留空
                - "interpolate": 按时间线性插值

        Returns:
            DataFrame：index=ts，列为 inst_id_field（如 BTC-USDT_close）
        """
        if not bars_by_inst:
            return pd.DataFrame()

        # 各品种转 DataFrame
        dfs: list[pd.DataFrame] = []
        for inst_id, bars in bars_by_inst.items():
            df = self._bars_to_df(bars).add_prefix(f"{inst_id}_")
            dfs.append(df)

        # 外连接得到统一时间轴
        merged = pd.concat(dfs, axis=1)

        if fill == "ffill":
            merged = merged.ffill()
        elif fill == "interpolate":
            # 用 linear 按行号插值；对等间隔 K线等价于按时间插值。
            # 若未来要支持不等间隔的时间加权，需要把 int ms 索引转 DatetimeIndex。
            merged = merged.interpolate(method="linear")
        # "nan" 时不填充

        # 时间索引升序
        merged = merged.sort_index()
        return merged

    @staticmethod
    def _bars_to_df(bars: list[Bar]) -> pd.DataFrame:
        """Bar 列表转 DataFrame（Decimal 存为 float 以便数值运算）。

        index=ts，列为 open/high/low/close/vol。
        """
        if not bars:
            return pd.DataFrame()
        rows = []
        for b in bars:
            rows.append({
                "ts": b.ts,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "vol": float(b.vol),
            })
        df = pd.DataFrame(rows).set_index("ts").sort_index()
        # 同 ts 去重（保留最后）
        df = df[~df.index.duplicated(keep="last")]
        return df


__all__ = ["DataAligner"]
