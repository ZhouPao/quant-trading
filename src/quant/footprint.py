"""订单流足迹图聚合（M2-14 批3）。

纯函数：输入逐笔成交 + K 线 bar 边界，按 bar × 自适应价格档聚合出 footprint
矩阵（每档主动买/卖量、delta、总量），bar 级 POC / 净 delta / 累计 delta。

不依赖 IO，便于单测与 service 层复用。

约定（ATAS 惯例）：
- side='buy'（主动买 / taker buy）→ 记为 ask 量（吃掉卖单，显示在档右侧/绿）
- side='sell'（主动卖 / taker sell）→ 记为 bid 量（吃掉买单，显示在档左侧/红）
- delta = ask_vol - bid_vol；bar 净 delta 即失衡代理（>0 买控，<0 卖控）
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant.models.bar import Bar
from quant.models.trade import Trade

# bar_size(大小写归一) -> 毫秒
_BAR_MS = {
    "1M": 60_000, "3M": 180_000, "5M": 300_000, "15M": 900_000,
    "30M": 1_800_000, "1H": 3_600_000, "2H": 7_200_000, "4H": 14_400_000,
    "6H": 21_600_000, "12H": 43_200_000, "1D": 86_400_000, "1W": 604_800_000,
}


def bar_ms(bar_size: str) -> int:
    """bar_size（如 '1H'/'1h'/'1m'）转毫秒。"""
    v = _BAR_MS.get(bar_size) or _BAR_MS.get(bar_size.upper())
    if v is None:
        raise ValueError(f"unsupported bar_size: {bar_size!r}")
    return v


@dataclass(frozen=True, slots=True)
class FootprintLevel:
    """单个价格档的聚合结果。"""

    price: Decimal       # 档中点价
    bid_vol: Decimal     # 主动卖量（taker sell）
    ask_vol: Decimal     # 主动买量（taker buy）
    delta: Decimal       # ask_vol - bid_vol
    total: Decimal       # bid_vol + ask_vol


@dataclass(frozen=True, slots=True)
class FootprintBar:
    """单个 bar 的 footprint 聚合。"""

    ts: int
    inst_id: str
    bar_size: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vol: Decimal
    vol_quote: Decimal
    levels: tuple[FootprintLevel, ...]
    poc_price: Decimal   # 最大量档中点（POC）
    poc_vol: Decimal     # POC 对应量
    imbalance: Decimal   # bar 净 delta（买控为正，卖控为负）
    cum_delta: Decimal   # 跨 bar 累计 delta（含本 bar）


def aggregate_footprint(
    trades: list[Trade], bars: list[Bar], num_levels: int = 20
) -> list[FootprintBar]:
    """按 bar × 自适应分档聚合 footprint。

    - 每 bar 内按 [low, high] 均分 num_levels 档（档宽 = (high-low)/num_levels）
    - trade.px 落入对应档；bid_vol(sell)/ask_vol(buy) 累加
    - POC = 量最大档；imbalance = bar 净 delta；cum_delta 按 bar 顺序累计（含本 bar）

    trades 中不属于任何 bar 时间区间的会被跳过（边界外成交不参与）。
    """
    if num_levels <= 0:
        raise ValueError("num_levels must be positive")

    spans = sorted(
        ((b.ts, b.ts + bar_ms(b.bar_size), b) for b in bars), key=lambda x: x[0]
    )
    grouped: dict[int, list[Trade]] = {}
    for t in trades:
        idx = _bar_index(t.ts, spans)
        if idx is not None:
            grouped.setdefault(idx, []).append(t)

    out: list[FootprintBar] = []
    cum = Decimal(0)
    for i, (_s, _e, bar) in enumerate(spans):
        fp = _aggregate_one_bar(bar, grouped.get(i, []), num_levels, running_cum=cum)
        cum += fp.imbalance
        out.append(fp)
    return out


def _bar_index(ts: int, spans: list[tuple[int, int, Bar]]) -> int | None:
    """线性查找 trade 所属 bar（bars 通常几十~几百，足够）。"""
    for i, (s, e, _b) in enumerate(spans):
        if s <= ts < e:
            return i
    return None


def _aggregate_one_bar(
    bar: Bar, trades: list[Trade], num_levels: int, running_cum: Decimal
) -> FootprintBar:
    low, high = bar.low, bar.high
    # 退化：high==low（doji 一字）或异常，归为单档
    if high <= low:
        step = Decimal(1)
        n = 1
    else:
        step = (high - low) / Decimal(num_levels)
        n = num_levels

    sums: list[tuple[Decimal, Decimal]] = [
        (Decimal(0), Decimal(0)) for _ in range(n)
    ]  # (bid_vol, ask_vol)
    for t in trades:
        idx = _level_index(t.px, low, step, n)
        if idx is None:
            continue
        bid, ask = sums[idx]
        if t.side == "sell":
            sums[idx] = (bid + t.sz, ask)
        else:  # buy -> ask
            sums[idx] = (bid, ask + t.sz)

    levels: list[FootprintLevel] = []
    for i in range(n):
        bid, ask = sums[i]
        levels.append(
            FootprintLevel(
                price=low + step * (i + Decimal("0.5")),
                bid_vol=bid,
                ask_vol=ask,
                delta=ask - bid,
                total=bid + ask,
            )
        )

    if levels:
        poc_idx = max(range(n), key=lambda i: levels[i].total)
        poc = levels[poc_idx]
        poc_price, poc_vol = poc.price, poc.total
    else:  # 无成交流（空 bar），POC 落在 low
        poc_price, poc_vol = low, Decimal(0)

    net = sum((ask - bid) for bid, ask in sums)
    return FootprintBar(
        ts=bar.ts,
        inst_id=bar.inst_id,
        bar_size=bar.bar_size,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        vol=bar.vol,
        vol_quote=bar.vol_quote,
        levels=tuple(levels),
        poc_price=poc_price,
        poc_vol=poc_vol,
        imbalance=net,
        cum_delta=running_cum + net,
    )


def _level_index(px: Decimal, low: Decimal, step: Decimal, n: int) -> int | None:
    """px 落入的档索引（0-based）；退化单档时返回 0。"""
    if step <= 0:
        return 0 if low <= px <= low else None
    idx = int((px - low) / step)
    return idx if 0 <= idx < n else None


__all__ = ["FootprintBar", "FootprintLevel", "aggregate_footprint", "bar_ms"]
