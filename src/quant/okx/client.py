"""OKX REST 客户端（spec §4.3）。

同步 httpx 客户端，自动处理：
- HMAC-SHA256 签名
- 端点组分桶限频
- 错误码分发到具体异常
- 重试（网络错误/5xx/50011）
- 服务端时间同步

使用：
    from quant.foundation.config import Settings
    from quant.okx.client import OKXClient

    settings = Settings.load(Path("config/config.toml"))
    client = OKXClient(settings.okx)
    bars = client.get_history_candles("BTC-USDT", "1H", limit=100)
    client.close()
"""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from quant.foundation.config import OKXSettings
from quant.foundation.errors import (
    AuthError,
    InsufficientBalanceError,
    NetworkError,
    QuantError,
    RateLimitError,
    ServerError,
)
from quant.foundation.logger import get_logger
from quant.models import Bar, FundingRate, Instrument
from quant.okx.rate_limiter import RateLimiter
from quant.okx.signer import sign

# 端点路径 → 限频组映射（spec §4.2）
PATH_TO_GROUP: list[tuple[str, str]] = [
    (r"^/api/v5/market/", "public"),
    (r"^/api/v5/public/", "public"),
    (r"^/api/v5/account/", "account"),
    (r"^/api/v5/trade/order\b", "trade_order"),
    (r"^/api/v5/trade/batch-orders", "trade_batch"),
    (r"^/api/v5/trade/cancel", "trade_cancel"),
    (r"^/api/v5/trade/amend", "trade_order"),
    (r"^/api/v5/trade/orders-history", "history"),
    (r"^/api/v5/trade/fills", "history"),
    (r"^/api/v5/trade/orders-algo", "algo"),
]

# 鉴权错误码（含 50110 IP 不在白名单：不可重试）
_AUTH_CODES = ("50101", "50102", "50111", "50113", "50105", "50103", "50110")
# 余额不足
_INSUFFICIENT_CODES = ("51008", "51121", "51124")
# 按 instId 查产品配置时的探测顺序（instId 本身不含类型信息）
_INST_TYPES = ("SPOT", "SWAP", "FUTURES")


