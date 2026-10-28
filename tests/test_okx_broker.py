"""OkxBroker 测试（M3）。

用 FakeOKXClient 隔离网络，覆盖：
- 市价单提交 → 回查 → Fill
- 限价单 px 字段
- sCode 拒单 / 缺 ordId
- 轮询直到终结状态
- 状态映射（filled/pending/canceled）
- Broker 协议兼容
"""
from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant.backtest.signal import Signal
from quant.execution.broker import Broker
from quant.execution.executor import Executor
from quant.execution.okx_broker import OkxBroker, _cl_ord_id, _fmt, detail_to_fill
from quant.foundation.errors import OrderError


def _signal(side="buy", order_type="market", price=None, signal_id="sig-001"):
    return Signal(
        strategy_id="sma_cross",
        side=side,
        order_type=order_type,
        inst_id="BTC-USDT",
        qty=Decimal("0.001"),
        ts=1_700_000_000_000,
        price=Decimal(price) if price is not None else None,
        signal_id=signal_id,
    )


class FakeOKXClient:
    def __init__(self, post_resp=None, order_details=None, instrument=None):
        self.post_resp = (
            post_resp
            if post_resp is not None
            else [{"ordId": "ord-1", "sCode": "0", "sMsg": ""}]
        )
        self._details = list(order_details or [])
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []
        self.instrument = instrument

    def post(self, path, body):
        self.posts.append((path, body))
        return self.post_resp

    def get(self, path, params=None):
        self.gets.append((path, params or {}))
        if len(self._details) > 1:
            return [self._details.pop(0)]
        return [self._details[0]] if self._details else []

    def get_instrument(self, inst_id):
        return self.instrument


def _filled_detail(**overrides):
    detail = {
        "ordId": "ord-1",
        "state": "filled",
        "accFillSz": "0.001",
        "avgPx": "65000",
        "fee": "-0.065",
        "fillTime": "1700000001000",
        "side": "buy",
    }
    detail.update(overrides)
    return detail


def _pending_detail(**overrides):
    detail = {
        "ordId": "ord-1",
        "state": "live",
        "accFillSz": "0",
        "avgPx": "0",
    }
    detail.update(overrides)
    return detail


class TestFormatting:
    def test_fmt_no_scientific_notation(self):
        assert _fmt(Decimal("0.00000001")) == "0.00000001"
        assert _fmt(Decimal("6.5E+4")) == "65000"
        assert _fmt(Decimal("1.0")) == "1"

    def test_cl_ord_id_strips_non_alnum(self):
        assert _cl_ord_id(_signal(signal_id="a8f5-1234-zz")) == "a8f51234zz"

    def test_cl_ord_id_generated_when_empty(self):
        cid = _cl_ord_id(_signal(signal_id=""))
        assert cid.startswith("q")
        assert cid.isalnum()


class TestOkxBrokerMarket:
    @pytest.mark.asyncio
    async def test_market_fill(self):
        client = FakeOKXClient(order_details=[_filled_detail()])
        broker = OkxBroker(client, poll_attempts=1)

        result = await broker.execute(_signal(), Decimal("0.001"))

        assert result.order.status == "filled"
        assert result.order.order_id == "ord-1"
        assert result.fill is not None
        assert result.fill.qty == Decimal("0.001")
        assert result.fill.price == Decimal("65000")
        assert result.fill.fee == Decimal("0.065")
        assert result.fill.ts == 1_700_000_001_000

        path, body = client.posts[0]
        assert path == "/api/v5/trade/order"
        assert body["instId"] == "BTC-USDT"
        assert body["side"] == "buy"
        assert body["ordType"] == "market"
        assert body["sz"] == "0.001"
        assert body["tdMode"] == "cash"
        assert body["tgtCcy"] == "base_ccy"
        assert "px" not in body

    @pytest.mark.asyncio
    async def test_market_pending_when_not_filled(self):
        client = FakeOKXClient(order_details=[_pending_detail()])
        broker = OkxBroker(client, poll_attempts=1)

        result = await broker.execute(_signal(), Decimal("0.001"))

        assert result.order.status == "pending"
        assert result.fill is None

    @pytest.mark.asyncio
    async def test_canceled_order(self):
        client = FakeOKXClient(order_details=[_pending_detail(state="canceled")])
        broker = OkxBroker(client, poll_attempts=1)

        result = await broker.execute(_signal(), Decimal("0.001"))

        assert result.order.status == "canceled"
        assert result.fill is None

    @pytest.mark.asyncio
    async def test_partial_fill_reports_pending(self):
        client = FakeOKXClient(
            order_details=[
                _pending_detail(state="partially_filled", accFillSz="0.0005", avgPx="65000")
            ]
        )
        broker = OkxBroker(client, poll_attempts=1)

        result = await broker.execute(_signal(), Decimal("0.001"))

        assert result.order.status == "pending"
        # 部分成交不产生 Fill（由后续订单查询/成交推送补齐）
        assert result.fill is None


