"""Matcher 成交模拟单元测试（spec §4.4）。

覆盖：
- 市价单：下一根开盘价成交 + 买/卖滑点
- 限价单：穿透成交（成交价=限价）/ 未穿透抛 NoFillError
- 手续费计算
- 构造参数校验、品种一致性校验
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.matcher import Matcher
from quant.backtest.signal import Signal
from quant.foundation.errors import NoFillError
from quant.models.bar import Bar


def _bar(
    ts: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    inst_id: str = "BTC-USDT",
) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        vol=Decimal("1"),
        vol_quote=Decimal("1"),
        confirm=1,
        inst_id=inst_id,
        bar_size="1H",
    )


def _signal(
    side: str = "buy",
    order_type: str = "market",
    qty: str = "1",
    price: str | None = None,
    inst_id: str = "BTC-USDT",
    ts: int = 1_000,
) -> Signal:
    return Signal(
        strategy_id="test",
        side=side,  # type: ignore[arg-type]
        order_type=order_type,  # type: ignore[arg-type]
        inst_id=inst_id,
        qty=Decimal(qty),
        ts=ts,
        price=Decimal(price) if price is not None else None,
    )


# =====================================================================
# 构造与参数校验
# =====================================================================


class TestMatcherInit:
    def test_defaults(self) -> None:
        m = Matcher()
        assert m.slippage_rate == Decimal("0")
        assert m.commission_rate == Decimal("0.001")

    def test_negative_slippage_raises(self) -> None:
        with pytest.raises(ValueError, match="slippage_rate"):
            Matcher(slippage_rate=Decimal("-0.001"))

    def test_slippage_ge_one_raises(self) -> None:
        with pytest.raises(ValueError, match="slippage_rate"):
            Matcher(slippage_rate=Decimal("1"))

    def test_negative_commission_raises(self) -> None:
        with pytest.raises(ValueError, match="commission_rate"):
            Matcher(commission_rate=Decimal("-0.001"))


# =====================================================================
# 市价单
# =====================================================================


class TestMarketMatch:
    def test_buy_at_next_open(self) -> None:
        """市价买入按下一根开盘价成交，手续费 = price*qty*rate。"""
        m = Matcher(commission_rate=Decimal("0.001"))
        fill = m.match(_signal(side="buy", qty="2"), _bar(2_000, "100", "110", "90", "105"))

        assert fill.price == Decimal("100")
        assert fill.qty == Decimal("2")
        assert fill.fee == Decimal("0.2")
        assert fill.ts == 2_000
        assert fill.side == "buy"
        assert fill.inst_id == "BTC-USDT"
        assert fill.order_id

    def test_sell_at_next_open(self) -> None:
        m = Matcher(commission_rate=Decimal("0.001"))
        fill = m.match(_signal(side="sell", qty="1"), _bar(2_000, "100", "110", "90", "105"))

        assert fill.price == Decimal("100")
        assert fill.fee == Decimal("0.1")

    def test_buy_slippage_increases_price(self) -> None:
        """买入滑点抬价：price = open * (1 + slippage_rate)。"""
        m = Matcher(
            slippage_rate=Decimal("0.0005"),  # 5bp
            commission_rate=Decimal("0"),
        )
        fill = m.match(_signal(side="buy"), _bar(2_000, "100", "110", "90", "105"))

        assert fill.price == Decimal("100") * Decimal("1.0005")

    def test_sell_slippage_decreases_price(self) -> None:
        """卖出滑点压价：price = open * (1 - slippage_rate)。"""
        m = Matcher(
            slippage_rate=Decimal("0.0005"),
            commission_rate=Decimal("0"),
        )
        fill = m.match(_signal(side="sell"), _bar(2_000, "100", "110", "90", "105"))

        assert fill.price == Decimal("100") * Decimal("0.9995")

    def test_zero_commission(self) -> None:
        m = Matcher(commission_rate=Decimal("0"))
        fill = m.match(_signal(), _bar(2_000, "100", "110", "90", "105"))
        assert fill.fee == Decimal("0")


# =====================================================================
# 限价单
# =====================================================================


class TestLimitMatch:
    def test_buy_touched_fills_at_limit(self) -> None:
        """买入限价单：下一根 low <= 限价 → 成交价=限价。"""
        m = Matcher(commission_rate=Decimal("0"))
        signal = _signal(side="buy", order_type="limit", price="95")
        fill = m.match(signal, _bar(2_000, "100", "110", "90", "105"))

        assert fill.price == Decimal("95")

    def test_buy_not_touched_raises(self) -> None:
        """买入限价单：下一根 low > 限价 → 未穿透。"""
        m = Matcher()
        signal = _signal(side="buy", order_type="limit", price="85")
        with pytest.raises(NoFillError, match="not touched"):
            m.match(signal, _bar(2_000, "100", "110", "90", "105"))

    def test_buy_touched_exactly_at_limit(self) -> None:
        """边界：low == 限价 视为穿透。"""
        m = Matcher(commission_rate=Decimal("0"))
        signal = _signal(side="buy", order_type="limit", price="90")
        fill = m.match(signal, _bar(2_000, "100", "110", "90", "105"))
        assert fill.price == Decimal("90")

    def test_sell_touched_fills_at_limit(self) -> None:
        """卖出限价单：下一根 high >= 限价 → 成交价=限价。"""
        m = Matcher(commission_rate=Decimal("0"))
        signal = _signal(side="sell", order_type="limit", price="108")
        fill = m.match(signal, _bar(2_000, "100", "110", "90", "105"))

        assert fill.price == Decimal("108")

    def test_sell_not_touched_raises(self) -> None:
        m = Matcher()
        signal = _signal(side="sell", order_type="limit", price="120")
        with pytest.raises(NoFillError):
            m.match(signal, _bar(2_000, "100", "110", "90", "105"))

    def test_sell_touched_exactly_at_limit(self) -> None:
        m = Matcher(commission_rate=Decimal("0"))
        signal = _signal(side="sell", order_type="limit", price="110")
        fill = m.match(signal, _bar(2_000, "100", "110", "90", "105"))
        assert fill.price == Decimal("110")

    def test_limit_fee(self) -> None:
        m = Matcher(commission_rate=Decimal("0.001"))
        signal = _signal(side="buy", order_type="limit", price="95", qty="2")
        fill = m.match(signal, _bar(2_000, "100", "110", "90", "105"))
        assert fill.fee == Decimal("95") * Decimal("2") * Decimal("0.001")


# =====================================================================
# 一致性校验
# =====================================================================


class TestMatchValidation:
    def test_inst_mismatch_raises(self) -> None:
        m = Matcher()
        signal = _signal(inst_id="BTC-USDT")
        bar = _bar(2_000, "100", "110", "90", "105", inst_id="ETH-USDT")
        with pytest.raises(ValueError, match="inst_id"):
            m.match(signal, bar)