class OKXClient:
    """OKX V5 REST 客户端。"""

    BASE_URL = "https://www.okx.com"
    MAX_RETRIES = 3

    def __init__(
        self,
        settings: OKXSettings,
        rate_limiter: RateLimiter | None = None,
        demo: bool | None = None,
        sync_time_on_init: bool = True,
    ) -> None:
        self._s = settings
        self._demo = demo if demo is not None else settings.demo
        self._rl = rate_limiter or RateLimiter()
        self._log = get_logger("okx.client")
        self._client = httpx.Client(timeout=30.0)
        self._server_time_offset_ms = 0
        self._request_count = 0
        self._error_count = 0
        self._instrument_cache: dict[str, Instrument | None] = {}
        if sync_time_on_init:
            self.sync_time()

    # -------------------- 公开方法 --------------------

    def get(self, path: str, params: dict | None = None) -> Any:
        """发送 GET 请求，返回 data 字段内容。"""
        request_path = path
        if params:
            request_path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        return self._request("GET", path, request_path, params=params, body="")

    def post(self, path: str, body: dict) -> Any:
        """发送 POST 请求，返回 data 字段内容。"""
        body_str = json.dumps(body, separators=(",", ":"))
        return self._request("POST", path, path, params=None, body=body_str)

    def close(self) -> None:
        """关闭底层 httpx 连接。"""
        self._client.close()

    def __enter__(self) -> OKXClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -------------------- 高层便利方法 --------------------

    def sync_time(self) -> int:
        """同步服务端时间，返回 offset_ms（server - local）。

        |offset_ms| > 15000 打 WARNING；调用失败抛错。
        """
        try:
            resp = self._client.get(f"{self.BASE_URL}/api/v5/public/time")
            server_ts = int(resp.json()["data"][0]["ts"])
            local_ts = int(datetime.now(UTC).timestamp() * 1000)
            self._server_time_offset_ms = server_ts - local_ts
            if abs(self._server_time_offset_ms) > 15000:
                self._log.warning(
                    f"large time offset: {self._server_time_offset_ms}ms"
                )
            else:
                self._log.debug(
                    f"sync_time ok, offset_ms={self._server_time_offset_ms}"
                )
            return self._server_time_offset_ms
        except Exception as e:
            self._log.error(f"time sync failed: {e}")
            raise NetworkError(f"time sync failed: {e}") from e

    def get_history_candles(
        self,
        inst_id: str,
        bar: str = "1H",
        after: int | None = None,
        before: int | None = None,
        limit: int = 100,
    ) -> list[Bar]:
        """拉取历史 K 线（已结束）。

        端点：GET /api/v5/market/history-candles
        - after:  返回此时间戳之前的 K 线（旧数据）
        - before: 返回此时间戳之后的 K 线（新数据）
        - limit:  1-100，默认 100

        Returns:
            Bar 列表（时间从新到旧，OKX 返回顺序）
        """
        params: dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        if before is not None:
            params["before"] = str(before)
        data = self.get("/api/v5/market/history-candles", params)
        # data 是 list of list[str]
        if not isinstance(data, list):
            return []
        bars = [Bar.from_okx_raw(raw, inst_id=inst_id, bar_size=bar) for raw in data]
        self._log.info(
            f"GET /api/v5/market/history-candles 200, {len(bars)} bars "
            f"for {inst_id} {bar}"
        )
        return bars

    def get_candles(
        self,
        inst_id: str,
        bar: str = "1H",
        limit: int = 100,
    ) -> list[Bar]:
        """拉取最新 K 线（含未结束的当前根）。

        端点：GET /api/v5/market/candles
        """
        params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        data = self.get("/api/v5/market/candles", params)
        if not isinstance(data, list):
            return []
        return [Bar.from_okx_raw(raw, inst_id=inst_id, bar_size=bar) for raw in data]

    def get_instruments(self, inst_type: str = "SPOT") -> list[Instrument]:
        """拉取产品配置列表。

        端点：GET /api/v5/public/instruments?instType=SPOT/SWAP/FUTURES/OPTION
        """
        params = {"instType": inst_type}
        data = self.get("/api/v5/public/instruments", params)
        if not isinstance(data, list):
            return []
        return [_instrument_from_raw(r) for r in data]

    def get_instrument(self, inst_id: str) -> Instrument | None:
        """按 instId 查单个产品配置（结果进程内缓存）。

        OKX 的 /public/instruments 必须带 instType，类型不对时会返回
        51001（Instrument ID doesn't exist）而不是空列表，所以这里逐个类型
        探测并把该错误当作「这个类型下没有」继续。网络/鉴权错误照常抛出，
        不会把瞬时故障缓存成 None。产品面值在进程生命周期内不会变化，
        因此不做 TTL。
        """
        if inst_id in self._instrument_cache:
            return self._instrument_cache[inst_id]
        for inst_type in _INST_TYPES:
            try:
                data = self.get(
                    "/api/v5/public/instruments",
                    {"instType": inst_type, "instId": inst_id},
                )
            except ValueError:
                # 该 instType 下没有这个 instId
                continue
            if isinstance(data, list) and data:
                instrument = _instrument_from_raw(data[0])
                self._instrument_cache[inst_id] = instrument
                return instrument
        self._instrument_cache[inst_id] = None
        return None

    def get_funding_rate(self, inst_id: str) -> FundingRate | None:
        """拉取当前资金费率（SWAP 专用）。

        端点：GET /api/v5/public/funding-rate?instId=...
        """
        data = self.get("/api/v5/public/funding-rate", {"instId": inst_id})
        if not isinstance(data, list) or not data:
            return None
        return _funding_rate_from_raw(data[0])

    def get_balance(self) -> dict:
        """查询账户余额（M4 监控）。

        端点：GET /api/v5/account/balance
        返回 data[0]（含 totalEq/adjEq/mgnRatio/upl 与 details 币种明细）。
        """
        data = self.get("/api/v5/account/balance")
        if not isinstance(data, list) or not data:
            return {}
        return data[0]

    def get_positions(self, inst_type: str = "SPOT") -> list[dict]:
        """查询持仓（M4 监控）。

        端点：GET /api/v5/account/positions?instType=SPOT
        返回 data 列表（每项含 instId/pos/avgPx/upl/lever 等）。
        """
        data = self.get("/api/v5/account/positions", {"instType": inst_type})
        if not isinstance(data, list):
            return []
        return data

    def get_error_rate(self) -> float | None:
        """累计请求错误率（0~1）；尚无请求时返回 None（M4 监控）。

        按每次 `_request` 调用的最终结果计数（成功/失败）。
        """
        if self._request_count == 0:
            return None
        return self._error_count / self._request_count

    # -------------------- 内部 --------------------

    def _record_request(self, ok: bool) -> None:
        """记录一次请求的最终结果（成功/失败），供错误率统计。"""
        self._request_count += 1
        if not ok:
            self._error_count += 1

    def _request(
        self,
        method: str,
        path: str,
        request_path_for_sign: str,
        params: dict | None = None,
        body: str = "",
    ) -> Any:
        group = self._group_of(path)
        url = self.BASE_URL + path

        last_exc: QuantError | None = None
        for attempt in range(self.MAX_RETRIES):
            self._rl.acquire(group)
            ts = self._timestamp()
            headers = self._headers(method, request_path_for_sign, ts, body)

            try:
                t0 = time.perf_counter()
                if method == "GET":
                    resp = self._client.get(url, params=params, headers=headers)
                else:
                    resp = self._client.post(url, content=body, headers=headers)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                payload = resp.json()
            except httpx.NetworkError as e:
                last_exc = NetworkError(str(e))
                self._log.warning(f"network error attempt {attempt + 1}: {e}")
                continue
            except httpx.TimeoutException as e:
                last_exc = NetworkError(f"timeout: {e}")
                self._log.warning(f"timeout attempt {attempt + 1}: {e}")
                continue

            code = str(payload.get("code", ""))
            msg = payload.get("msg", "")

            if code == "0":
                self._log.debug(
                    f"{method} {path} {resp.status_code} "
                    f"({elapsed_ms:.0f}ms)"
                )
                self._record_request(True)
                return payload.get("data")

            # 限频：阻塞 2s 重试
            if code == "50011":
                self._log.warning(
                    f"rate limited (50011), sleeping 2s, msg={msg}"
                )
                self._rl.penalize(group, 2.0)
                last_exc = RateLimitError(msg)
                continue

            # 鉴权错误：立即抛
            if code in _AUTH_CODES:
                self._record_request(False)
                raise AuthError(code, msg)

            # 余额不足：立即抛
            if code in _INSUFFICIENT_CODES:
                self._record_request(False)
                raise InsufficientBalanceError(msg)

            # 服务端 5xx（仅 500xx 系统错误）：重试，指数退避。
            # 注意：50011（限频）已在上方单独处理；501xx（鉴权）已在 _AUTH_CODES。
            # 510xx/511xx 是参数/业务错误（如 51000 Parameter error），不可重试，
            # 落到下方 ValueError 分支，避免无谓重试消耗限频配额与延迟。
            if code.startswith("500"):
                if attempt < self.MAX_RETRIES - 1:
                    self._log.warning(
                        f"server error {code}, retrying ({attempt + 1}/{self.MAX_RETRIES})"
                    )
                    last_exc = ServerError(code, msg)
                    time.sleep(2 ** attempt)  # 1s, 2s 指数退避
                    continue
                self._record_request(False)
                raise ServerError(code, msg)

            # 其他业务错误码：抛 ValueError（不可重试）
            self._record_request(False)
            raise ValueError(f"unknown response: code={code} msg={msg}")

        # 重试耗尽
        if last_exc is not None:
            self._record_request(False)
            raise last_exc
        self._record_request(False)
        raise RuntimeError("exhausted retries without specific error")

    def _headers(self, method: str, path: str, ts: str, body: str) -> dict:
        headers = {
            "OK-ACCESS-KEY": self._s.api_key,
            "OK-ACCESS-SIGN": sign(ts, method, path, body, self._s.secret),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._s.passphrase,
            "Content-Type": "application/json",
        }
        if self._demo:
            headers["x-simulated-trading"] = "1"
        return headers

    def _timestamp(self) -> str:
        """生成 ISO 8601 UTC 毫秒时间戳，校正服务端时差。"""
        now_ms = datetime.now(UTC).timestamp() * 1000
        adjusted_ms = now_ms + self._server_time_offset_ms
        dt = datetime.fromtimestamp(adjusted_ms / 1000, tz=UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    @staticmethod
    def _group_of(path: str) -> str:
        """根据路径返回限频组名。"""
        for pattern, group in PATH_TO_GROUP:
            if re.match(pattern, path):
                return group
        return "public"


# -------------------- 响应 → 模型 转换函数 --------------------


def _instrument_from_raw(raw: dict) -> Instrument:
    """从 OKX /public/instruments 响应项构造 Instrument。

    OKX SPOT 响应中 settle_ccy/ct_val 为空字符串，需给默认值。
    """
    return Instrument(
        inst_id=raw["instId"],
        inst_type=raw["instType"],
        base_ccy=raw.get("baseCcy", "") or "",
        quote_ccy=raw.get("quoteCcy", "") or "",
        settle_ccy=raw.get("settleCcy", "") or "",
        tick_sz=Decimal(raw.get("tickSz") or "0.01"),
        lot_sz=Decimal(raw.get("lotSz") or "0.0001"),
        min_sz=Decimal(raw.get("minSz") or "0.0001"),
        ct_val=Decimal(raw.get("ctVal") or "1"),
        state=raw.get("state", "") or "",
        list_time=int(raw.get("listTime") or "0"),
    )


def _funding_rate_from_raw(raw: dict) -> FundingRate:
    """从 OKX /public/funding-rate 响应项构造 FundingRate。"""
    return FundingRate(
        inst_id=raw["instId"],
        ts=int(raw.get("fundingTime", "0")),
        rate=Decimal(raw.get("fundingRate", "0")),
        next_funding_ts=int(raw.get("nextFundingTime", "0")),
    )


__all__ = ["OKXClient", "PATH_TO_GROUP"]
