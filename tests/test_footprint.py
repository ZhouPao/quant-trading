"""footprint 聚合单测（M2-14 批3）。

验证：分档归属、买卖量/delta、POC、累计 delta、退化单档、区间外成交跳过。
"""
from __future__ import annotations

from decimal import Decimal

from quant.footprint import aggregate_footprint
from quant.models.bar import Bar
from quant.models.trade import Trade


def _bar(ts: int, o: int = 100, h: int = 110, lo: int = 100, c: int = 105,
         vol: str = "100") -> Bar:
    return Bar(
        ts=ts, open=Decimal(o), high=Decimal(h), low=Decimal(lo), close=Decimal(c),
        vol=Decimal(vol), vol_quote=Decimal("0"), confirm=1,
        inst_id="BTC-USDT-SWAP", bar_size="1m",
    )


def _trade(ts: int, px: str, sz: str, side: str) -> Trade:
    return Trade(
        ts=ts, inst_id="BTC-USDT-SWAP", trade_id=f"t{ts}{side}{px}",
        px=Decimal(px), sz=Decimal(sz), side=side,
    )


def test_aggregate_basic_levels_and_delta() -> None:
    bars = [_bar(1_000_000), _bar(1_060_000)]
    trades = [
        _trade(1_000_100, "105", "2", "sell"),  # 档 idx5 bid=2
        _trade(1_000_200, "105", "3", "buy"),   # 档 idx5 ask=3
        _trade(1_060_100, "108", "5", "buy"),   # bar1 档 idx8 ask=5
    ]
    out = aggregate_footprint(trades, bars, num_levels=10)
    assert len(out) == 2

    b0 = out[0]
    # 档宽=(110-100)/10=1；px=105 -> idx=5
    lvl5 = b0.levels[5]
    assert lvl5.bid_vol == Decimal("2")
    assert lvl5.ask_vol == Decimal("3")
    assert lvl5.delta == Decimal("1")
    assert b0.poc_price == Decimal("105.5")  # 中点
    assert b0.imbalance == Decimal("1")       # 净 delta
    assert b0.cum_delta == Decimal("1")       # 首 bar 累计（含本 bar）

    b1 = out[1]
    assert b1.levels[8].ask_vol == Decimal("5")
    assert b1.imbalance == Decimal("5")
    assert b1.cum_delta == Decimal("6")        # 1 + 5


def test_poc_and_degenerate_single_level() -> None:
    # high==low 退化：单档
    bars = [_bar(1_000_000, o=200, h=200, lo=200, c=200)]
    trades = [_trade(1_000_100, "200", "4", "sell")]
    out = aggregate_footprint(trades, bars, num_levels=10)
    assert out[0].poc_vol == Decimal("4")
    assert out[0].levels[0].bid_vol == Decimal("4")


def test_trade_outside_bar_skipped() -> None:
    bars = [_bar(1_000_000)]
    trades = [_trade(999_000, "105", "1", "buy")]  # ts 不在 bar 区间
    out = aggregate_footprint(trades, bars, num_levels=10)
    assert out[0].imbalance == Decimal("0")  # 被跳过


def test_bar_size_case_insensitive() -> None:
    assert aggregate_footprint([], [_bar(1_000_000)], num_levels=10)
    from quant.footprint import bar_ms
    assert bar_ms("1H") == 3_600_000
    assert bar_ms("1h") == 3_600_000


def test_empty_levels_for_no_trades() -> None:
    bars = [_bar(1_000_000)]
    out = aggregate_footprint([], bars, num_levels=10)
    assert out[0].imbalance == Decimal("0")
    assert out[0].poc_vol == Decimal("0")
    assert len(out[0].levels) == 10