class TestOkxBrokerLimit:
    @pytest.mark.asyncio
    async def test_limit_includes_price(self):
        client = FakeOKXClient(order_details=[_pending_detail()])
        broker = OkxBroker(client, poll_attempts=1)

        await broker.execute(_signal(order_type="limit", price="64000"), Decimal("0.001"))

        body = client.posts[0][1]
        assert body["ordType"] == "limit"
        assert body["px"] == "64000"

    @pytest.mark.asyncio
    async def test_limit_without_price_raises(self):
        broker = OkxBroker(FakeOKXClient(), poll_attempts=1)
        # 用轻量 stub 绕过 Signal 模型校验，模拟调用方错误
        sig = SimpleNamespace(
            strategy_id="s",
            side="buy",
            order_type="limit",
            inst_id="BTC-USDT",
            qty=Decimal("1"),
            ts=1,
            price=None,
            signal_id="s1",
        )
        with pytest.raises(ValueError, match="limit order requires price"):
            await broker.execute(sig, Decimal("1"))


class TestOkxBrokerErrors:
    @pytest.mark.asyncio
    async def test_s_code_rejection(self):
        client = FakeOKXClient(
            post_resp=[{"ordId": "", "sCode": "51008", "sMsg": "Insufficient balance"}]
        )
        broker = OkxBroker(client, poll_attempts=1)
        with pytest.raises(OrderError) as exc:
            await broker.execute(_signal(), Decimal("1"))
        assert exc.value.code == "51008"

    @pytest.mark.asyncio
    async def test_empty_response(self):
        client = FakeOKXClient(post_resp=[])
        broker = OkxBroker(client, poll_attempts=1)
        with pytest.raises(OrderError):
            await broker.execute(_signal(), Decimal("0.001"))

    @pytest.mark.asyncio
    async def test_missing_ord_id(self):
        client = FakeOKXClient(post_resp=[{"sCode": "0"}])
        broker = OkxBroker(client, poll_attempts=1)
        with pytest.raises(OrderError, match="missing ordId"):
            await broker.execute(_signal(), Decimal("0.001"))

    @pytest.mark.asyncio
    async def test_non_positive_qty(self):
        broker = OkxBroker(FakeOKXClient(), poll_attempts=1)
        with pytest.raises(ValueError, match="qty must be positive"):
            await broker.execute(_signal(), Decimal("0"))


class TestOkxBrokerPolling:
    @pytest.mark.asyncio
    async def test_polls_until_filled(self):
        client = FakeOKXClient(
            order_details=[_pending_detail(), _filled_detail()]
        )
        broker = OkxBroker(client, poll_attempts=3, poll_interval=0)

        result = await broker.execute(_signal(), Decimal("0.001"))

        assert result.order.status == "filled"
        assert len(client.gets) == 2

    @pytest.mark.asyncio
    async def test_stops_after_max_attempts(self):
        client = FakeOKXClient(
            order_details=[
                _pending_detail(),
                _pending_detail(),
                _pending_detail(),
            ]
        )
        broker = OkxBroker(client, poll_attempts=2, poll_interval=0)

        result = await broker.execute(_signal(), Decimal("0.001"))

        assert result.order.status == "pending"
        assert len(client.gets) == 2

    @pytest.mark.asyncio
    async def test_created_ts_is_now(self):
        client = FakeOKXClient(order_details=[_filled_detail()])
        broker = OkxBroker(client, poll_attempts=1)
        before = int(time.time() * 1000)
        result = await broker.execute(_signal(), Decimal("0.001"))
        assert result.order.created_ts >= before


