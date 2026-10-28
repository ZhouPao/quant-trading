"""Bar 数据模型（spec §5.2）。

代表单根 K 线，所有数值字段用 Decimal 保证精度，时间用 int UTC ms。
不可变 + slots，内存友好，可在多线程间安全传递。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Bar:
    """单根 K 线。

    字段对齐 OKX `/api/v5/market/history-candles` 与 `/candles` 返回顺序：
    [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]

    Attributes:
        ts: 开盘时间戳 UTC ms（OKX 返回的是开盘时间，非收盘）
        open: 开盘价
        high: 最高价
        low: 最低价
        close: 收盘价
        vol: 基础币成交量（如 BTC 数量）
        vol_quote: 计价币成交额（如 USDT 金额；对应 OKX volCcyQuote）
        confirm: 0=未结束（当前正在形成的K线），1=已结束
        inst_id: 产品ID，如 "BTC-USDT"
        bar_size: K线周期，OKX 命名如 "1m"/"5m"/"1H"/"1D"
    """

    ts: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vol: Decimal
    vol_quote: Decimal
    confirm: int
    inst_id: str
    bar_size: str

    def is_closed(self) -> bool:
        """该 K 线是否已结束（收盘）。"""
        return self.confirm == 1

    def __post_init__(self) -> None:
        """构造时校验：OHLC 合法性 + ts 非负。"""
        if self.ts < 0:
            raise ValueError(f"ts must be non-negative, got {self.ts}")
        if self.high < self.low:
            raise ValueError(
                f"high ({self.high}) must be >= low ({self.low}) for {self.inst_id}@{self.ts}"
            )
        if self.open < self.low or self.open > self.high:
            raise ValueError(
                f"open ({self.open}) must be within [{self.low}, {self.high}] "
                f"for {self.inst_id}@{self.ts}"
            )
        if self.close < self.low or self.close > self.high:
            raise ValueError(
                f"close ({self.close}) must be within [{self.low}, {self.high}] "
                f"for {self.inst_id}@{self.ts}"
            )
        if self.vol < 0:
            raise ValueError(f"vol must be non-negative, got {self.vol}")
        if self.vol_quote < 0:
            raise ValueError(f"vol_quote must be non-negative, got {self.vol_quote}")
        if self.confirm not in (0, 1):
            raise ValueError(f"confirm must be 0 or 1, got {self.confirm}")

    @classmethod
    def from_okx_raw(
        cls,
        raw: list[str],
        inst_id: str,
        bar_size: str,
    ) -> Bar:
        """从 OKX REST 响应的原始 list[str] 构造 Bar。

        OKX 字段顺序：[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        索引 6 是 volCcy（基础币额），7 是 volCcyQuote（计价币额）。
        本类 vol 字段对应索引 5（基础币成交量），vol_quote 对应索引 7。
        """
        if len(raw) < 9:
            raise ValueError(
                f"OKX raw candle must have >=9 elements, got {len(raw)}: {raw}"
            )
        return cls(
            ts=int(raw[0]),
            open=Decimal(raw[1]),
            high=Decimal(raw[2]),
            low=Decimal(raw[3]),
            close=Decimal(raw[4]),
            vol=Decimal(raw[5]),
            vol_quote=Decimal(raw[7]),
            confirm=int(raw[8]),
            inst_id=inst_id,
            bar_size=bar_size,
        )


__all__ = ["Bar"]
