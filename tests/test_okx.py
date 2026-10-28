"""OKX 客户端层单元测试（spec §4）。

覆盖：
- Signer: 签名确定性、HMAC-SHA256 交叉验证、方法大小写不敏感、空 body
- RateLimiter: 令牌桶补充速率、惩罚阻塞、acquire 超时、未知组拒绝
- OKXClient: 错误码分发、50011 重试、5xx 退避重试、鉴权立即抛、
            时间戳格式、限频组映射、高层方法响应→模型转换
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from quant.foundation.config import OKXSettings
from quant.foundation.errors import (
    AuthError,
    InsufficientBalanceError,
    NetworkError,
    RateLimitTimeoutError,
    ServerError,
)
from quant.models import Bar, FundingRate, Instrument
from quant.okx.client import OKXClient
from quant.okx.rate_limiter import BucketConfig, RateLimiter
from quant.okx.signer import sign

# =====================================================================
# Signer 测试
# =====================================================================


class TestSigner:
    """HMAC-SHA256 签名测试。"""

    def test_sign_deterministic(self):
        """相同输入应产生相同签名。"""
        s1 = sign("2024-01-01T10:30:00.000Z", "GET", "/api/v5/account/balance", "", "secret123")
        s2 = sign("2024-01-01T10:30:00.000Z", "GET", "/api/v5/account/balance", "", "secret123")
        assert s1 == s2

    def test_sign_cross_verify_with_stdlib(self):
        """与 Python 标准库独立计算结果比对，验证算法正确。"""
        ts = "2024-01-01T10:30:00.000Z"
        method = "GET"
        path = "/api/v5/account/balance"
        body = ""
        secret = "test-secret-abcdef"

        # 标准库独立实现
        prehash = ts + method + path + body
        expected = base64.b64encode(
            hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()

        assert sign(ts, method, path, body, secret) == expected

    def test_sign_post_with_body(self):
        """POST + body 的签名正确。"""
        ts = "2024-01-01T10:30:00.000Z"
        secret = "secret"
        body = '{"instId":"BTC-USDT","side":"buy"}'
        result = sign(ts, "POST", "/api/v5/trade/order", body, secret)

        prehash = ts + "POST" + "/api/v5/trade/order" + body
        expected = base64.b64encode(
            hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        assert result == expected

    def test_sign_path_with_query(self):
        """request_path 含 query string 也要正确签名。"""
        ts = "2024-01-01T10:30:00.000Z"
        secret = "secret"
        path = "/api/v5/market/candles?instId=BTC-USDT&bar=1H"
        result = sign(ts, "GET", path, "", secret)

        prehash = ts + "GET" + path + ""
        expected = base64.b64encode(
            hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        assert result == expected

    def test_sign_method_case_insensitive(self):
        """方法大小写不敏感，内部转大写。"""
        ts = "2024-01-01T10:30:00.000Z"
        secret = "secret"
        path = "/api/v5/account/balance"
        body = ""
        lower = sign(ts, "get", path, body, secret)
        upper = sign(ts, "GET", path, body, secret)
        mixed = sign(ts, "GeT", path, body, secret)
        assert lower == upper == mixed

    def test_sign_different_input_produces_different_output(self):
        """不同输入必须产生不同签名。"""
        s1 = sign("2024-01-01T10:30:00.000Z", "GET", "/path", "", "secret")
        s2 = sign("2024-01-01T10:30:00.001Z", "GET", "/path", "", "secret")  # 时间差 1ms
        assert s1 != s2

    def test_sign_returns_base64_string(self):
        """返回值是 Base64 字符串。"""
        result = sign("2024-01-01T10:30:00.000Z", "GET", "/path", "", "secret")
        assert isinstance(result, str)
        # Base64 字符集
        b64_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
        assert all(c in b64_chars for c in result)

    def test_sign_empty_secret_raises(self):
        """空 secret 不应导致崩溃（生产不应出现，但代码应稳定）。"""
        # 空字符串作为 secret 仍然能签名，只是不安全
        result = sign("2024-01-01T10:30:00.000Z", "GET", "/path", "", "")
        assert isinstance(result, str)
        assert len(result) > 0


# =====================================================================
# RateLimiter 测试
# =====================================================================


class TestRateLimiter:
    """令牌桶限频器测试。"""

    def test_unknown_group_rejected(self):
        """未知 group 抛 ValueError。"""
        rl = RateLimiter()
        with pytest.raises(ValueError, match="unknown group"):
            rl.acquire("nonexistent", timeout=0.1)

    def test_acquire_capacity_tokens_without_waiting(self):
        """桶满时应能立即拿到 capacity 个令牌。"""
        # 自定义小桶：capacity=3, refill=1/s
        cfg = {"public": BucketConfig(capacity=3, refill_per_sec=1.0)}
        rl = RateLimiter(cfg)
        t0 = time.monotonic()
        for _ in range(3):
            rl.acquire("public", timeout=1.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"3 个令牌应立即拿到，实际耗时 {elapsed:.3f}s"

    def test_acquire_blocks_when_empty(self):
        """桶空时第 4 个令牌应阻塞约 1 秒（refill=1/s）。"""
        cfg = {"public": BucketConfig(capacity=3, refill_per_sec=10.0)}  # 10 令牌/秒
        rl = RateLimiter(cfg)
        for _ in range(3):
            rl.acquire("public", timeout=1.0)
        t0 = time.monotonic()
        rl.acquire("public", timeout=2.0)  # 需要等 0.1s 补一个令牌
        elapsed = time.monotonic() - t0
        assert 0.05 < elapsed < 0.3, f"应阻塞约 0.1s，实际 {elapsed:.3f}s"

    def test_acquire_timeout_raises(self):
        """桶空且 timeout 太短应抛 RateLimitTimeoutError。"""
        cfg = {"public": BucketConfig(capacity=1, refill_per_sec=0.5)}
        rl = RateLimiter(cfg)
        rl.acquire("public", timeout=0.5)  # 拿走唯一令牌
        # 桶空，需要 2 秒才补一个，但 timeout=0.1s
        with pytest.raises(RateLimitTimeoutError):
            rl.acquire("public", timeout=0.1)

    def test_penalize_blocks_acquire(self):
        """penalize 后该组 acquire 应阻塞到惩罚期结束。"""
        cfg = {"public": BucketConfig(capacity=10, refill_per_sec=10.0)}
        rl = RateLimiter(cfg)
        rl.penalize("public", seconds=0.3)
        t0 = time.monotonic()
        rl.acquire("public", timeout=2.0)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.25, f"惩罚期应阻塞 >=0.3s，实际 {elapsed:.3f}s"

    def test_penalize_clears_tokens(self):
        """penalize 应清空令牌桶且惩罚期内不补充。"""
        cfg = {"public": BucketConfig(capacity=10, refill_per_sec=100.0)}
        rl = RateLimiter(cfg)
        # 先确认桶满
        assert rl.tokens("public") >= 9.0
        rl.penalize("public", seconds=0.1)
        # 惩罚期内令牌应严格为 0（_refill 在惩罚期不会补充）
        assert rl.tokens("public") == 0.0

    def test_refill_over_time(self):
        """令牌应随时间补充。"""
        cfg = {"public": BucketConfig(capacity=10, refill_per_sec=100.0)}
        rl = RateLimiter(cfg)
        rl.penalize("public", seconds=0.05)
        time.sleep(0.1)  # 等补充
        # 0.1s × 100/s = 10 个令牌，但被 capacity=10 上限
        tokens = rl.tokens("public")
        assert tokens > 5, f"应补充到接近 10，实际 {tokens:.2f}"

    def test_different_groups_independent(self):
        """不同组的令牌桶相互独立。"""
        cfg = {
            "public": BucketConfig(capacity=1, refill_per_sec=1.0),
            "history": BucketConfig(capacity=1, refill_per_sec=1.0),
        }
        rl = RateLimiter(cfg)
        # 拿光 public 但不影响 history
        rl.acquire("public", timeout=0.5)
        # history 仍能立即拿到
        t0 = time.monotonic()
        rl.acquire("history", timeout=0.5)
        assert time.monotonic() - t0 < 0.05


# =====================================================================
# OKXClient 测试（用 respx mock HTTP）
# =====================================================================


# 测试用 OKX 凭证
TEST_API_KEY = "test-api-key"
TEST_SECRET = "test-secret"
TEST_PASSPHRASE = "test-passphrase"


def _make_settings(demo: bool = False) -> OKXSettings:
    """构造测试用 OKXSettings。"""
    return OKXSettings(
        api_key=TEST_API_KEY,
        secret=TEST_SECRET,
        passphrase=TEST_PASSPHRASE,
        demo=demo,
    )


def _make_client_no_sync(
    settings: OKXSettings | None = None,
    rate_limiter: RateLimiter | None = None,
) -> OKXClient:
    """构造 OKXClient 但跳过初始化时的时间同步（避免联网）。"""
    s = settings or _make_settings()
    return OKXClient(s, rate_limiter=rate_limiter, sync_time_on_init=False)


class TestOKXClientGroup:
    """_group_of 路径到限频组的映射。"""

    def test_market_path_is_public(self):
        assert OKXClient._group_of("/api/v5/market/candles") == "public"

    def test_public_path_is_public(self):
        assert OKXClient._group_of("/api/v5/public/instruments") == "public"

    def test_account_path_is_account(self):
        assert OKXClient._group_of("/api/v5/account/balance") == "account"

    def test_trade_order_path(self):
        assert OKXClient._group_of("/api/v5/trade/order") == "trade_order"

    def test_trade_batch_path(self):
        assert OKXClient._group_of("/api/v5/trade/batch-orders") == "trade_batch"

    def test_trade_cancel_path(self):
        assert OKXClient._group_of("/api/v5/trade/cancel-order") == "trade_cancel"

    def test_trade_fills_history(self):
        assert OKXClient._group_of("/api/v5/trade/fills") == "history"

    def test_unknown_path_defaults_public(self):
        assert OKXClient._group_of("/api/v5/unknown/endpoint") == "public"


class TestOKXClientTimestamp:
    """_timestamp 生成 ISO 8601 UTC 毫秒时间戳。"""

    def test_timestamp_format(self):
        c = _make_client_no_sync()
        ts = c._timestamp()
        # 格式：YYYY-MM-DDTHH:MM:SS.mmmZ
        assert len(ts) == 24
        assert ts[4] == "-" and ts[7] == "-" and ts[10] == "T"
        assert ts[13] == ":" and ts[16] == ":" and ts[19] == "."
        assert ts[23] == "Z"

    def test_timestamp_with_offset(self):
        """时间偏差应体现在时间戳上。"""
        c = _make_client_no_sync()
        # 模拟服务端比本地快 5000ms
        c._server_time_offset_ms = 5000
        ts = c._timestamp()
        # 解析时间戳
        from datetime import UTC, datetime

        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        local_now_ms = datetime.now(UTC).timestamp() * 1000
        diff = dt.timestamp() * 1000 - local_now_ms
        assert 4000 < diff < 6000, f"应偏移约 5000ms，实际 {diff:.0f}ms"


class TestOKXClientHeaders:
    """_headers 生成正确 HTTP 头。"""

    def test_headers_contain_required_keys(self):
        c = _make_client_no_sync()
        h = c._headers("GET", "/api/v5/account/balance", "2024-01-01T10:30:00.000Z", "")
        assert h["OK-ACCESS-KEY"] == TEST_API_KEY
        assert h["OK-ACCESS-PASSPHRASE"] == TEST_PASSPHRASE
        assert h["OK-ACCESS-TIMESTAMP"] == "2024-01-01T10:30:00.000Z"
        assert h["Content-Type"] == "application/json"
        assert "OK-ACCESS-SIGN" in h
        # 真实模式下不应有 x-simulated-trading
        assert "x-simulated-trading" not in h

    def test_headers_demo_mode(self):
        c = _make_client_no_sync(_make_settings(demo=True))
        h = c._headers("GET", "/path", "ts", "")
        assert h.get("x-simulated-trading") == "1"

    def test_headers_sign_correctly(self):
        """签名应与 Signer.sign 直接调用结果一致。"""
        c = _make_client_no_sync()
        ts = "2024-01-01T10:30:00.000Z"
        path = "/api/v5/account/balance"
        h = c._headers("GET", path, ts, "")
        assert h["OK-ACCESS-SIGN"] == sign(ts, "GET", path, "", TEST_SECRET)


# =====================================================================
# OKXClient HTTP 错误处理测试（mock httpx）
# =====================================================================


class TestOKXClientErrorHandling:
    """错误码分发与重试逻辑。"""

    def test_success_returns_data(self):
        """code=0 返回 data 字段。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(
            200,
            json={"code": "0", "msg": "", "data": [{"foo": "bar"}]},
        )
        with patch.object(c._client, "get", return_value=mock_resp):
            data = c.get("/api/v5/market/ticker", {"instId": "BTC-USDT"})
        assert data == [{"foo": "bar"}]

    def test_auth_error_immediate_raise(self):
        """50101 鉴权失败立即抛 AuthError，不重试。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(
            200,
            json={"code": "50101", "msg": "API key invalid", "data": []},
        )
        with patch.object(c._client, "get", return_value=mock_resp) as mock_get:
            with pytest.raises(AuthError) as exc_info:
                c.get("/api/v5/account/balance")
            assert exc_info.value.code == "50101"
            # 应只调用 1 次（不重试）
            assert mock_get.call_count == 1

    def test_insufficient_balance_raises(self):
        """51008 余额不足立即抛 InsufficientBalanceError。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(
            200,
            json={"code": "51008", "msg": "insufficient balance", "data": []},
        )
        with patch.object(c._client, "get", return_value=mock_resp):
            with pytest.raises(InsufficientBalanceError):
                c.get("/api/v5/account/balance")

    def test_rate_limit_50011_retries_with_penalize(self):
        """50011 应触发 penalize + 重试。"""
        c = _make_client_no_sync()
        rl = RateLimiter()
        c._rl = rl  # 替换为可监听的限频器
        responses = [
            httpx.Response(200, json={"code": "50011", "msg": "rate limit", "data": []}),
            httpx.Response(200, json={"code": "0", "msg": "", "data": [{"ok": True}]}),
        ]
        with patch.object(c._client, "get", side_effect=responses):
            with patch.object(rl, "penalize") as mock_penalize:
                with patch("time.sleep"):  # 跳过真实 sleep
                    data = c.get("/api/v5/market/candles")
        assert data == [{"ok": True}]
        mock_penalize.assert_called_once_with("public", 2.0)

    def test_server_error_5xx_retries_with_backoff(self):
        """50001 等 5xx 应重试，指数退避。"""
        c = _make_client_no_sync()
        responses = [
            httpx.Response(200, json={"code": "50001", "msg": "server error", "data": []}),
            httpx.Response(200, json={"code": "0", "msg": "", "data": [{"ok": True}]}),
        ]
        with patch.object(c._client, "get", side_effect=responses):
            with patch("time.sleep") as mock_sleep:
                data = c.get("/api/v5/market/candles")
        assert data == [{"ok": True}]
        mock_sleep.assert_called_once()  # 重试前 sleep 1 次

    def test_server_error_exhausted_retries(self):
        """3 次都 5xx 应抛 ServerError。"""
        c = _make_client_no_sync()
        resp = httpx.Response(200, json={"code": "50001", "msg": "server error", "data": []})
        with patch.object(c._client, "get", return_value=resp):
            with patch("time.sleep"):
                with pytest.raises(ServerError) as exc_info:
                    c.get("/api/v5/market/candles")
            assert exc_info.value.code == "50001"

    def test_network_error_retries(self):
        """httpx.NetworkError 应重试。"""
        c = _make_client_no_sync()
        ok_resp = httpx.Response(
            200, json={"code": "0", "msg": "", "data": [{"ok": True}]}
        )
        with patch.object(
            c._client, "get", side_effect=[httpx.NetworkError("conn refused"), ok_resp]
        ):
            data = c.get("/api/v5/market/candles")
        assert data == [{"ok": True}]

    def test_network_error_exhausted(self):
        """3 次都网络错误应抛 NetworkError。"""
        c = _make_client_no_sync()
        with patch.object(c._client, "get", side_effect=httpx.NetworkError("conn refused")):
            with pytest.raises(NetworkError):
                c.get("/api/v5/market/candles")

    def test_unknown_business_code_raises_valueerror(self):
        """非已知业务码应抛 ValueError。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(
            200,
            json={"code": "99999", "msg": "unknown", "data": []},
        )
        with patch.object(c._client, "get", return_value=mock_resp):
            with pytest.raises(ValueError, match="unknown response"):
                c.get("/api/v5/market/candles")

    def test_param_error_51000_no_retry(self):
        """51000 参数错误应立即抛 ValueError，不重试。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(
            200,
            json={"code": "51000", "msg": "Parameter error", "data": []},
        )
        with patch.object(c._client, "get", return_value=mock_resp) as mock_get:
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(ValueError, match="51000"):
                    c.get("/api/v5/account/positions")
        # 只调用 1 次（不重试）
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    def test_business_error_51xxx_no_retry(self):
        """511xx 业务错误应立即抛 ValueError，不重试。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(
            200,
            json={"code": "51112", "msg": "business error", "data": []},
        )
        with patch.object(c._client, "get", return_value=mock_resp) as mock_get:
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(ValueError, match="51112"):
                    c.get("/api/v5/account/positions")
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()


# =====================================================================
# OKXClient 高层方法测试
# =====================================================================


class TestOKXClientHighLevelMethods:
    """get_history_candles / get_instruments / get_funding_rate 测试。"""

    def test_get_history_candles_returns_bars(self):
        """get_history_candles 应返回 Bar 列表。"""
        c = _make_client_no_sync()
        # OKX 真实响应格式：list of [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        okx_data = [
            ["1700000000000", "50000", "50500", "49800", "50200", "100", "0", "5000000", "1"],
            ["1700003600000", "50200", "50800", "50100", "50600", "150", "0", "7500000", "1"],
        ]
        mock_resp = httpx.Response(
            200, json={"code": "0", "msg": "", "data": okx_data}
        )
        with patch.object(c._client, "get", return_value=mock_resp):
            bars = c.get_history_candles("BTC-USDT", "1H", limit=2)
        assert len(bars) == 2
        assert all(isinstance(b, Bar) for b in bars)
        assert bars[0].inst_id == "BTC-USDT"
        assert bars[0].bar_size == "1H"
        assert bars[0].confirm == 1

    def test_get_history_candles_empty_data(self):
        """空数据应返回空列表。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(200, json={"code": "0", "msg": "", "data": []})
        with patch.object(c._client, "get", return_value=mock_resp):
            bars = c.get_history_candles("BTC-USDT", "1H")
        assert bars == []

    def test_get_instruments_returns_list(self):
        """get_instruments 应返回 Instrument 列表。"""
        c = _make_client_no_sync()
        okx_data = [
            {
                "instId": "BTC-USDT",
                "instType": "SPOT",
                "baseCcy": "BTC",
                "quoteCcy": "USDT",
                "settleCcy": "",
                "tickSz": "0.1",
                "lotSz": "0.00001",
                "minSz": "0.00001",
                "ctVal": "1",
                "state": "live",
                "listTime": "1630473600000",
            }
        ]
        mock_resp = httpx.Response(
            200, json={"code": "0", "msg": "", "data": okx_data}
        )
        with patch.object(c._client, "get", return_value=mock_resp):
            instruments = c.get_instruments("SPOT")
        assert len(instruments) == 1
        inst = instruments[0]
        assert isinstance(inst, Instrument)
        assert inst.inst_id == "BTC-USDT"
        assert inst.inst_type == "SPOT"
        assert inst.base_ccy == "BTC"
        assert inst.quote_ccy == "USDT"
        assert inst.settle_ccy == ""
        assert inst.tick_sz == Decimal("0.1")

    def test_get_instrument_finds_swap_and_caches(self):
        """get_instrument 按 instId 探测类型，命中后缓存不再请求。"""
        c = _make_client_no_sync()
        swap_data = [
            {
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "baseCcy": "",
                "quoteCcy": "",
                "settleCcy": "USDT",
                "tickSz": "0.1",
                "lotSz": "0.01",
                "minSz": "0.01",
                "ctVal": "0.01",
                "state": "live",
                "listTime": "1630473600000",
            }
        ]

        def fake_get(path, params=None, **kwargs):
            # 真实的 OKX：类型不对时返回 51001，而不是空 data
            if (params or {}).get("instType") != "SWAP":
                return httpx.Response(
                    200,
                    json={
                        "code": "51001",
                        "msg": "Instrument ID doesn't exist",
                        "data": [],
                    },
                )
            return httpx.Response(
                200, json={"code": "0", "msg": "", "data": swap_data}
            )

        with patch.object(c._client, "get", side_effect=fake_get) as get:
            inst = c.get_instrument("BTC-USDT-SWAP")
            again = c.get_instrument("BTC-USDT-SWAP")

        assert inst is not None
        assert inst.inst_type == "SWAP"
        assert inst.ct_val == Decimal("0.01")
        assert again is inst
        # SPOT + SWAP 各一次，第二次命中缓存
        assert get.call_count == 2

    def test_get_instrument_caches_miss(self):
        """查不到也缓存 None，避免每轮重复探测。"""
        c = _make_client_no_sync()
        empty = httpx.Response(200, json={"code": "0", "msg": "", "data": []})
        with patch.object(c._client, "get", return_value=empty) as get:
            assert c.get_instrument("NOPE-USDT-SWAP") is None
            assert c.get_instrument("NOPE-USDT-SWAP") is None
        assert get.call_count == 3  # SPOT / SWAP / FUTURES，只探测一轮

    def test_get_funding_rate_returns_none_when_empty(self):
        """get_funding_rate 空数据应返回 None。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(200, json={"code": "0", "msg": "", "data": []})
        with patch.object(c._client, "get", return_value=mock_resp):
            result = c.get_funding_rate("BTC-USDT-SWAP")
        assert result is None

    def test_get_funding_rate_returns_funding_rate(self):
        """get_funding_rate 应返回 FundingRate 对象。"""
        c = _make_client_no_sync()
        okx_data = [
            {
                "instId": "BTC-USDT-SWAP",
                "fundingRate": "0.00015",
                "fundingTime": "1700000000000",
                "nextFundingTime": "1700086400000",
            }
        ]
        mock_resp = httpx.Response(
            200, json={"code": "0", "msg": "", "data": okx_data}
        )
        with patch.object(c._client, "get", return_value=mock_resp):
            result = c.get_funding_rate("BTC-USDT-SWAP")
        assert isinstance(result, FundingRate)
        assert result.inst_id == "BTC-USDT-SWAP"
        assert result.rate == Decimal("0.00015")
        assert result.ts == 1700000000000

    def test_get_balance_returns_dict(self):
        """get_balance 应返回 data[0]。"""
        c = _make_client_no_sync()
        okx_data = [{"totalEq": "10000", "details": []}]
        mock_resp = httpx.Response(
            200, json={"code": "0", "msg": "", "data": okx_data}
        )
        with patch.object(c._client, "get", return_value=mock_resp):
            result = c.get_balance()
        assert result == {"totalEq": "10000", "details": []}

    def test_get_balance_empty_returns_empty_dict(self):
        """get_balance 空数据应返回空 dict。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(200, json={"code": "0", "msg": "", "data": []})
        with patch.object(c._client, "get", return_value=mock_resp):
            assert c.get_balance() == {}

    def test_get_positions_returns_list(self):
        """get_positions 应返回 data 列表。"""
        c = _make_client_no_sync()
        okx_data = [{"instId": "BTC-USDT", "pos": "0.1"}]
        mock_resp = httpx.Response(
            200, json={"code": "0", "msg": "", "data": okx_data}
        )
        with patch.object(c._client, "get", return_value=mock_resp) as mock_get:
            result = c.get_positions()
        assert result == okx_data
        # 应携带 instType 参数
        assert mock_get.call_args.kwargs["params"] == {"instType": "SPOT"}

    def test_get_positions_empty_returns_empty_list(self):
        """get_positions 空数据应返回空列表。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(200, json={"code": "0", "msg": "", "data": []})
        with patch.object(c._client, "get", return_value=mock_resp):
            assert c.get_positions() == []

    def test_get_error_rate_none_without_requests(self):
        """无请求时 get_error_rate 应返回 None。"""
        c = _make_client_no_sync()
        assert c.get_error_rate() is None

    def test_get_error_rate_after_success(self):
        """成功请求后错误率应为 0。"""
        c = _make_client_no_sync()
        okx_data = [{"totalEq": "10000", "details": []}]
        mock_resp = httpx.Response(
            200, json={"code": "0", "msg": "", "data": okx_data}
        )
        with patch.object(c._client, "get", return_value=mock_resp):
            c.get_balance()
        assert c.get_error_rate() == 0.0

    def test_get_error_rate_after_auth_failure(self):
        """鉴权失败后错误率应为 1。"""
        c = _make_client_no_sync()
        mock_resp = httpx.Response(
            200, json={"code": "50101", "msg": "API key invalid", "data": []}
        )
        with patch.object(c._client, "get", return_value=mock_resp):
            with pytest.raises(AuthError):
                c.get("/api/v5/account/balance")
        assert c.get_error_rate() == 1.0

    def test_sync_time_success(self):
        """sync_time 应更新 _server_time_offset_ms。"""
        c = _make_client_no_sync()
        server_ts = 1700000000000
        mock_resp = httpx.Response(
            200, json={"code": "0", "msg": "", "data": [{"ts": str(server_ts)}]}
        )
        with patch.object(c._client, "get", return_value=mock_resp):
            offset = c.sync_time()
        # offset = server - local，本地时间小于服务端时间
        assert offset > 1700000000000 - int(time.time() * 1000) - 1000
        assert offset < 1700000000000 - int(time.time() * 1000) + 1000

    def test_sync_time_failure_raises_network_error(self):
        """sync_time 失败应抛 NetworkError。"""
        c = _make_client_no_sync()
        with patch.object(c._client, "get", side_effect=httpx.NetworkError("conn refused")):
            with pytest.raises(NetworkError, match="time sync failed"):
                c.sync_time()