class TestBrokerProtocol:
    def test_executor_and_okx_broker_satisfy_protocol(self):
        assert isinstance(Executor(Decimal("0.001")), Broker)
        assert isinstance(OkxBroker(FakeOKXClient()), Broker)


class TestOkxBrokerCancel:
    @pytest.mark.asyncio
    async def test_cancel_order(self):
        client = FakeOKXClient()
        broker = OkxBroker(client, poll_attempts=1)

        row = await broker.cancel_order("BTC-USDT", "ord-1")

        path, body = client.posts[0]
        assert path == "/api/v5/trade/cancel-order"
        assert body == {"instId": "BTC-USDT", "ordId": "ord-1"}
        assert row["sCode"] == "0"

    @pytest.mark.asyncio
    async def test_cancel_order_rejection(self):
        client = FakeOKXClient(
            post_resp=[{"sCode": "51400", "sMsg": "Order does not exist"}]
        )
        broker = OkxBroker(client, poll_attempts=1)
        with pytest.raises(OrderError) as exc:
            await broker.cancel_order("BTC-USDT", "ord-1")
        assert exc.value.code == "51400"

    @pytest.mark.asyncio
    async def test_cancel_order_empty_response(self):
        broker = OkxBroker(FakeOKXClient(post_resp=[]), poll_attempts=1)
        with pytest.raises(OrderError):
            await broker.cancel_order("BTC-USDT", "ord-1")


class TestOkxBrokerBatchCancel:
    class FakeBatchClient:
        def __init__(self, responses: list) -> None:
            self.responses = list(responses)
            self.posts: list[tuple[str, list]] = []

        def post(self, path, body):
            self.posts.append((path, body))
            return self.responses.pop(0) if self.responses else []

    @pytest.mark.asyncio
    async def test_cancel_orders_returns_codes_by_ord_id(self):
        client = self.FakeBatchClient(
            [
                [
                    {"ordId": "o-1", "sCode": "0"},
                    {"ordId": "o-2", "sCode": "51400"},
                ]
            ]
        )
        broker = OkxBroker(client, poll_attempts=1)

        results = await broker.cancel_orders(
            [("BTC-USDT", "o-1"), ("ETH-USDT", "o-2")]
        )

        path, body = client.posts[0]
        assert path == "/api/v5/trade/cancel-batch-orders"
        assert body == [
            {"instId": "BTC-USDT", "ordId": "o-1"},
            {"instId": "ETH-USDT", "ordId": "o-2"},
        ]
        assert results == {"o-1": "0", "o-2": "51400"}

    @pytest.mark.asyncio
    async def test_cancel_orders_chunks_by_20(self):
        orders = [(f"i-{n}", f"o-{n}") for n in range(21)]
        client = self.FakeBatchClient(
            [
                [{"ordId": f"o-{n}", "sCode": "0"} for n in range(20)],
                [{"ordId": "o-20", "sCode": "0"}],
            ]
        )
        broker = OkxBroker(client, poll_attempts=1)

        results = await broker.cancel_orders(orders)

        assert [len(body) for _path, body in client.posts] == [20, 1]
        assert len(results) == 21
        assert all(code == "0" for code in results.values())

    @pytest.mark.asyncio
    async def test_cancel_orders_marks_missing_rows_failed(self):
        client = self.FakeBatchClient([[{"ordId": "o-1", "sCode": "0"}]])
        broker = OkxBroker(client, poll_attempts=1)

        results = await broker.cancel_orders(
            [("BTC-USDT", "o-1"), ("BTC-USDT", "o-2")]
        )

        assert results == {"o-1": "0", "o-2": "-1"}

    @pytest.mark.asyncio
    async def test_cancel_orders_empty_response(self):
        broker = OkxBroker(self.FakeBatchClient([[]]), poll_attempts=1)

        results = await broker.cancel_orders([("BTC-USDT", "o-1")])

        assert results == {"o-1": "-1"}

    @pytest.mark.asyncio
    async def test_cancel_orders_no_orders_makes_no_request(self):
        client = self.FakeBatchClient([])
        broker = OkxBroker(client, poll_attempts=1)

        assert await broker.cancel_orders([]) == {}
        assert client.posts == []


