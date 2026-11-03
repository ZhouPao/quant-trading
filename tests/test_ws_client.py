"""WSClient / ticker 解析测试（M3）。

覆盖：
- parse_ticker: 正常/缺字段/零价/交叉盘口
- WSClient: 订阅、重连后恢复订阅、消息分发、错误事件、优雅关闭
- LivePriceBook: 更新/取值/回调
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from quant.execution.price import LivePriceBook, RestPriceProvider
from quant.okx.ws_client import WSClient, parse_ticker


def _ticker(**overrides) -> dict:
    item = {
        "instId": "BTC-USDT",
        "last": "65000.5",
        "lastSz": "0.01",
        "askPx": "65001",
        "askSz": "1.2",
        "bidPx": "65000",
        "bidSz": "0.8",
        "ts": "1700000000000",
    }
    item.update(overrides)
    return item


class TestParseTicker:
    def test_normal(self):
        tick = parse_ticker(_ticker())
        assert tick is not None
        assert tick.inst_id == "BTC-USDT"
        assert tick.bid_px == Decimal("65000")
        assert tick.ask_px == Decimal("65001")
        assert tick.last_px == Decimal("65000.5")
        assert tick.ts == 1_700_000_000_000

    def test_missing_field_returns_none(self):
        item = _ticker()
        del item["bidPx"]
        assert parse_ticker(item) is None

    def test_zero_bid_returns_none(self):
        assert parse_ticker(_ticker(bidPx="0")) is None

    def test_crossed_book_returns_none(self):
        assert parse_ticker(_ticker(bidPx="65002", askPx="65001")) is None

    def test_invalid_decimal_returns_none(self):
        assert parse_ticker(_ticker(askPx="abc")) is None

    def test_empty_last_falls_back_to_zero(self):
        tick = parse_ticker(_ticker(last=""))
        assert tick is not None
        assert tick.last_px == Decimal("0")


class FakeWS:
    """最小假 WebSocket。"""

    def __init__(self, messages: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self._messages = [json.dumps(m) for m in (messages or [])]
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for m in self._messages:
            yield m


class FakeConnect:
    """连续返回预设连接（用于测试重连）。"""

    def __init__(self, *sessions: FakeWS) -> None:
        self._sessions = list(sessions)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        ws = self._sessions.pop(0) if self._sessions else FakeWS()
        return _AsyncCtx(ws)


class _AsyncCtx:
    def __init__(self, ws: FakeWS) -> None:
        self._ws = ws

    async def __aenter__(self) -> FakeWS:
        return self._ws

    async def __aexit__(self, *exc) -> None:
        return None


class _HangingWS(FakeWS):
    """保持连接不结束，直到 close()：用于在「已连接」状态下取快照。"""

    def __init__(self) -> None:
        super().__init__()
        self._closed = asyncio.Event()

    async def close(self) -> None:
        self.closed = True
        self._closed.set()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        await self._closed.wait()
        return
        yield  # pragma: no cover


class TestWSClient:
    @pytest.mark.asyncio
    async def test_subscribe_sends_when_connected(self):
        ws = FakeWS()
        client = WSClient(connect_fn=FakeConnect(ws))
        received: list[dict] = []

        async def cb(item):
            received.append(item)

        await client.subscribe("tickers", "BTC-USDT", cb)
        # 未连接时只登记
        assert ws.sent == []

        await client.subscribe("tickers", "ETH-USDT", cb)
        assert len(client._subscriptions) == 2

    @pytest.mark.asyncio
    async def test_restore_subscriptions_after_connect(self):
        msg = {
            "arg": {"channel": "tickers", "instId": "BTC-USDT"},
            "data": [_ticker()],
        }
        fake = FakeWS([msg])
        client = WSClient(connect_fn=FakeConnect(fake))
        received: list[dict] = []

        async def cb(item):
            received.append(item)

        await client.subscribe("tickers", "BTC-USDT", cb)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.05)
        await client.close()
        await asyncio.wait_for(task, timeout=2)

        assert fake.sent[0]["op"] == "subscribe"
        assert fake.sent[0]["args"][0]["instId"] == "BTC-USDT"
        assert len(received) == 1
        assert received[0]["instId"] == "BTC-USDT"

    @pytest.mark.asyncio
    async def test_message_without_callback_is_ignored(self):
        client = WSClient()
        await client._handle_message(
            json.dumps(
                {
                    "arg": {"channel": "tickers", "instId": "BTC-USDT"},
                    "data": [_ticker()],
                }
            )
        )

    @pytest.mark.asyncio
    async def test_error_event_does_not_raise(self):
        client = WSClient()
        await client._handle_message(
            json.dumps({"event": "error", "code": "60012", "msg": "bad request"})
        )

    @pytest.mark.asyncio
    async def test_invalid_json_does_not_raise(self):
        client = WSClient()
        await client._handle_message("not-json")

    @pytest.mark.asyncio
    async def test_reconnect_and_restore(self):
        first = FakeWS([])
        second = FakeWS([])
        connect = FakeConnect(first, second)
        client = WSClient(connect_fn=connect, reconnect_delay=0.01)

        async def cb(item):
            pass

        await client.subscribe("tickers", "BTC-USDT", cb)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.05)
        await client.close()
        await asyncio.wait_for(task, timeout=2)

        assert connect.calls >= 2
        assert len(second.sent) == 1

    @pytest.mark.asyncio
    async def test_close_stops_run_loop(self):
        client = WSClient(connect_fn=FakeConnect(FakeWS([])))
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.02)
        await client.close()
        await asyncio.wait_for(task, timeout=2)
        assert not client.is_connected

    def test_jitter_stays_within_20_percent(self):
        client = WSClient(reconnect_delay=5.0, max_reconnect_delay=60.0)
        waits = [client._jitter(5.0) for _ in range(50)]
        assert all(4.0 <= w <= 6.0 for w in waits)
        # 有抖动才叫抖动：多进程同时断线不该落在同一秒
        assert len(set(waits)) > 1

    def test_jitter_capped_at_max_delay(self):
        client = WSClient(reconnect_delay=5.0, max_reconnect_delay=60.0)
        waits = [client._jitter(60.0) for _ in range(50)]
        assert all(48.0 <= w <= 60.0 for w in waits)

    @pytest.mark.asyncio
    async def test_status_records_consecutive_failures(self):
        class _BoomConnect:
            def __call__(self, *args, **kwargs):
                raise RuntimeError("boom")

        # 正数退避保证每轮都有 await 点，循环不会把事件循环卡死
        client = WSClient(connect_fn=_BoomConnect(), reconnect_delay=0.001)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.05)
        await client.close()
        await asyncio.wait_for(task, timeout=2)

        status = client.status()
        assert status["connected"] is False
        assert status["failures"] >= 2
        assert "boom" in (status["last_error"] or "")
        assert status["last_error_ts"] is not None

    @pytest.mark.asyncio
    async def test_status_resets_failures_after_connect(self):
        ws = _HangingWS()
        client = WSClient(connect_fn=FakeConnect(ws), reconnect_delay=0.001)
        task = asyncio.create_task(client.run())
        assert await client.wait_connected(1.0)

        status = client.status()
        assert status["connected"] is True
        assert status["failures"] == 0
        assert status["connected_since"] is not None

        await client.close()
        await asyncio.wait_for(task, timeout=2)
        assert client.status()["connected"] is False


class TestWsRetryAt:
    """next_retry_at：看板真倒计时的时间来源（P1 口径）。"""

    def test_next_retry_at_null_until_failure(self):
        ws = WSClient()
        assert ws.status()["next_retry_at"] is None

    def test_next_retry_at_set_on_failure(self):
        ws = WSClient()
        wait = ws._record_failure("boom")
        status = ws.status()
        assert status["next_retry_at"] is not None
        # ≈ last_error_ts + wait*1000（容许取整误差）
        assert status["next_retry_at"] >= status["last_error_ts"]
        assert (
            abs(status["next_retry_at"] - status["last_error_ts"] - wait * 1000)
            < 50
        )


class _RawRecordingWS(_HangingWS):
    """记录原始发送内容（基类 send 会 json.loads，"ping" 不是 JSON）。"""

    def __init__(self) -> None:
        super().__init__()
        self.raw_sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.raw_sent.append(raw)


class TestWsKeepalive:
    @pytest.mark.asyncio
    async def test_sends_app_layer_ping_while_connected(self):
        """连接空闲时按周期发送文本 "ping"（OKX 应用层心跳，P2）。"""
        ws = _RawRecordingWS()
        client = WSClient(connect_fn=FakeConnect(ws), keepalive_interval=0.05)
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.2)
        await client.close()
        await task
        assert any(s == "ping" for s in ws.raw_sent)


class TestLivePriceBook:
    def test_update_and_get(self):
        book = LivePriceBook()
        tick = parse_ticker(_ticker())
        book.update(tick)
        assert book.get_price("BTC-USDT") == Decimal("65000.5")
        assert book.get_tick("BTC-USDT").bid_px == Decimal("65000")
        assert book.get_price("ETH-USDT") is None

    def test_fallback_to_mid_price(self):
        book = LivePriceBook()
        book.update(parse_ticker(_ticker(last="0")))
        assert book.get_price("BTC-USDT") == Decimal("65000.5")

    @pytest.mark.asyncio
    async def test_payload_callback(self):
        book = LivePriceBook()
        await book.update_from_payload(_ticker())
        assert book.as_provider()("BTC-USDT") == Decimal("65000.5")

    @pytest.mark.asyncio
    async def test_invalid_payload_ignored(self):
        book = LivePriceBook()
        await book.update_from_payload({"instId": "BTC-USDT"})
        assert book.get_price("BTC-USDT") is None


class FakeTickerClient:
    def __init__(self, rows=None, error=None):
        self._rows = rows if rows is not None else [{"last": "65000.5"}]
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        self.calls.append((path, params or {}))
        if self._error:
            raise self._error
        return self._rows


class TestRestPriceProvider:
    def test_returns_last_price(self):
        client = FakeTickerClient()
        provider = RestPriceProvider(client)
        assert provider("BTC-USDT") == Decimal("65000.5")
        assert client.calls == [("/api/v5/market/ticker", {"instId": "BTC-USDT"})]

    def test_empty_rows_returns_none(self):
        assert RestPriceProvider(FakeTickerClient(rows=[]))("BTC-USDT") is None

    def test_empty_last_returns_none(self):
        assert RestPriceProvider(FakeTickerClient(rows=[{"last": ""}]))("BTC-USDT") is None

    def test_error_returns_none(self):
        provider = RestPriceProvider(FakeTickerClient(error=RuntimeError("boom")))
        assert provider("BTC-USDT") is None
