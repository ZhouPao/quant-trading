"""Strategy + 数据模型单元测试（spec §4.1-§4.2）。

覆盖：
- Signal 构造/校验/序列化往返
- Order 构造/校验
- Fill 构造/校验/value/net_value/序列化
- Position apply_fill（开仓/加仓/减仓/反手/空头/空仓边界）
- Context 状态读取
- Strategy ABC 强制实现 + 生命周期回调

测试策略：
- 不依赖外部 OKX/Redis，纯内存
- 用 Decimal 精确比较
- 每个测试聚焦一个行为
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.backtest.context import Context
from quant.backtest.order import Fill, Order
from quant.backtest.position import Position
from quant.backtest.signal import Signal
from quant.backtest.strategy import Strategy
from quant.models.bar import Bar

# =====================================================================
# Signal 单元测试
# =====================================================================


class TestSignalConstruction:
    """Signal 构造与字段校验。"""

    def test_market_order_minimal(self) -> None:
        """市价单最小构造。"""
        s = Signal(
            strategy_id="sma_cross",
            side="buy",
            order_type="market",
            inst_id="BTC-USDT",
            qty=Decimal("0.001"),
            ts=1_700_000_000_000,
        )
        assert s.strategy_id == "sma_cross"
        assert s.side == "buy"
        assert s.order_type == "market"
        assert s.price is None
        assert s.signal_id == ""

    def test_limit_order_with_price(self) -> None:
        """限价单必须带 price。"""
        s = Signal(
            strategy_id="rsi",
            side="sell",
            order_type="limit",
            inst_id="BTC-USDT",
            qty=Decimal("0.5"),
            ts=1,
            price=Decimal("50000"),
        )
        assert s.price == Decimal("50000")

    def test_signal_id_default_empty(self) -> None:
        """signal_id 默认空字符串（回测模式）。"""
        s = Signal(
            strategy_id="x", side="buy", order_type="market",
            inst_id="BTC-USDT", qty=Decimal("1"), ts=1,
        )
        assert s.signal_id == ""

    def test_frozen_immutable(self) -> None:
        """Signal frozen=True 不可变。"""
        s = Signal(
            strategy_id="x", side="buy", order_type="market",
            inst_id="BTC-USDT", qty=Decimal("1"), ts=1,
        )
        with pytest.raises((AttributeError, Exception)):
            s.side = "sell"  # type: ignore[misc]


class TestSignalValidation:
    """Signal 字段校验。"""

    def test_empty_strategy_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="strategy_id"):
            Signal(strategy_id="", side="buy", order_type="market",
                   inst_id="BTC", qty=Decimal("1"), ts=1)

    def test_empty_inst_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="inst_id"):
            Signal(strategy_id="x", side="buy", order_type="market",
                   inst_id="", qty=Decimal("1"), ts=1)

    def test_invalid_side_rejected(self) -> None:
        with pytest.raises(ValueError, match="side"):
            Signal(strategy_id="x", side="hold", order_type="market",
                   inst_id="BTC", qty=Decimal("1"), ts=1)

    def test_invalid_order_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="order_type"):
            Signal(strategy_id="x", side="buy", order_type="stop",
                   inst_id="BTC", qty=Decimal("1"), ts=1)

    def test_zero_qty_rejected(self) -> None:
        with pytest.raises(ValueError, match="qty"):
            Signal(strategy_id="x", side="buy", order_type="market",
                   inst_id="BTC", qty=Decimal("0"), ts=1)

    def test_negative_qty_rejected(self) -> None:
        with pytest.raises(ValueError, match="qty"):
            Signal(strategy_id="x", side="buy", order_type="market",
                   inst_id="BTC", qty=Decimal("-1"), ts=1)

    def test_negative_ts_rejected(self) -> None:
        with pytest.raises(ValueError, match="ts"):
            Signal(strategy_id="x", side="buy", order_type="market",
                   inst_id="BTC", qty=Decimal("1"), ts=-1)

    def test_market_order_with_price_rejected(self) -> None:
        """市价单不能带 price。"""
        with pytest.raises(ValueError, match="market order must not have price"):
            Signal(strategy_id="x", side="buy", order_type="market",
                   inst_id="BTC", qty=Decimal("1"), ts=1, price=Decimal("100"))

    def test_limit_order_without_price_rejected(self) -> None:
        """限价单必须带 price。"""
        with pytest.raises(ValueError, match="limit order must have price"):
            Signal(strategy_id="x", side="buy", order_type="limit",
                   inst_id="BTC", qty=Decimal("1"), ts=1)

    def test_zero_price_rejected(self) -> None:
        """限价单 price 必须 > 0。"""
        with pytest.raises(ValueError, match="price"):
            Signal(strategy_id="x", side="buy", order_type="limit",
                   inst_id="BTC", qty=Decimal("1"), ts=1, price=Decimal("0"))


class TestSignalSerialization:
    """Signal JSON 序列化往返。"""

    def test_market_roundtrip(self) -> None:
        s = Signal(strategy_id="x", side="buy", order_type="market",
                   inst_id="BTC-USDT", qty=Decimal("0.001"), ts=42)
        j = s.to_json()
        assert j["qty"] == "0.001"
        assert j["price"] is None
        s2 = Signal.from_json(j)
        assert s2 == s

    def test_limit_roundtrip(self) -> None:
        s = Signal(strategy_id="x", side="sell", order_type="limit",
                   inst_id="BTC-USDT", qty=Decimal("1.5"), ts=42,
                   price=Decimal("50000.5"))
        j = s.to_json()
        assert j["price"] == "50000.5"
        s2 = Signal.from_json(j)
        assert s2 == s

    def test_signal_id_preserved(self) -> None:
        """signal_id 在 roundtrip 中保留。"""
        s = Signal(strategy_id="x", side="buy", order_type="market",
                   inst_id="BTC", qty=Decimal("1"), ts=1, signal_id="abc-123")
        j = s.to_json()
        s2 = Signal.from_json(j)
        assert s2.signal_id == "abc-123"

    def test_to_json_decimal_to_str(self) -> None:
        """to_json 中 Decimal 必须转 str 才能被 json.dumps 处理。"""
        import json
        s = Signal(strategy_id="x", side="buy", order_type="limit",
                   inst_id="BTC", qty=Decimal("0.001"), ts=1, price=Decimal("100"))
        # 不抛异常即可
        json.dumps(s.to_json())


# =====================================================================
# Order 单元测试
# =====================================================================


class TestOrderConstruction:
    """Order 构造与校验。"""

    def _make_signal(self) -> Signal:
        return Signal(strategy_id="x", side="buy", order_type="market",
                      inst_id="BTC-USDT", qty=Decimal("1"), ts=1)

    def test_minimal_order(self) -> None:
        o = Order(order_id="o1", signal=self._make_signal(),
                  status="pending", created_ts=1)
        assert o.status == "pending"

    def test_empty_order_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="order_id"):
            Order(order_id="", signal=self._make_signal(),
                  status="pending", created_ts=1)

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValueError, match="status"):
            Order(order_id="o1", signal=self._make_signal(),
                  status="unknown", created_ts=1)

    def test_negative_created_ts_rejected(self) -> None:
        with pytest.raises(ValueError, match="created_ts"):
            Order(order_id="o1", signal=self._make_signal(),
                  status="pending", created_ts=-1)


# =====================================================================
# Fill 单元测试
# =====================================================================


class TestFillConstruction:
    """Fill 构造与校验。"""

    def test_minimal_fill(self) -> None:
        f = Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                 qty=Decimal("1"), price=Decimal("100"),
                 fee=Decimal("0.1"), ts=1)
        assert f.value == Decimal("100")
        assert f.net_value == Decimal("100.1")  # buy: +value + fee

    def test_sell_net_value(self) -> None:
        """sell: net_value = -value - fee（资金流出）。"""
        f = Fill(order_id="o1", inst_id="BTC-USDT", side="sell",
                 qty=Decimal("1"), price=Decimal("100"),
                 fee=Decimal("0.1"), ts=1)
        assert f.net_value == Decimal("-100.1")

    def test_zero_fee_allowed(self) -> None:
        """fee=0 允许（免手续费场景）。"""
        f = Fill(order_id="o1", inst_id="BTC", side="buy",
                 qty=Decimal("1"), price=Decimal("100"),
                 fee=Decimal("0"), ts=1)
        assert f.fee == Decimal("0")

    def test_negative_fee_rejected(self) -> None:
        with pytest.raises(ValueError, match="fee"):
            Fill(order_id="o1", inst_id="BTC", side="buy",
                 qty=Decimal("1"), price=Decimal("100"),
                 fee=Decimal("-0.1"), ts=1)

    def test_invalid_side_rejected(self) -> None:
        with pytest.raises(ValueError, match="side"):
            Fill(order_id="o1", inst_id="BTC", side="hold",
                 qty=Decimal("1"), price=Decimal("100"),
                 fee=Decimal("0.1"), ts=1)

    def test_zero_qty_rejected(self) -> None:
        with pytest.raises(ValueError, match="qty"):
            Fill(order_id="o1", inst_id="BTC", side="buy",
                 qty=Decimal("0"), price=Decimal("100"),
                 fee=Decimal("0.1"), ts=1)

    def test_zero_price_rejected(self) -> None:
        with pytest.raises(ValueError, match="price"):
            Fill(order_id="o1", inst_id="BTC", side="buy",
                 qty=Decimal("1"), price=Decimal("0"),
                 fee=Decimal("0.1"), ts=1)


class TestFillSerialization:
    """Fill JSON 往返。"""

    def test_roundtrip(self) -> None:
        f = Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                 qty=Decimal("1.5"), price=Decimal("50000.5"),
                 fee=Decimal("50"), ts=1_700_000_000_000)
        j = f.to_json()
        f2 = Fill.from_json(j)
        assert f2 == f


# =====================================================================
# Position 单元测试
# =====================================================================


class TestPositionConstruction:
    """Position 构造与初始状态。"""

    def test_default_flat(self) -> None:
        p = Position(inst_id="BTC-USDT")
        assert p.is_flat
        assert not p.is_long
        assert not p.is_short
        assert p.avg_price == Decimal("0")
        assert p.realized_pnl == Decimal("0")

    def test_empty_inst_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="inst_id"):
            Position(inst_id="")

    def test_flat_with_nonzero_avg_price_rejected(self) -> None:
        """qty=0 时 avg_price 必须为 0。"""
        with pytest.raises(ValueError, match="avg_price"):
            Position(inst_id="BTC", avg_price=Decimal("100"))

    def test_negative_avg_price_rejected(self) -> None:
        with pytest.raises(ValueError, match="avg_price"):
            Position(inst_id="BTC", avg_price=Decimal("-1"))


class TestPositionApplyFill:
    """Position.apply_fill 各成交场景。"""

    def test_open_long(self) -> None:
        """空仓开多头。"""
        p = Position(inst_id="BTC-USDT")
        f = Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                 qty=Decimal("1"), price=Decimal("100"),
                 fee=Decimal("0"), ts=1)
        p.apply_fill(f)
        assert p.qty == Decimal("1")
        assert p.is_long
        assert p.avg_price == Decimal("100")
        assert p.realized_pnl == Decimal("0")

    def test_add_to_long(self) -> None:
        """多头加仓：avg_price 按数量加权。"""
        p = Position(inst_id="BTC-USDT")
        p.apply_fill(Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                          qty=Decimal("1"), price=Decimal("100"),
                          fee=Decimal("0"), ts=1))
        p.apply_fill(Fill(order_id="o2", inst_id="BTC-USDT", side="buy",
                          qty=Decimal("1"), price=Decimal("120"),
                          fee=Decimal("0"), ts=2))
        # 加权: (1*100 + 1*120) / 2 = 110
        assert p.qty == Decimal("2")
        assert p.avg_price == Decimal("110")
        assert p.realized_pnl == Decimal("0")

    def test_close_long_partial(self) -> None:
        """多头部分减仓：实现部分盈亏。"""
        p = Position(inst_id="BTC-USDT")
        # 开 2 @ 100
        p.apply_fill(Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                          qty=Decimal("2"), price=Decimal("100"),
                          fee=Decimal("0"), ts=1))
        # 卖 1 @ 110
        p.apply_fill(Fill(order_id="o2", inst_id="BTC-USDT", side="sell",
                          qty=Decimal("1"), price=Decimal("110"),
                          fee=Decimal("0"), ts=2))
        # realized = (110-100)*1 = 10
        assert p.qty == Decimal("1")
        assert p.is_long
        assert p.avg_price == Decimal("100")  # avg 不变
        assert p.realized_pnl == Decimal("10")

    def test_close_long_full(self) -> None:
        """多头全平：avg_price 归 0。"""
        p = Position(inst_id="BTC-USDT")
        p.apply_fill(Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                          qty=Decimal("1"), price=Decimal("100"),
                          fee=Decimal("0"), ts=1))
        p.apply_fill(Fill(order_id="o2", inst_id="BTC-USDT", side="sell",
                          qty=Decimal("1"), price=Decimal("110"),
                          fee=Decimal("0"), ts=2))
        assert p.is_flat
        assert p.avg_price == Decimal("0")
        assert p.realized_pnl == Decimal("10")

    def test_reverse_long_to_short(self) -> None:
        """多头反手到空头：avg_price 重置为成交价。"""
        p = Position(inst_id="BTC-USDT")
        # 开多 1 @ 100
        p.apply_fill(Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                          qty=Decimal("1"), price=Decimal("100"),
                          fee=Decimal("0"), ts=1))
        # 卖 2 @ 110（1 平多 + 1 开空）
        p.apply_fill(Fill(order_id="o2", inst_id="BTC-USDT", side="sell",
                          qty=Decimal("2"), price=Decimal("110"),
                          fee=Decimal("0"), ts=2))
        # realized: 平 1 多 = (110-100)*1 = 10
        # 新空头 1 @ 110
        assert p.qty == Decimal("-1")
        assert p.is_short
        assert p.avg_price == Decimal("110")
        assert p.realized_pnl == Decimal("10")

    def test_close_long_loss(self) -> None:
        """多头亏损平仓：realized_pnl 为负。"""
        p = Position(inst_id="BTC-USDT")
        p.apply_fill(Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                          qty=Decimal("1"), price=Decimal("100"),
                          fee=Decimal("0"), ts=1))
        p.apply_fill(Fill(order_id="o2", inst_id="BTC-USDT", side="sell",
                          qty=Decimal("1"), price=Decimal("90"),
                          fee=Decimal("0"), ts=2))
        assert p.realized_pnl == Decimal("-10")

    def test_short_open_close(self) -> None:
        """空头开仓 + 平仓。"""
        p = Position(inst_id="BTC-USDT")
        # 开空 1 @ 100
        p.apply_fill(Fill(order_id="o1", inst_id="BTC-USDT", side="sell",
                          qty=Decimal("1"), price=Decimal("100"),
                          fee=Decimal("0"), ts=1))
        assert p.is_short
        assert p.qty == Decimal("-1")
        assert p.avg_price == Decimal("100")
        # 买回 1 @ 90：pnl = (100-90)*1 = 10
        p.apply_fill(Fill(order_id="o2", inst_id="BTC-USDT", side="buy",
                          qty=Decimal("1"), price=Decimal("90"),
                          fee=Decimal("0"), ts=2))
        assert p.is_flat
        assert p.realized_pnl == Decimal("10")

    def test_inst_id_mismatch_rejected(self) -> None:
        """fill.inst_id 与 position.inst_id 不一致时报错。"""
        p = Position(inst_id="BTC-USDT")
        f = Fill(order_id="o1", inst_id="ETH-USDT", side="buy",
                 qty=Decimal("1"), price=Decimal("100"),
                 fee=Decimal("0"), ts=1)
        with pytest.raises(ValueError, match="fill inst_id"):
            p.apply_fill(f)


class TestPositionValuation:
    """Position 估值方法。"""

    def test_market_value(self) -> None:
        p = Position(inst_id="BTC-USDT", qty=Decimal("2"),
                     avg_price=Decimal("100"))
        assert p.market_value(Decimal("150")) == Decimal("300")

    def test_unrealized_pnl_long(self) -> None:
        p = Position(inst_id="BTC-USDT", qty=Decimal("2"),
                     avg_price=Decimal("100"))
        # 多头未实现 = (150-100)*2 = 100
        assert p.unrealized_pnl(Decimal("150")) == Decimal("100")

    def test_unrealized_pnl_short(self) -> None:
        p = Position(inst_id="BTC-USDT", qty=Decimal("-2"),
                     avg_price=Decimal("100"))
        # 空头未实现 = (100-150)*2 = -100
        assert p.unrealized_pnl(Decimal("150")) == Decimal("-100")

    def test_unrealized_pnl_flat(self) -> None:
        p = Position(inst_id="BTC-USDT")
        assert p.unrealized_pnl(Decimal("150")) == Decimal("0")


class TestPositionSerialization:
    """Position JSON 往返。"""

    def test_roundtrip_with_state(self) -> None:
        p = Position(inst_id="BTC-USDT")
        p.apply_fill(Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                          qty=Decimal("2"), price=Decimal("100"),
                          fee=Decimal("0"), ts=1))
        p.apply_fill(Fill(order_id="o2", inst_id="BTC-USDT", side="sell",
                          qty=Decimal("1"), price=Decimal("110"),
                          fee=Decimal("0"), ts=2))
        j = p.to_json()
        p2 = Position.from_json(j)
        assert p2.qty == p.qty
        assert p2.avg_price == p.avg_price
        assert p2.realized_pnl == p.realized_pnl


# =====================================================================
# Context 单元测试
# =====================================================================


class TestContext:
    """Context 状态读取。"""

    def test_default_context(self) -> None:
        p = Position(inst_id="BTC-USDT")
        ctx = Context(position=p)
        assert ctx.cash == Decimal("0")
        assert ctx.timestamp == 0
        assert ctx.bars_window == []
        assert ctx.params == {}

    def test_current_bar_returns_last(self) -> None:
        p = Position(inst_id="BTC-USDT")
        bars = [_make_bar(ts=1, close=Decimal("100")),
                _make_bar(ts=2, close=Decimal("110"))]
        ctx = Context(position=p, bars_window=bars, timestamp=2)
        assert ctx.current_bar().ts == 2

    def test_current_bar_empty_raises(self) -> None:
        p = Position(inst_id="BTC-USDT")
        ctx = Context(position=p)
        with pytest.raises(ValueError, match="bars_window is empty"):
            ctx.current_bar()

    def test_equity_at_price(self) -> None:
        """cash + position 市值。"""
        p = Position(inst_id="BTC-USDT", qty=Decimal("2"),
                     avg_price=Decimal("100"))
        bars = [_make_bar(ts=1, close=Decimal("150"))]
        ctx = Context(position=p, bars_window=bars, cash=Decimal("1000"))
        # equity = 1000 + 2*150 = 1300
        assert ctx.equity_at_price == Decimal("1300")

    def test_equity_empty_window_raises(self) -> None:
        p = Position(inst_id="BTC-USDT")
        ctx = Context(position=p)
        with pytest.raises(ValueError, match="bars_window is empty"):
            _ = ctx.equity_at_price


# =====================================================================
# Strategy ABC 单元测试
# =====================================================================


class TestStrategyABC:
    """Strategy 抽象基类。"""

    def test_cannot_instantiate_abc(self) -> None:
        """ABC 必须由子类实现。"""
        with pytest.raises(TypeError, match="abstract"):
            Strategy()

    def test_partial_implementation_rejected(self) -> None:
        """缺实现 on_bar 时仍不能实例化。"""

        class BadStrategy(Strategy):
            @property
            def strategy_id(self) -> str:
                return "bad"

            def on_init(self, ctx: Context) -> None:
                pass

        with pytest.raises(TypeError, match="on_bar"):
            BadStrategy()

    def test_full_implementation_instantiable(self) -> None:
        """完整实现可实例化。"""

        class GoodStrategy(Strategy):
            @property
            def strategy_id(self) -> str:
                return "good"

            def on_init(self, ctx: Context) -> None:
                self.called_init = True

            def on_bar(self, bar: Bar, ctx: Context):
                self.last_bar = bar
                return None

        s = GoodStrategy()
        assert s.strategy_id == "good"

    def test_default_on_trade_returns_none(self) -> None:
        """默认 on_trade 返回 None。"""

        class S(Strategy):
            @property
            def strategy_id(self) -> str:
                return "x"

            def on_init(self, ctx: Context) -> None:
                pass

            def on_bar(self, bar: Bar, ctx: Context):
                return None

        s = S()
        fill = Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                   qty=Decimal("1"), price=Decimal("100"),
                   fee=Decimal("0"), ts=1)
        ctx = Context(position=Position(inst_id="BTC-USDT"))
        # 默认实现不抛异常，返回 None
        assert s.on_trade(fill, ctx) is None

    def test_default_on_finish_returns_none(self) -> None:
        """默认 on_finish 返回 None。"""

        class S(Strategy):
            @property
            def strategy_id(self) -> str:
                return "x"

            def on_init(self, ctx: Context) -> None:
                pass

            def on_bar(self, bar: Bar, ctx: Context):
                return None

        s = S()
        ctx = Context(position=Position(inst_id="BTC-USDT"))
        assert s.on_finish(ctx) is None


class TestStrategyLifecycle:
    """完整生命周期：on_init → on_bar → on_trade → on_finish。"""

    def test_full_lifecycle_invocation(self) -> None:
        """验证回调顺序与参数传递。"""

        class TraceStrategy(Strategy):
            @property
            def strategy_id(self) -> str:
                return "trace"

            def on_init(self, ctx: Context) -> None:
                self.events = ["init"]

            def on_bar(self, bar: Bar, ctx: Context):
                self.events.append(f"bar:{bar.ts}")
                if bar.ts == 2:
                    return Signal(
                        strategy_id="trace", side="buy", order_type="market",
                        inst_id="BTC-USDT", qty=Decimal("1"), ts=bar.ts,
                    )
                return None

            def on_trade(self, trade: Fill, ctx: Context) -> None:
                self.events.append(f"trade:{trade.order_id}")

            def on_finish(self, ctx: Context) -> None:
                self.events.append("finish")

        s = TraceStrategy()
        p = Position(inst_id="BTC-USDT")
        ctx = Context(position=p, params={})

        s.on_init(ctx)
        bars = [_make_bar(ts=1, close=Decimal("100")),
                _make_bar(ts=2, close=Decimal("110")),
                _make_bar(ts=3, close=Decimal("120"))]
        for bar in bars:
            ctx.timestamp = bar.ts
            ctx.bars_window.append(bar)
            sig = s.on_bar(bar, ctx)
            if sig is not None:
                # 模拟 Matcher 成交
                fill = Fill(order_id="o1", inst_id="BTC-USDT", side="buy",
                            qty=Decimal("1"), price=Decimal("110"),
                            fee=Decimal("0"), ts=bar.ts)
                p.apply_fill(fill)
                s.on_trade(fill, ctx)
        s.on_finish(ctx)

        assert s.events == [
            "init",
            "bar:1",
            "bar:2",
            "trade:o1",
            "bar:3",
            "finish",
        ]
        # 验证策略内部状态通过 ctx 反映
        assert p.qty == Decimal("1")
        assert p.avg_price == Decimal("110")


# =====================================================================
# 辅助函数
# =====================================================================


def _make_bar(ts: int, close: Decimal) -> Bar:
    """构造测试用 Bar（OHLC 用 close 统一）。"""
    return Bar(
        ts=ts,
        open=close, high=close, low=close, close=close,
        vol=Decimal("0"), vol_quote=Decimal("0"),
        confirm=1, inst_id="BTC-USDT", bar_size="1H",
    )