class TestOkxBrokerAmend:
    @pytest.mark.asyncio
    async def test_amend_order_new_size(self):
        client = FakeOKXClient()
        broker = OkxBroker(client, poll_attempts=1)

        await broker.amend_order("BTC-USDT", "ord-1", new_size=Decimal("0.002"))

        path, body = client.posts[0]
        assert path == "/api/v5/trade/amend-order"
        assert body == {"instId": "BTC-USDT", "ordId": "ord-1", "newSz": "0.002"}

    @pytest.mark.asyncio
    async def test_amend_order_new_price(self):
        client = FakeOKXClient()
        broker = OkxBroker(client, poll_attempts=1)

        await broker.amend_order("BTC-USDT", "ord-1", new_price=Decimal("64000"))

        path, body = client.posts[0]
        assert body == {"instId": "BTC-USDT", "ordId": "ord-1", "newPx": "64000"}

    @pytest.mark.asyncio
    async def test_amend_order_requires_arg(self):
        broker = OkxBroker(FakeOKXClient(), poll_attempts=1)
        with pytest.raises(ValueError, match="new_size or new_price"):
            await broker.amend_order("BTC-USDT", "ord-1")


class TestOkxBrokerGetOrder:
    @pytest.mark.asyncio
    async def test_get_order_returns_detail(self):
        client = FakeOKXClient(order_details=[_filled_detail()])
        broker = OkxBroker(client, poll_attempts=1)

        detail = await broker.get_order("BTC-USDT", "ord-1")

        assert detail["state"] == "filled"
        assert client.gets[0] == (
            "/api/v5/trade/order",
            {"instId": "BTC-USDT", "ordId": "ord-1"},
        )

    @pytest.mark.asyncio
    async def test_get_order_empty(self):
        broker = OkxBroker(FakeOKXClient(order_details=[]), poll_attempts=1)
        assert await broker.get_order("BTC-USDT", "ord-1") == {}


class TestDetailToFill:
    def test_filled_detail(self):
        fill = detail_to_fill("BTC-USDT", "ord-1", _filled_detail())
        assert fill is not None
        assert fill.inst_id == "BTC-USDT"
        assert fill.side == "buy"
        assert fill.qty == Decimal("0.001")
        assert fill.price == Decimal("65000")
        assert fill.fee == Decimal("0.065")
        assert fill.ts == 1_700_000_001_000

    def test_pending_returns_none(self):
        assert detail_to_fill("BTC-USDT", "ord-1", _pending_detail()) is None

    def test_spot_buy_fee_in_base_deducted_and_converted(self):
        """现货买入手续费以基础币收取：数量扣费，费用按成交价折成计价币。"""
        fill = detail_to_fill(
            "BTC-USDT",
            "ord-1",
            _filled_detail(
                accFillSz="0.001", avgPx="80573.8", fee="-0.000001", feeCcy="BTC"
            ),
        )

        assert fill is not None
        # 交易所实际入账 0.001 - 0.000001
        assert fill.qty == Decimal("0.000999")
        # 0.000001 BTC 按成交价折成 USDT
        assert fill.fee == Decimal("0.000001") * Decimal("80573.8")

    def test_spot_sell_fee_in_quote_unchanged(self):
        """现货卖出手续费本来就是计价币，数量与费用都不动。"""
        fill = detail_to_fill(
            "BTC-USDT",
            "ord-1",
            _filled_detail(
                side="sell",
                accFillSz="0.0025",
                avgPx="80422.1",
                fee="-0.20105525",
                feeCcy="USDT",
            ),
        )

        assert fill is not None
        assert fill.qty == Decimal("0.0025")
        assert fill.fee == Decimal("0.20105525")

    def test_swap_fee_not_converted(self):
        """合约 sz 是张数、feeCcy 是结算币，不做现货口径换算。"""
        fill = detail_to_fill(
            "BTC-USDT-SWAP",
            "ord-1",
            _filled_detail(accFillSz="5", fee="-0.01", feeCcy="BTC"),
        )

        assert fill is not None
        assert fill.qty == Decimal("5")
        assert fill.fee == Decimal("0.01")

    def test_missing_side_returns_none(self):
        detail = _filled_detail()
        detail.pop("side")
        assert detail_to_fill("BTC-USDT", "ord-1", detail) is None


