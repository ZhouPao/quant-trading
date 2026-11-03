"""Models 单元测试（spec §5）。

覆盖：
- Bar: 正常构造 + from_okx_raw + OHLC 校验 + confirm + is_closed
- Tick: bid/ask 顺序校验 + 价格正性
- Instrument: SPOT/SWAP 区分 + 精度字段正性 + ct_val 规则
- FundingRate: rate 符号自由 + |rate|<1 边界
- frozen: 不可变性验证
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from quant.models import Bar, FundingRate, Instrument, Tick

# -------------------- Bar --------------------


def _make_bar(
    ts: int = 1700000000000,
    open_: str = "50000",
    high: str = "50500",
    low: str = "49800",
    close: str = "50200",
    vol: str = "100.5",
    vol_quote: str = "5000000",
    confirm: int = 1,
    inst_id: str = "BTC-USDT",
    bar_size: str = "1H",
) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        vol=Decimal(vol),
        vol_quote=Decimal(vol_quote),
        confirm=confirm,
        inst_id=inst_id,
        bar_size=bar_size,
    )


def test_bar_construction_normal():
    b = _make_bar()
    assert b.ts == 1700000000000
    assert b.open == Decimal("50000")
    assert b.is_closed() is True


def test_bar_is_closed_false_when_confirm_0():
    b = _make_bar(confirm=0)
    assert b.is_closed() is False


def test_bar_from_okx_raw_normal():
    # OKX 实际返回的 9 元素 list[str]
    raw = [
        "1700000000000",  # ts
        "50000.0",  # o
        "50500.5",  # h
        "49800.2",  # l
        "50200.3",  # c
        "100.5",  # vol
        "999",  # volCcy（被忽略）
        "5000000.7",  # volCcyQuote
        "1",  # confirm
    ]
    b = Bar.from_okx_raw(raw, inst_id="BTC-USDT", bar_size="1H")
    assert b.ts == 1700000000000
    assert b.open == Decimal("50000.0")
    assert b.high == Decimal("50500.5")
    assert b.low == Decimal("49800.2")
    assert b.close == Decimal("50200.3")
    assert b.vol == Decimal("100.5")
    assert b.vol_quote == Decimal("5000000.7")
    assert b.confirm == 1
    assert b.inst_id == "BTC-USDT"
    assert b.bar_size == "1H"


def test_bar_from_okx_raw_swap_vol_uses_volccy():
    """SWAP 的 idx5 vol 单位是张，基础币量在 idx6 volCcy（= 张数×ctVal）。

    统一成基础币口径，否则合约成交量与现货差 ctVal 倍（P1 口径）。
    """
    raw = [
        "1700000000000",
        "50000.0",
        "50500.5",
        "49500.0",
        "50200.0",
        "1234",  # vol（张数）
        "12.34",  # volCcy（基础币量，1234 张 × ctVal=0.01）
        "620000.7",  # volCcyQuote
        "1",
    ]
    b = Bar.from_okx_raw(raw, inst_id="BTC-USDT-SWAP", bar_size="1H")
    assert b.vol == Decimal("12.34")
    assert b.vol_quote == Decimal("620000.7")


def test_bar_from_okx_raw_too_short():
    with pytest.raises(ValueError, match=">=9 elements"):
        Bar.from_okx_raw(["1", "2"], inst_id="BTC-USDT", bar_size="1H")


def test_bar_high_lt_low_rejected():
    with pytest.raises(ValueError, match="high .* must be >= low"):
        _make_bar(high="40000", low="50000")


def test_bar_open_outside_range_rejected():
    with pytest.raises(ValueError, match="open .* must be within"):
        _make_bar(open_="60000", high="50500", low="49800")  # open > high


def test_bar_close_below_low_rejected():
    with pytest.raises(ValueError, match="close .* must be within"):
        _make_bar(close="40000", low="49800")  # close < low


def test_bar_negative_ts_rejected():
    with pytest.raises(ValueError, match="ts must be non-negative"):
        _make_bar(ts=-1)


def test_bar_negative_vol_rejected():
    with pytest.raises(ValueError, match="vol must be non-negative"):
        _make_bar(vol="-1")


def test_bar_invalid_confirm_rejected():
    with pytest.raises(ValueError, match="confirm must be 0 or 1"):
        _make_bar(confirm=2)


def test_bar_frozen_immutable():
    b = _make_bar()
    with pytest.raises(FrozenInstanceError):
        b.close = Decimal("99999")  # type: ignore[misc]


def test_bar_slots_memory_efficient():
    """slots=True 时不应有 __dict__。"""
    b = _make_bar()
    assert not hasattr(b, "__dict__")


# -------------------- Tick --------------------


def _make_tick(
    ts: int = 1700000000000,
    bid_px: str = "50000",
    bid_sz: str = "1.5",
    ask_px: str = "50010",
    ask_sz: str = "2.0",
    last_px: str = "50005",
    last_sz: str = "0.5",
) -> Tick:
    return Tick(
        ts=ts,
        inst_id="BTC-USDT",
        bid_px=Decimal(bid_px),
        bid_sz=Decimal(bid_sz),
        ask_px=Decimal(ask_px),
        ask_sz=Decimal(ask_sz),
        last_px=Decimal(last_px),
        last_sz=Decimal(last_sz),
    )


def test_tick_construction_normal():
    t = _make_tick()
    assert t.bid_px == Decimal("50000")
    assert t.ask_px == Decimal("50010")


def test_tick_bid_gt_ask_rejected():
    with pytest.raises(ValueError, match="bid_px .* must be <= ask_px"):
        _make_tick(bid_px="50020", ask_px="50010")


def test_tick_zero_bid_px_rejected():
    with pytest.raises(ValueError, match="bid_px must be positive"):
        _make_tick(bid_px="0")


def test_tick_negative_sz_rejected():
    with pytest.raises(ValueError, match="bid_sz must be non-negative"):
        _make_tick(bid_sz="-1")


def test_tick_bid_eq_ask_allowed():
    """bid == ask 是允许的（流动性极低时可能出现）。"""
    t = _make_tick(bid_px="50000", ask_px="50000")
    assert t.bid_px == t.ask_px


def test_tick_frozen_immutable():
    t = _make_tick()
    with pytest.raises(FrozenInstanceError):
        t.last_px = Decimal("99999")  # type: ignore[misc]


# -------------------- Instrument --------------------


def _make_spot_instrument(
    inst_id: str = "BTC-USDT",
    state: str = "live",
) -> Instrument:
    return Instrument(
        inst_id=inst_id,
        inst_type="SPOT",
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy="",
        tick_sz=Decimal("0.1"),
        lot_sz=Decimal("0.00001"),
        min_sz=Decimal("0.00001"),
        ct_val=Decimal("1"),
        state=state,
        list_time=1600000000000,
    )


def _make_swap_instrument() -> Instrument:
    return Instrument(
        inst_id="BTC-USDT-SWAP",
        inst_type="SWAP",
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy="USDT",
        tick_sz=Decimal("0.1"),
        lot_sz=Decimal("0.00001"),
        min_sz=Decimal("0.00001"),
        ct_val=Decimal("0.01"),
        state="live",
        list_time=1600000000000,
    )


def test_instrument_spot_normal():
    inst = _make_spot_instrument()
    assert inst.is_spot() is True
    assert inst.is_swap() is False
    assert inst.is_live() is True


def test_instrument_swap_normal():
    inst = _make_swap_instrument()
    assert inst.is_swap() is True
    assert inst.is_spot() is False
    assert inst.settle_ccy == "USDT"
    assert inst.ct_val == Decimal("0.01")


def test_instrument_spot_settle_ccy_must_be_empty():
    with pytest.raises(ValueError, match="SPOT.*settle_ccy must be empty"):
        Instrument(
            inst_id="BTC-USDT",
            inst_type="SPOT",
            base_ccy="BTC",
            quote_ccy="USDT",
            settle_ccy="USDT",  # SPOT 不该有
            tick_sz=Decimal("0.1"),
            lot_sz=Decimal("0.00001"),
            min_sz=Decimal("0.00001"),
            ct_val=Decimal("1"),
            state="live",
            list_time=1600000000000,
        )


def test_instrument_spot_ct_val_must_be_1():
    with pytest.raises(ValueError, match="SPOT ct_val must be 1"):
        Instrument(
            inst_id="BTC-USDT",
            inst_type="SPOT",
            base_ccy="BTC",
            quote_ccy="USDT",
            settle_ccy="",
            tick_sz=Decimal("0.1"),
            lot_sz=Decimal("0.00001"),
            min_sz=Decimal("0.00001"),
            ct_val=Decimal("0.01"),  # SPOT 必须为 1
            state="live",
            list_time=1600000000000,
        )


def test_instrument_unknown_type_rejected():
    with pytest.raises(ValueError, match="unknown inst_type"):
        Instrument(
            inst_id="X-XXX",
            inst_type="WEIRD",
            base_ccy="X",
            quote_ccy="XXX",
            settle_ccy="",
            tick_sz=Decimal("0.1"),
            lot_sz=Decimal("0.00001"),
            min_sz=Decimal("0.00001"),
            ct_val=Decimal("1"),
            state="live",
            list_time=1600000000000,
        )


def test_instrument_zero_tick_sz_rejected():
    with pytest.raises(ValueError, match="tick_sz must be positive"):
        _make_spot_instrument().__class__(
            inst_id="BTC-USDT",
            inst_type="SPOT",
            base_ccy="BTC",
            quote_ccy="USDT",
            settle_ccy="",
            tick_sz=Decimal("0"),
            lot_sz=Decimal("0.00001"),
            min_sz=Decimal("0.00001"),
            ct_val=Decimal("1"),
            state="live",
            list_time=1600000000000,
        )


def test_instrument_empty_inst_id_rejected():
    with pytest.raises(ValueError, match="inst_id must not be empty"):
        _make_spot_instrument().__class__(
            inst_id="",
            inst_type="SPOT",
            base_ccy="BTC",
            quote_ccy="USDT",
            settle_ccy="",
            tick_sz=Decimal("0.1"),
            lot_sz=Decimal("0.00001"),
            min_sz=Decimal("0.00001"),
            ct_val=Decimal("1"),
            state="live",
            list_time=1600000000000,
        )


def test_instrument_frozen_immutable():
    inst = _make_spot_instrument()
    with pytest.raises(FrozenInstanceError):
        inst.state = "canceled"  # type: ignore[misc]


# -------------------- FundingRate --------------------


def _make_funding_rate(
    rate: str = "0.0001",
    next_funding_ts: int = 1700000000000 + 28800000,  # 8h later
) -> FundingRate:
    return FundingRate(
        inst_id="BTC-USDT-SWAP",
        ts=1700000000000,
        rate=Decimal(rate),
        next_funding_ts=next_funding_ts,
    )


def test_funding_rate_normal_positive():
    f = _make_funding_rate(rate="0.0001")
    assert f.rate == Decimal("0.0001")
    assert f.next_funding_ts > f.ts


def test_funding_rate_negative_allowed():
    """rate 可以为负（空付多）。"""
    f = _make_funding_rate(rate="-0.0002")
    assert f.rate == Decimal("-0.0002")


def test_funding_rate_zero_allowed():
    f = _make_funding_rate(rate="0")
    assert f.rate == Decimal("0")


def test_funding_rate_abs_geq_1_rejected():
    """|rate| >= 1 视为异常。"""
    with pytest.raises(ValueError, match="rate .* abnormal"):
        _make_funding_rate(rate="1.0")
    with pytest.raises(ValueError, match="rate .* abnormal"):
        _make_funding_rate(rate="-1.5")


def test_funding_rate_negative_ts_rejected():
    with pytest.raises(ValueError, match="ts must be non-negative"):
        FundingRate(
            inst_id="BTC-USDT-SWAP",
            ts=-1,
            rate=Decimal("0.0001"),
            next_funding_ts=1700000000000,
        )


def test_funding_rate_empty_inst_id_rejected():
    with pytest.raises(ValueError, match="inst_id must not be empty"):
        FundingRate(
            inst_id="",
            ts=1700000000000,
            rate=Decimal("0.0001"),
            next_funding_ts=1700000000000,
        )


def test_funding_rate_frozen_immutable():
    f = _make_funding_rate()
    with pytest.raises(FrozenInstanceError):
        f.rate = Decimal("0.5")  # type: ignore[misc]


# -------------------- 跨模型：Decimal 精度 --------------------


def test_decimal_precision_no_float_error():
    """关键场景：用 Decimal 避免 0.1+0.2 != 0.3 的精度问题。"""
    b1 = _make_bar(open_="0.1", high="0.3", low="0.1", close="0.2")
    # 如果用 float 会得到 0.30000000000000004，Decimal 不会有此问题
    assert b1.high - b1.low == Decimal("0.2")
    assert b1.high == Decimal("0.3")