def _swap_instrument(ct_val="0.01", lot_sz="1"):
    """BTC-USDT-SWAP 产品配置：1 张 = 0.01 BTC。"""
    from quant.models.instrument import Instrument

    return Instrument(
        inst_id="BTC-USDT-SWAP",
        inst_type="SWAP",
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy="USDT",
        tick_sz=Decimal("0.1"),
        lot_sz=Decimal(lot_sz),
        min_sz=Decimal(lot_sz),
        ct_val=Decimal(ct_val),
        state="live",
        list_time=0,
    )


def _swap_signal():
    return Signal(
        strategy_id="sma_cross",
        side="buy",
        order_type="market",
        inst_id="BTC-USDT-SWAP",
        qty=Decimal("0.05"),
        ts=1_700_000_000_000,
        signal_id="sig-swap",
    )


class TestContractUnits:
    """合约单位统一：调用方只给基础币，broker 在边界折成张数。"""

    @pytest.mark.asyncio
    async def test_submits_contracts_and_returns_base_qty(self):
        client = FakeOKXClient(
            order_details=[_filled_detail(accFillSz="5", avgPx="80000")],
            instrument=_swap_instrument(),
        )
        broker = OkxBroker(client, poll_attempts=1)

        result = await broker.execute(_swap_signal(), Decimal("0.05"))

        _, body = client.posts[0]
        # 0.05 BTC / 0.01 = 5 张
        assert body["sz"] == "5"
        # tgtCcy 只对现货有效
        assert "tgtCcy" not in body
        assert result.fill is not None
        # 回报再折回基础币，本地记账只见基础币
        assert result.fill.qty == Decimal("0.05")

    @pytest.mark.asyncio
    async def test_rounds_down_to_lot_size(self):
        client = FakeOKXClient(
            order_details=[_filled_detail(accFillSz="5", avgPx="80000")],
            instrument=_swap_instrument(ct_val="0.01", lot_sz="2"),
        )
        broker = OkxBroker(client, poll_attempts=1)

        await broker.execute(_swap_signal(), Decimal("0.05"))

        _, body = client.posts[0]
        # 5 张按 lotSz=2 向下取整为 4
        assert body["sz"] == "4"

    @pytest.mark.asyncio
    async def test_rejects_when_below_one_contract(self):
        client = FakeOKXClient(
            order_details=[_filled_detail()], instrument=_swap_instrument()
        )
        broker = OkxBroker(client, poll_attempts=1)

        with pytest.raises(OrderError):
            await broker.execute(_swap_signal(), Decimal("0.001"))

        assert client.posts == []

    @pytest.mark.asyncio
    async def test_rejects_when_ct_val_unknown(self):
        """查不到面值时拒单：按 1 处理会让数量差 ctVal 倍。"""
        client = FakeOKXClient(order_details=[_filled_detail()])
        broker = OkxBroker(client, poll_attempts=1)

        with pytest.raises(OrderError):
            await broker.execute(_swap_signal(), Decimal("0.05"))

        assert client.posts == []

    @pytest.mark.asyncio
    async def test_spot_ct_val_is_one_without_lookup(self):
        client = FakeOKXClient(order_details=[_filled_detail()])
        broker = OkxBroker(client, poll_attempts=1)

        assert await broker.ct_val("BTC-USDT") == Decimal("1")

    @pytest.mark.asyncio
    async def test_td_mode_follows_instrument_type(self):
        """配置 cross 只作用于合约；现货必须仍发 cash，否则会被交易所拒单。"""
        swap_client = FakeOKXClient(
            order_details=[_filled_detail(accFillSz="5", avgPx="80000")],
            instrument=_swap_instrument(),
        )
        await OkxBroker(swap_client, td_mode="cross", poll_attempts=1).execute(
            _swap_signal(), Decimal("0.05")
        )
        assert swap_client.posts[0][1]["tdMode"] == "cross"

        spot_client = FakeOKXClient(order_details=[_filled_detail()])
        await OkxBroker(spot_client, td_mode="cross", poll_attempts=1).execute(
            _signal(), Decimal("0.001")
        )
        assert spot_client.posts[0][1]["tdMode"] == "cash"
