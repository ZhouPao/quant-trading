# M1 数据底座 详细设计文档

> 文档版本：v1.1
> 创建日期：2026-10-17
> 状态：待用户审阅
> 上游需求：[2026-10-17-quant-trading-platform-requirements.md v0.5](../requirements/2026-10-17-quant-trading-platform-requirements.md)
> 关联参考：[OKX API速查](../reference/OKX_API_REFERENCE.md)
> 设计方案：方案A（最小依赖、自封装、同步优先）。M1 本身不变；本版仅更新 §12 接口契约以适配 PRD v0.5 选定的 webhook 方案B（FastAPI+Redis+worker）。

---

## 目录

1. [引言](#1-引言)
2. [整体架构](#2-整体架构)
3. [模块清单与依赖关系](#3-模块清单与依赖关系)
4. [OKX 客户端设计](#4-okx-客户端设计)
5. [数据模型](#5-数据模型)
6. [存储设计](#6-存储设计)
7. [配置设计](#7-配置设计)
8. [日志与可观测性](#8-日志与可观测性)
9. [错误处理与重试策略](#9-错误处理与重试策略)
10. [测试策略](#10-测试策略)
11. [CLI 入口](#11-cli-入口)
12. [M2-M6 接口契约](#12-m2-m6-接口契约)
13. [实现顺序](#13-实现顺序)
14. [验收标准映射](#14-验收标准映射)
15. [风险与开放问题](#15-风险与开放问题)
16. [变更记录](#16-变更记录)

---

## 1. 引言

### 1.1 目的

本设计文档定义量化交易平台 M1 里程碑（数据底座）的详细设计。M1 是整个项目的地基，后续所有模块（回测、模拟盘、实盘、监控）都依赖其产出的 OKX 客户端、数据模型、存储层。

### 1.2 范围

- **包含**：OKX REST/WS 客户端、数据模型、parquet 存储、配置加载、日志、限频器、数据校验、多品种对齐、CLI 入口
- **不包含**：策略接口、回测引擎、订单管理、监控告警、实盘交易（这些在 M2-M5 各自的 spec 中设计）

### 1.3 设计原则

| 原则 | 落地 |
|---|---|
| YAGNI | 不引入 asyncio、不引入数据库、不引入策略抽象 |
| 最小依赖 | 5 个核心包 + 4 个辅助包，详见 §3 |
| 同步优先 | M1 全程同步代码；M3 模拟盘时再转异步（httpx 天然支持双模式） |
| 代码全可见 | 不用 OKX 官方 SDK，所有 HTTP/WS 逻辑自封装 |
| 边界校验 | 用 pydantic 在 IO 边界校验，内部用 frozen dataclass |
| 精度保证 | 价格/数量用 `Decimal`，避免浮点表示误差 |

### 1.4 假设与依赖

- 上游 PRD 的所有假设（§1.4）继续生效
- OKX V5 API 在 M1 实施期间不发生破坏性变更
- Python 3.11+ 可用
- 用户已创建 OKX 模拟盘 API Key 并配置到本地

---

## 2. 整体架构

### 2.1 分层架构

```
┌──────────────────────────────────────────────────┐
│                  CLI / Scripts                    │  ← fetch_candles.py
│                                                   │     validate_data.py
├──────────────────────────────────────────────────┤
│              Application Layer                    │
│   CandleFetcher | InstrumentFetcher              │  ← 业务编排
│   DataValidator | DataAligner                     │
├──────────────────────────────────────────────────┤
│            Infrastructure Layer                   │
│   OKXClient | RateLimiter | Signer | WSClient     │  ← 底层能力
│   BarStorage | InstrumentStorage                  │
├──────────────────────────────────────────────────┤
│                  Foundation                       │
│   Settings | Logger | Models | Errors            │  ← 全局基础
└──────────────────────────────────────────────────┘
```

**依赖方向**：上层依赖下层，下层不感知上层。`Foundation` 被所有层引用，但不引用任何上层。

### 2.2 不引入的概念

M1 严格遵守"数据底座"边界，**不出现**以下概念：
- `Strategy`（M2）
- `Signal`（M2）
- `Order`、`Broker`、`Position`（M3/M5）
- `Monitor`、`Alerter`（M4）

如果实施时发现需要这些，说明设计被打破了，应回到 brainstorming。

---

## 3. 模块清单与依赖关系

### 3.1 目录结构

```
quant-trading/
├── src/
│   └── quant/
│       ├── __init__.py
│       ├── foundation/
│       │   ├── __init__.py
│       │   ├── config.py          # Settings 类
│       │   ├── logger.py          # loguru 包装
│       │   ├── errors.py          # 异常基类与具体异常
│       │   └── types.py           # Decimal 等类型别名
│       ├── models/
│       │   ├── __init__.py
│       │   ├── bar.py             # Bar dataclass
│       │   ├── tick.py            # Tick dataclass
│       │   ├── instrument.py      # Instrument dataclass
│       │   └── funding_rate.py    # FundingRate dataclass
│       ├── okx/
│       │   ├── __init__.py
│       │   ├── client.py          # OKXClient
│       │   ├── signer.py          # Signer
│       │   ├── rate_limiter.py    # RateLimiter
│       │   ├── ws_client.py       # WSClient
│       │   └── schemas.py         # pydantic 响应 schema
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── bar_storage.py     # BarStorage
│       │   ├── instrument_storage.py
│       │   ├── validator.py       # DataValidator
│       │   └── aligner.py         # DataAligner
│       ├── fetcher/
│       │   ├── __init__.py
│       │   ├── candle_fetcher.py  # CandleFetcher
│       │   └── instrument_fetcher.py
│       └── cli/
│           ├── __init__.py
│           ├── fetch_candles.py
│           ├── validate_data.py
│           └── align_data.py
├── tests/
│   ├── unit/
│   │   ├── test_signer.py
│   │   ├── test_rate_limiter.py
│   │   ├── test_bar_storage.py
│   │   ├── test_validator.py
│   │   └── test_aligner.py
│   ├── integration/
│   │   ├── test_okx_client.py    # 用 respx mock HTTP
│   │   └── test_candle_fetcher.py # 用 VCR 录制响应
│   └── smoke/
│       └── test_real_okx.py       # 真实调用，CI 跳过
├── data/                          # 数据产出（gitignore）
├── logs/                          # 日志（gitignore）
├── config.toml                    # 项目配置
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

### 3.2 依赖清单

**核心运行时依赖**（`[project]` dependencies）：

| 包 | 版本约束 | 用途 |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | HTTP 客户端 |
| `websockets` | `>=12,<14` | WebSocket 客户端 |
| `pandas` | `>=2.1,<3.0` | 数据处理 |
| `pyarrow` | `>=15,<20` | parquet 读写 |
| `pydantic` | `>=2.5,<3.0` | 边界校验 |
| `pydantic-settings` | `>=2.1,<3.0` | 配置加载 |
| `loguru` | `>=0.7,<1.0` | 日志 |
| `python-dateutil` | `>=2.9,<3.0` | 时区处理 |

**开发依赖**（`[project.optional-dependencies] dev`）：

| 包 | 用途 |
|---|---|
| `pytest` | 测试框架 |
| `pytest-asyncio` | 异步测试（M3 用，M1 预留） |
| `pytest-cov` | 覆盖率 |
| `respx` | httpx mock |
| `pytest-vcr` | HTTP 录制回放 |
| `ruff` | lint+format |

> Python 3.11+ 自带 `tomllib`，无需 `tomli`。

### 3.3 模块依赖矩阵

| 模块 | 依赖 ↓ |
|---|---|
| `cli/` | `fetcher/`, `storage/`, `foundation/` |
| `fetcher/` | `okx/`, `storage/`, `models/`, `foundation/` |
| `storage/` | `models/`, `foundation/` |
| `okx/` | `models/`, `foundation/` |
| `models/` | `foundation/` |
| `foundation/` | （无内部依赖） |

---

## 4. OKX 客户端设计

### 4.1 Signer（签名器）

**职责**：生成 HMAC-SHA256 签名。纯函数，无状态。

```python
# src/quant/okx/signer.py
import hmac, hashlib, base64

def sign(
    timestamp: str,
    method: str,
    request_path: str,
    body: str,
    secret: str,
) -> str:
    """
    生成 OKX V5 签名。

    Args:
        timestamp: ISO 8601 UTC 毫秒，如 "2024-01-15T10:30:00.123Z"
        method: HTTP 方法大写，如 "GET" / "POST"
        request_path: 含 query string，如 "/api/v5/market/candles?instId=BTC-USDT"
        body: POST body 字符串；GET 为 ""
        secret: API Secret

    Returns:
        Base64 编码的 HMAC-SHA256 签名
    """
    prehash = timestamp + method.upper() + request_path + body
    digest = hmac.new(
        secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")
```

**测试**：与 OKX 官方文档示例输入输出对齐（详见 §10.2）。

### 4.2 RateLimiter（限频器）

**职责**：按端点组维护令牌桶，阻塞 acquire。

```python
# src/quant/okx/rate_limiter.py
import time
from dataclasses import dataclass
from threading import Lock
from quant.foundation.errors import RateLimitTimeoutError

@dataclass
class BucketConfig:
    capacity: int       # 桶容量
    refill_per_sec: float  # 每秒补充速率

# 端点组配置（与 OKX_API_REFERENCE.md §4.2 一致）
GROUP_CONFIG = {
    "public":      BucketConfig(capacity=20, refill_per_sec=10.0),   # 20 req/2s per IP
    "account":     BucketConfig(capacity=10, refill_per_sec=5.0),   # 10 req/2s per UID
    "trade_order": BucketConfig(capacity=60, refill_per_sec=30.0),  # 60 req/2s per UID+instId
    "trade_batch": BucketConfig(capacity=300, refill_per_sec=150.0),
    "trade_cancel":BucketConfig(capacity=60, refill_per_sec=30.0),
    "algo":        BucketConfig(capacity=20, refill_per_sec=10.0),
    "history":     BucketConfig(capacity=10, refill_per_sec=5.0),
}

class RateLimiter:
    def __init__(self) -> None:
        self._buckets = {name: _Bucket(cfg) for name, cfg in GROUP_CONFIG.items()}

    def acquire(self, group: str, timeout: float = 10.0) -> None:
        """阻塞直到获取令牌；超时抛 RateLimitTimeoutError。"""
        if group not in self._buckets:
            raise ValueError(f"unknown group: {group}")
        self._buckets[group].acquire(timeout)

    def penalize(self, group: str, seconds: float = 2.0) -> None:
        """命中 50011 时强制阻塞该桶。"""
        self._buckets[group].penalize(seconds)
```

**`_Bucket` 实现**（令牌桶）：

```python
class _Bucket:
    def __init__(self, cfg: BucketConfig) -> None:
        self._cfg = cfg
        self._tokens = float(cfg.capacity)
        self._last = time.monotonic()
        self._lock = Lock()
        self._penalized_until = 0.0

    def acquire(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                now = time.monotonic()
                if now < self._penalized_until:
                    wait = self._penalized_until - now
                    if time.monotonic() + wait > deadline:
                        raise RateLimitTimeoutError(group=..., timeout=timeout)
                    time.sleep(wait)
                    continue
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                need = 1.0 - self._tokens
                wait = need / self._cfg.refill_per_sec
                if time.monotonic() + wait > deadline:
                    raise RateLimitTimeoutError(...)
                time.sleep(wait)

    def _refill(self, now: float) -> None:
        elapsed = now - self._last
        self._tokens = min(self._cfg.capacity, self._tokens + elapsed * self._cfg.refill_per_sec)
        self._last = now

    def penalize(self, seconds: float) -> None:
        with self._lock:
            self._penalized_until = max(self._penalized_until, time.monotonic() + seconds)
            self._tokens = 0.0  # 清空令牌
```

**端点→组映射**（在 OKXClient 中维护）：

```python
PATH_TO_GROUP = [
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
```

### 4.3 OKXClient（REST 客户端）

```python
# src/quant/okx/client.py
from typing import Any, Optional
from decimal import Decimal
import httpx
from quant.foundation.config import OKXSettings
from quant.foundation.logger import get_logger
from quant.foundation.errors import (
    AuthError, InsufficientBalanceError, RateLimitError,
    ServerError, NetworkError,
)
from .signer import sign
from .rate_limiter import RateLimiter

class OKXClient:
    BASE_URL = "https://www.okx.com"

    def __init__(
        self,
        settings: OKXSettings,
        rate_limiter: Optional[RateLimiter] = None,
        demo: Optional[bool] = None,
    ) -> None:
        self._s = settings
        self._demo = demo if demo is not None else settings.demo
        self._rl = rate_limiter or RateLimiter()
        self._log = get_logger("okx.client")
        self._client = httpx.Client(timeout=30.0)
        self._server_time_offset_ms = 0
        self._sync_time()

    # ---- 公开方法 ----

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        """发送 GET 请求。"""
        request_path = path
        if params:
            request_path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        return self._request("GET", path, request_path, params=params)

    def post(self, path: str, body: dict) -> dict:
        """发送 POST 请求。"""
        import json
        body_str = json.dumps(body, separators=(",", ":"))
        return self._request("POST", path, path, body=body_str)

    def close(self) -> None:
        self._client.close()

    # ---- 内部 ----

    def _request(
        self,
        method: str,
        path: str,
        request_path_for_sign: str,
        params: Optional[dict] = None,
        body: str = "",
    ) -> dict:
        group = self._group_of(path)
        url = self.BASE_URL + path

        last_exc = None
        for attempt in range(3):
            self._rl.acquire(group)
            ts = self._timestamp()
            headers = self._headers(method, request_path_for_sign, ts, body)

            try:
                if method == "GET":
                    resp = self._client.get(url, params=params, headers=headers)
                else:
                    resp = self._client.post(url, content=body, headers=headers)
                payload = resp.json()
            except httpx.NetworkError as e:
                last_exc = NetworkError(str(e))
                self._log.warning(f"network error attempt {attempt+1}: {e}")
                continue
            except httpx.TimeoutException as e:
                last_exc = NetworkError(f"timeout: {e}")
                continue

            code = payload.get("code", "")
            if code == "0":
                return payload["data"]

            if code == "50011":
                self._log.warning(f"rate limited, sleeping 2s")
                self._rl.penalize(group, 2.0)
                last_exc = RateLimitError(payload.get("msg", ""))
                continue

            if code in ("50101", "50111", "50113", "50105"):
                raise AuthError(code, payload.get("msg", ""))
            if code == "51008":
                raise InsufficientBalanceError(payload.get("msg", ""))
            if code.startswith("5"):
                if attempt < 2:
                    self._log.warning(f"server error {code}, retrying")
                    last_exc = ServerError(code, payload.get("msg", ""))
                    continue
                raise ServerError(code, payload.get("msg", ""))

            raise ValueError(f"unknown response: code={code} msg={payload.get('msg')}")

        raise last_exc or RuntimeError("exhausted retries")

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
        from datetime import datetime, timezone
        # 校正服务端时差
        now = datetime.now(timezone.utc)
        adjusted = now.timestamp() * 1000 + self._server_time_offset_ms
        dt = datetime.fromtimestamp(adjusted / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}Z"

    def _sync_time(self) -> None:
        """启动时同步服务端时间，每5分钟由调用方触发一次。"""
        try:
            resp = self._client.get(f"{self.BASE_URL}/api/v5/public/time")
            server_ts = int(resp.json()["data"][0]["ts"])
            local_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
            self._server_time_offset_ms = server_ts - local_ts
            if abs(self._server_time_offset_ms) > 15000:
                self._log.warning(f"large time offset: {self._server_time_offset_ms}ms")
        except Exception as e:
            self._log.error(f"time sync failed: {e}")
            raise

    @staticmethod
    def _group_of(path: str) -> str:
        import re
        for pattern, group in PATH_TO_GROUP:
            if re.match(pattern, path):
                return group
        return "public"

    def sync_time_periodically(self) -> None:
        """由外部定时调用，每5分钟校时一次。"""
        self._sync_time()
```

### 4.4 WSClient（WebSocket 客户端）

```python
# src/quant/okx/ws_client.py
import asyncio
import json
import websockets
from typing import Callable, Awaitable
from quant.foundation.logger import get_logger

class WSClient:
    PUBLIC_URL = "wss://ws.okx.com:8443/ws/v5/public"
    PRIVATE_URL = "wss://ws.okx.com:8443/ws/v5/private"

    def __init__(self, url: str = PUBLIC_URL) -> None:
        self._url = url
        self._log = get_logger("okx.ws")
        self._ws = None
        self._subscriptions: list[dict] = []
        self._callbacks: dict[str, Callable] = {}
        self._running = False

    async def connect(self) -> None:
        """建立连接；断线自动重连（5秒退避）。"""
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self._url, ping_interval=30) as ws:
                    self._ws = ws
                    # 恢复订阅
                    for sub in self._subscriptions:
                        await ws.send(json.dumps(sub))
                    self._log.info("ws connected")
                    await self._receive_loop(ws)
            except Exception as e:
                self._log.warning(f"ws disconnected: {e}, retry in 5s")
                await asyncio.sleep(5)

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            for item in msg.get("data", []):
                channel = msg.get("arg", {}).get("channel", "")
                cb = self._callbacks.get(channel)
                if cb:
                    await cb(item)

    async def subscribe(
        self,
        channel: str,
        inst_id: str,
        callback: Callable[[dict], Awaitable[None]],
    ) -> None:
        sub = {"op": "subscribe", "args": [{"channel": channel, "instId": inst_id}]}
        self._subscriptions.append(sub)
        self._callbacks[channel] = callback
        if self._ws:
            await self._ws.send(json.dumps(sub))

    async def close(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
```

> M1 阶段 WSClient 提供 API 但不强制使用——M3 模拟盘才真正依赖。M1 只需跑通"订阅 → 收到一条 tick"的烟测。

### 4.5 时序图：拉取历史K线

```
Caller          CandleFetcher    OKXClient     RateLimiter   OKX Server
  │                  │               │              │            │
  │ fetch(start,end) │               │              │            │
  ├─────────────────►│               │              │            │
  │                  │ 计算分页      │              │            │
  │                  ├──────────────►│ acquire(pub) │            │
  │                  │               ├─────────────►│            │
  │                  │               │              │            │
  │                  │               │ GET /market/history-candles?... │
  │                  │               ├─────────────────────────────►│
  │                  │               │              │            │
  │                  │               │◄─────────────────────────────┤
  │                  │               │ data: [[ts, o,h,l,c,...], ...]│
  │                  │               │              │            │
  │                  │               │ 校验 code=0   │            │
  │                  │◄──────────────┤              │            │
  │                  │ 转 Bar        │                            │
  │                  │ 存 parquet    │                            │
  │                  │ 翻页(若需)    │                            │
  │                  └──循环─────────┤                            │
  │                  │               │              │            │
  │◄─────────────────┤ done                                       │
```

---

## 5. 数据模型

### 5.1 设计原则

- 全部 `@dataclass(frozen=True, slots=True)`：不可变 + 内存效率
- 价格/数量用 `Decimal`：避免 `0.1+0.2 != 0.3` 误差
- 时间戳用 `int`（UTC ms）：与 OKX 一致，避免 datetime 时区坑
- 边界转换用 pydantic（IO 层），内部只用 dataclass

### 5.2 Bar（K线）

```python
# src/quant/models/bar.py
from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True, slots=True)
class Bar:
    ts: int           # 开盘时间戳 UTC ms
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vol: Decimal          # 基础币成交量
    vol_quote: Decimal    # 计价币成交额（volCcyQuote）
    confirm: int          # 0=未结束 1=已结束
    inst_id: str          # 如 "BTC-USDT"
    bar_size: str         # 如 "1H", "1D"

    def is_closed(self) -> bool:
        return self.confirm == 1
```

### 5.3 Tick（实时盘口）

```python
# src/quant/models/tick.py
@dataclass(frozen=True, slots=True)
class Tick:
    ts: int
    inst_id: str
    bid_px: Decimal
    bid_sz: Decimal
    ask_px: Decimal
    ask_sz: Decimal
    last_px: Decimal
    last_sz: Decimal
```

### 5.4 Instrument（产品配置）

```python
# src/quant/models/instrument.py
@dataclass(frozen=True, slots=True)
class Instrument:
    inst_id: str          # "BTC-USDT"
    inst_type: str        # SPOT/SWAP/FUTURES/OPTION
    base_ccy: str         # "BTC"
    quote_ccy: str        # "USDT"
    settle_ccy: str       # 交割币；SPOT 为空
    tick_sz: Decimal       # 价格精度
    lot_sz: Decimal       # 数量精度
    min_sz: Decimal       # 最小下单量
    ct_val: Decimal       # 合约面值；SPOT 为 1
    state: str            # live/sanceled/expired
    list_time: int        # 上线时间 UTC ms
```

### 5.5 FundingRate（资金费率）

```python
# src/quant/models/funding_rate.py
@dataclass(frozen=True, slots=True)
class FundingRate:
    inst_id: str          # "BTC-USDT-SWAP"
    ts: int               # 结算时间 UTC ms
    rate: Decimal          # 资金费率
    next_funding_ts: int   # 下次结算时间
```

### 5.6 pydantic 转换 schema

```python
# src/quant/okx/schemas.py
from pydantic import BaseModel, Field
from decimal import Decimal

class RawCandle(BaseModel):
    """OKX 返回的单根K线字段顺序：[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]"""
    raw: list[str]

    def to_bar(self, inst_id: str, bar_size: str) -> "Bar":
        from quant.models.bar import Bar
        return Bar(
            ts=int(self.raw[0]),
            open=Decimal(self.raw[1]),
            high=Decimal(self.raw[2]),
            low=Decimal(self.raw[3]),
            close=Decimal(self.raw[4]),
            vol=Decimal(self.raw[5]),
            vol_quote=Decimal(self.raw[7]),
            confirm=int(self.raw[8]),
            inst_id=inst_id,
            bar_size=bar_size,
        )
```

---

## 6. 存储设计

### 6.1 目录结构

```
data/
├── candles/
│   ├── spot/
│   │   ├── BTC-USDC/
│   │   │   └── 1H/
│   │   │       ├── 2023.parquet
│   │   │       └── 2024.parquet
│   │   └── ETH-USDC/
│   │       └── 1H/
│   │           └── 2024.parquet
│   └── swap/
│       └── BTC-USDT-SWAP/
│           └── 1H/
│               └── 2024.parquet
├── funding_rate/
│   └── BTC-USDT-SWAP/
│       └── 2024.parquet
└── instruments/
    ├── spot.json
    ├── swap.json
    └── futures.json
```

### 6.2 文件命名规则

- K线：`data/candles/{inst_type}/{inst_id}/{bar_size}/{year}.parquet`
  - `inst_type`: `spot` / `swap` / `futures` / `option`
  - `year`: UTC 年份（4位）
- 产品配置：`data/instruments/{inst_type}.json`
- 资金费率：`data/funding_rate/{inst_id}/{year}.parquet`

### 6.3 parquet schema

| 列名 | parquet 类型 | Python 类型 | 说明 |
|---|---|---|---|
| `ts` | int64 | int | 开盘时间 UTC ms |
| `open` | string | str | Decimal 字符串 |
| `high` | string | str | |
| `low` | string | str | |
| `close` | string | str | |
| `vol` | string | str | |
| `vol_quote` | string | str | |
| `confirm` | int8 | int | 0/1 |
| `inst_id` | string | str | |
| `bar_size` | string | str | |

> **为什么 Decimal 存为 string**：parquet 原生 `DECIMAL` 类型在 Python 端处理繁琐，且不同精度需声明；存为 string 简单可靠，读出时用 `Decimal(s)` 即可还原精度。

### 6.4 写入策略

```python
# src/quant/storage/bar_storage.py
from pathlib import Path
from decimal import Decimal
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from quant.models.bar import Bar

class BarStorage:
    def __init__(self, data_dir: Path) -> None:
        self._root = data_dir

    def save_bars(self, bars: list[Bar]) -> None:
        """批量保存K线；同时间戳去重（保留最新）；按 year 分文件。"""
        if not bars:
            return
        # 按 year 分组
        by_year: dict[int, list[Bar]] = {}
        for b in bars:
            year = self._year_of(b.ts)
            by_year.setdefault(year, []).append(b)

        for year, group in by_year.items():
            path = self._path_for(group[0].inst_id, group[0].bar_size, year)
            path.parent.mkdir(parents=True, exist_ok=True)
            df_new = self._to_df(group)
            if path.exists():
                df_old = pd.read_parquet(path)
                df = pd.concat([df_old, df_new])
                df = df.drop_duplicates(subset=["ts"], keep="last")
                df = df.sort_values("ts").reset_index(drop=True)
            else:
                df = df_new.sort_values("ts").reset_index(drop=True)
            df.to_parquet(path, index=False, engine="pyarrow")

    def load_bars(
        self,
        inst_id: str,
        bar_size: str,
        start_ts: int,
        end_ts: int,
    ) -> list[Bar]:
        """按时间区间读取K线。"""
        years = range(self._year_of(start_ts), self._year_of(end_ts) + 1)
        dfs = []
        for y in years:
            path = self._path_for(inst_id, bar_size, y)
            if path.exists():
                df = pd.read_parquet(path)
                df = df[(df["ts"] >= start_ts) & (df["ts"] <= end_ts)]
                dfs.append(df)
        if not dfs:
            return []
        df = pd.concat(dfs).sort_values("ts").reset_index(drop=True)
        return [self._row_to_bar(row) for _, row in df.iterrows()]

    def _path_for(self, inst_id: str, bar_size: str, year: int) -> Path:
        inst_type = self._infer_type(inst_id)
        return self._root / "candles" / inst_type / inst_id / bar_size / f"{year}.parquet"

    @staticmethod
    def _year_of(ts_ms: int) -> int:
        import datetime as dt
        return dt.datetime.utcfromtimestamp(ts_ms / 1000).year

    @staticmethod
    def _infer_type(inst_id: str) -> str:
        if inst_id.endswith("-SWAP"):
            return "swap"
        if "-USDT-" in inst_id or "-USD-" in inst_id:
            return "futures"
        return "spot"
```

### 6.5 DataValidator（数据校验器）

```python
# src/quant/storage/validator.py
from quant.models.bar import Bar

class DataValidator:
    def validate(self, bars: list[Bar]) -> "ValidationReport":
        """校验K线连续性、重复、OHLC合法性。"""
        report = ValidationReport()
        if not bars:
            report.add_error("empty bars")
            return report

        bars = sorted(bars, key=lambda b: b.ts)
        # 1. 重复时间戳
        seen = set()
        for b in bars:
            if b.ts in seen:
                report.add_duplicate(b.ts)
            seen.add(b.ts)

        # 2. 连续性（仅校验已确认K线）
        closed = [b for b in bars if b.is_closed()]
        bar_size_ms = self._bar_size_ms(bars[0].bar_size)
        for i in range(1, len(closed)):
            gap = closed[i].ts - closed[i-1].ts
            if gap != bar_size_ms:
                report.add_missing(closed[i-1].ts, closed[i].ts, gap, bar_size_ms)

        # 3. OHLC合法性
        for b in bars:
            if not (b.low <= b.high):
                report.add_invalid_ohlc(b.ts, "low>high")
            if not (b.low <= b.open <= b.high and b.low <= b.close <= b.high):
                report.add_invalid_ohlc(b.ts, "o/c not in [low,high]")

        return report

    @staticmethod
    def _bar_size_ms(bar_size: str) -> int:
        # 简化版：支持 1m/5m/15m/30m/1H/4H/1D/1W
        unit = bar_size[-1]
        n = int(bar_size[:-1])
        if unit == "m": return n * 60_000
        if unit == "H": return n * 3_600_000
        if unit == "D": return n * 86_400_000
        if unit == "W": return n * 604_800_000
        raise ValueError(f"unknown bar_size: {bar_size}")
```

### 6.6 DataAligner（多品种对齐）

```python
# src/quant/storage/aligner.py
import pandas as pd
from quant.models.bar import Bar

class DataAligner:
    def align(
        self,
        bars_by_inst: dict[str, list[Bar]],
        fill: str = "ffill",   # ffill / nan / interpolate
    ) -> pd.DataFrame:
        """
        多品种K线按统一时间轴对齐。

        Args:
            bars_by_inst: {inst_id: [Bar, ...]}
            fill: 缺失行填充策略
                ffill: 用前值填充（适合价格）
                nan: 留空
                interpolate: 线性插值（适合连续变量）

        Returns:
            DataFrame，index=ts，列为多级 (inst_id, field)
        """
        if not bars_by_inst:
            return pd.DataFrame()

        # 各品种转 DataFrame
        dfs = {}
        for inst_id, bars in bars_by_inst.items():
            df = pd.DataFrame([{
                "ts": b.ts, "open": b.open, "high": b.high, "low": b.low,
                "close": b.close, "vol": b.vol,
            } for b in bars]).set_index("ts").sort_index()
            dfs[inst_id] = df.add_prefix(f"{inst_id}_")

        # 外连接得到统一时间轴
        merged = pd.concat(dfs.values(), axis=1)

        if fill == "ffill":
            merged = merged.ffill()
        elif fill == "interpolate":
            merged = merged.interpolate(method="time")
        # nan 时不填充

        return merged
```

### 6.7 业务编排层

#### CandleFetcher（K线下载编排）

> `Checkpoint` 是简单 dataclass：`{last_ts: int}`，JSON 序列化到 `.checkpoints/{inst_id}_{bar_size}.json`。`bar_size_ms` 由 §6.5 `DataValidator._bar_size_ms` 提供。`Optional` 用 `typing.Optional`。

```python
# src/quant/fetcher/candle_fetcher.py
from pathlib import Path
from quant.okx.client import OKXClient
from quant.models.bar import Bar
from quant.storage.bar_storage import BarStorage
from quant.okx.schemas import RawCandle

class CandleFetcher:
    """
    从 OKX 拉取历史K线并保存到本地。

    核心能力：
    - 断点续传：记录已下载时间戳，中断后从断点续拉
    - 自动翻页：用 history-candles 的 before/after 翻页
    - 缺失补拉：下载完成后调用 DataValidator，发现缺失段自动补拉（最多3次）
    """
    PAGE_SIZE = 100  # OKX 单次返回上限

    def __init__(self, client: OKXClient, data_dir: Path) -> None:
        self._client = client
        self._storage = BarStorage(data_dir)
        self._ckpt_dir = data_dir / ".checkpoints"
        self._ckpt_dir.mkdir(parents=True, exist_ok=True)

    def fetch(
        self,
        inst_id: str,
        bar_size: str,
        start_ts: int,
        end_ts: int,
    ) -> int:
        """返回下载的K线数量。"""
        # 1. 读断点
        ckpt = self._load_checkpoint(inst_id, bar_size)
        cursor = max(start_ts, ckpt.last_ts + bar_size_ms) if ckpt else start_ts

        # 2. 翻页拉取
        total = 0
        while cursor < end_ts:
            batch = self._fetch_page(inst_id, bar_size, cursor, end_ts)
            if not batch:
                break
            self._storage.save_bars(batch)
            cursor = batch[-1].ts + bar_size_ms
            self._save_checkpoint(inst_id, bar_size, cursor)
            total += len(batch)

        # 3. 校验+补拉
        bars = self._storage.load_bars(inst_id, bar_size, start_ts, end_ts)
        validator = DataValidator()
        report = validator.validate(bars)
        if report.has_missing:
            self._fill_missing(inst_id, bar_size, report.missing_ranges)

        return total

    def _fetch_page(self, inst_id, bar_size, before, end_ts) -> list[Bar]:
        """单次API调用，最多100根。"""
        data = self._client.get(
            "/api/v5/market/history-candles",
            {"instId": inst_id, "bar": bar_size, "limit": str(self.PAGE_SIZE),
             "before": str(before), "after": str(end_ts)},
        )
        # OKX 返回按 ts 倒序，转正序
        data = sorted(data, key=lambda x: int(x[0]))
        return [RawCandle(raw=item).to_bar(inst_id, bar_size) for item in data]

    def _load_checkpoint(self, inst_id, bar_size) -> Optional[Checkpoint]:
        path = self._ckpt_dir / f"{inst_id}_{bar_size}.json"
        if not path.exists():
            return None
        return Checkpoint.parse_raw(path.read_text())

    def _save_checkpoint(self, inst_id, bar_size, last_ts) -> None:
        path = self._ckpt_dir / f"{inst_id}_{bar_size}.json"
        path.write_text(Checkpoint(last_ts=last_ts).json())

    def _fill_missing(self, inst_id, bar_size, ranges) -> None:
        """对每个缺失段单独拉取。"""
        for start, end in ranges:
            bars = self._fetch_page(inst_id, bar_size, start, end)
            self._storage.save_bars(bars)
```

#### InstrumentFetcher（产品配置下载）

```python
# src/quant/fetcher/instrument_fetcher.py
import json
from pathlib import Path
from quant.okx.client import OKXClient
from quant.models.instrument import Instrument

class InstrumentFetcher:
    """
    拉取所有产品配置并保存为 JSON。

    注意：模拟盘与实盘的 instIdCode 不同，需分别拉取。
    """
    INST_TYPES = ["SPOT", "SWAP", "FUTURES", "OPTION"]

    def __init__(self, client: OKXClient, data_dir: Path) -> None:
        self._client = client
        self._dir = data_dir / "instruments"
        self._dir.mkdir(parents=True, exist_ok=True)

    def fetch_all(self) -> dict[str, list[Instrument]]:
        out = {}
        for inst_type in self.INST_TYPES:
            data = self._client.get(
                "/api/v5/public/instruments",
                {"instType": inst_type},
            )
            instruments = [self._parse(item) for item in data]
            out[inst_type] = instruments
            self._save_json(inst_type, instruments)
        return out

    @staticmethod
    def _parse(raw: dict) -> Instrument:
        from decimal import Decimal
        return Instrument(
            inst_id=raw["instId"],
            inst_type=raw["instType"],
            base_ccy=raw.get("baseCcy", ""),
            quote_ccy=raw.get("quoteCcy", ""),
            settle_ccy=raw.get("settleCcy", ""),
            tick_sz=Decimal(raw["tickSz"]),
            lot_sz=Decimal(raw["lotSz"]),
            min_sz=Decimal(raw.get("minSz", "0")),
            ct_val=Decimal(raw.get("ctVal", "1")),
            state=raw.get("state", ""),
            list_time=int(raw.get("listTime", "0")),
        )

    def _save_json(self, inst_type: str, items: list[Instrument]) -> None:
        path = self._dir / f"{inst_type.lower()}.json"
        payload = [
            {k: str(v) if isinstance(v, Decimal) else v
             for k, v in i.__dict__.items()}
            for i in items
        ]
        path.write_text(json.dumps(payload, indent=2))
```

#### FundingRateFetcher（资金费率下载）

```python
# src/quant/fetcher/funding_rate_fetcher.py
class FundingRateFetcher:
    """
    拉取永续合约资金费率历史。

    端点：GET /api/v5/public/funding-rate?instId=...&before=...&after=...&limit=100
    限频组：public（20 req/2s per IP）
    """
    # 实现模式同 CandleFetcher：断点续传 + 翻页 + 校验
    ...
```

> `FundingRateFetcher` 的实现模式与 `CandleFetcher` 一致：断点续传+翻页+校验。
> 为避免重复，spec 不再展开伪代码，实施时复用 CandleFetcher 的结构即可。

---

## 7. 配置设计

### 7.1 config.toml（项目根目录）

```toml
[okx]
api_key = "${OKX_API_KEY}"        # env 引用语法
secret = "${OKX_SECRET}"
passphrase = "${OKX_PASSPHRASE}"
demo = true                         # true=模拟盘，false=实盘

[storage]
data_dir = "./data"
retention_days = 365

[rate_limit]
timeout_seconds = 10.0

[logging]
level = "INFO"                      # DEBUG/INFO/WARNING/ERROR
file = "./logs/quant-trading.log"
rotation = "10 MB"
retention = "30 days"

# 以下段落 M1 不实现，M2 起由下游模块扩展；此处仅预留形状
[redis]
url = "${REDIS_URL:-redis://localhost:6379/0}"
queue_key = "quant:signals"          # LIST 主队列
inflight_key = "quant:signals:inflight"  # 处理中暂存
dead_key = "quant:signals:dead"      # 死信队列
dedup_prefix = "quant:dedup:"        # SET 键前缀 + hash(signal_json)
dedup_ttl_seconds = 300              # 5 分钟

[webhook]
host = "127.0.0.1"
port = 8000
path = "/webhook/tv"
auth_token = "${WEBHOOK_AUTH_TOKEN}"  # TV alert header 校验
ack_timeout_seconds = 5
```

### 7.2 .env.example

```bash
# 复制为 .env 并填入真实值
OKX_API_KEY=your-api-key-here
OKX_SECRET=your-secret-here
OKX_PASSPHRASE=your-passphrase-here

# M2 起使用（M1 不需要）
REDIS_URL=redis://localhost:6379/0
WEBHOOK_AUTH_TOKEN=generate-a-random-secret
```

### 7.3 Settings 类

```python
# src/quant/foundation/config.py
from pathlib import Path
import os
import tomllib
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

class OKXSettings(BaseModel):
    api_key: str
    secret: str
    passphrase: str
    demo: bool = True

class StorageSettings(BaseModel):
    data_dir: Path = Path("./data")
    retention_days: int = 365

class RateLimitSettings(BaseModel):
    timeout_seconds: float = 10.0

class LoggingSettings(BaseModel):
    level: str = "INFO"
    file: Path = Path("./logs/quant-trading.log")
    rotation: str = "10 MB"
    retention: str = "30 days"

class Settings(BaseSettings):
    okx: OKXSettings
    storage: StorageSettings = StorageSettings()
    rate_limit: RateLimitSettings = RateLimitSettings()
    logging: LoggingSettings = LoggingSettings()

    @classmethod
    def load(cls, config_path: Path = Path("config.toml")) -> "Settings":
        # 1. 读 TOML
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        # 2. 展开 ${ENV} 引用
        data = cls._expand_env(data)
        return cls(**data)

    @staticmethod
    def _expand_env(d: dict) -> dict:
        out = {}
        for k, v in d.items():
            if isinstance(v, dict):
                out[k] = Settings._expand_env(v)
            elif isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                env_name = v[2:-1]
                val = os.environ.get(env_name)
                if val is None:
                    raise ValueError(f"env var {env_name} not set")
                out[k] = val
            else:
                out[k] = v
        return out
```

### 7.4 日志脱敏

`Logger` 自动过滤 `secret`/`passphrase`/`api_key` 字段：

```python
# src/quant/foundation/logger.py
from loguru import logger
import re

SENSITIVE_KEYS = {"secret", "passphrase", "api_key", "apisecret"}
SENSITIVE_PATTERN = re.compile(
    r'("(?:' + "|".join(SENSITIVE_KEYS) + r')":\s*")([^"]+)(")',
    re.IGNORECASE,
)

def setup_logger(level: str, file, rotation, retention) -> None:
    logger.remove()
    logger.add(
        lambda msg: print(msg, end=""),
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS}|{level:<5}|{module}|{message}",
        filter=_redact_sensitive,
    )
    logger.add(
        file,
        level=level,
        rotation=rotation,
        retention=retention,
        format="{time:YYYY-MM-DDTHH:mm:ss.SSSZ}|{level:<5}|{module}|{message}",
        filter=_redact_sensitive,
    )

def _redact_sensitive(record):
    msg = record["message"]
    record["message"] = SENSITIVE_PATTERN.sub(
        lambda m: f'{m.group(1)}***REDACTED***{m.group(3)}',
        msg,
    )
    return record

def get_logger(name: str):
    return logger.bind(name=name)
```

---

## 8. 日志与可观测性

### 8.1 日志格式

`{UTC time}|{level}|{module}|{message}`

例：
```
2026-10-17T10:30:00.123Z|INFO |okx.client|GET /api/v5/market/history-candles 200, 100 bars in 320ms
2026-10-17T10:30:02.456Z|WARN |okx.client|rate limited (50011), sleeping 2s
2026-10-17T10:30:04.458Z|ERROR|okx.client|auth failed (50111)
```

### 8.2 关键事件

| 事件 | 级别 | 触发 |
|---|---|---|
| 启动完成 | INFO | OKXClient 初始化、校时完成 |
| 请求成功 | DEBUG | 每次HTTP请求+耗时+数据量 |
| 限频命中 | WARNING | 50011 |
| 重试 | WARNING | 网络异常/5xx |
| 校时失败 | ERROR | _sync_time 异常 |
| 鉴权失败 | ERROR | 50101/50111/50113 |
| 余额不足 | ERROR | 51008 |
| 数据校验失败 | WARNING | 缺失/重复/OHLC非法 |
| 文件写入完成 | INFO | parquet 落盘 |

### 8.3 Secret 脱敏

所有 `api_key`/`secret`/`passphrase` 在日志中自动替换为 `***REDACTED***`，包括请求/响应body。

---

## 9. 错误处理与重试策略

### 9.1 异常体系

```python
# src/quant/foundation/errors.py

class QuantError(Exception):
    """所有项目异常的基类。"""

class NetworkError(QuantError):
    """网络异常（连接超时/DNS失败）。"""

class RateLimitError(QuantError):
    """OKX 返回 50011。"""
    def __init__(self, msg: str):
        super().__init__(msg)

class RateLimitTimeoutError(RateLimitError):
    """本地限频器 acquire 超时。"""
    def __init__(self, group: str, timeout: float):
        super().__init__(f"acquire {group} timeout after {timeout}s")

class AuthError(QuantError):
    """鉴权失败：50101/50111/50113/50105。"""
    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code

class ServerError(QuantError):
    """OKX 服务端 5xx。"""
    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code

class InsufficientBalanceError(QuantError):
    """51008：余额不足。"""

class ValidationError(QuantError):
    """数据校验失败（缺失/重复/OHLC非法）。"""
```

### 9.2 重试策略

| 错误 | 重试 | 退避 |
|---|---|---|
| NetworkError | 3次 | 1s, 2s, 4s 指数 |
| ServerError（5xx） | 3次 | 1s, 2s, 4s |
| RateLimitError | 3次 | 2s 固定（OKX 要求） |
| AuthError | 0次 | 立即抛出 |
| InsufficientBalanceError | 0次 | 立即抛出 |
| 其他错误码 | 0次 | 立即抛出 ValueError |

### 9.3 时间同步容错

- 启动时同步服务端时间，记录 offset_ms
- 校时失败 → 拒绝启动
- |offset_ms| > 15000 → WARNING，自动校时
- |offset_ms| > 30000 → 拒绝请求（OKX 必返 50114）
- 每 5 分钟由外部定时器触发 `sync_time_periodically()`

---

## 10. 测试策略

### 10.1 测试金字塔

| 层 | 占比 | 工具 |
|---|---|---|
| 单元测试 | 70% | pytest |
| 集成测试 | 25% | pytest + respx + VCR |
| 烟测 | 5% | pytest（标记 `@pytest.mark.smoke`，CI 跳过） |

### 10.2 单元测试用例

**`test_signer.py`**（与官方示例对齐）：

```python
def test_sign_official_example():
    """OKX 官方文档示例。

    实施时从 https://www.okx.com/docs-v5/en/#overview-rest-authentication
    提取官方公布的 timestamp/method/path/body/secret 与期望签名值，
    作为固定测试向量。签名是确定性函数，示例一次即可永久验证实现正确性。
    """
    ts = "2024-01-01T10:30:00.000Z"
    method = "GET"
    path = "/api/v5/account/balance"
    body = ""
    secret = "<填入官方示例 secret>"
    expected = "<填入官方期望签名>"
    assert sign(ts, method, path, body, secret) == expected

def test_sign_post_with_body():
    ...

def test_sign_path_with_query():
    ...
```

**`test_rate_limiter.py`**：

```python
def test_bucket_refills_at_rate():
    # 容量=20，速率=10/s：1秒可发10次
    ...

def test_penalize_blocks_for_n_seconds():
    ...

def test_acquire_timeout():
    ...
```

**`test_bar_storage.py`**：

```python
def test_save_and_load_roundtrip():
    bars = [Bar(ts=..., ...)]
    storage.save_bars(bars)
    loaded = storage.load_bars("BTC-USDT", "1H", ..., ...)
    assert loaded == bars

def test_dedup_keeps_latest():
    # 同 ts 不同 close，保存后读出最新
    ...

def test_year_split():
    # 跨年数据写入2个文件
    ...
```

**`test_validator.py`**：

```python
def test_detect_missing_bar():
    # 删一根，期望报告 missing
    ...

def test_detect_duplicate():
    ...

def test_detect_invalid_ohlc():
    ...
```

**`test_aligner.py`**：

```python
def test_align_two_instruments_same_range():
    ...

def test_align_with_different_lengths():
    # BTC有8784根，ETH只有8760根
    ...

def test_fill_strategies():
    # ffill/nan/interpolate
    ...
```

### 10.3 集成测试用例

**`test_okx_client.py`**（用 respx mock）：

```python
import respx
import httpx

@respx.mock
def test_get_returns_data_on_success():
    respx.get("/api/v5/market/ticker").respond(
        json={"code": "0", "msg": "", "data": [{"instId": "BTC-USDT"}]}
    )
    client = OKXClient(test_settings, rate_limiter=FakeLimiter())
    data = client.get("/api/v5/market/ticker", {"instId": "BTC-USDT"})
    assert data == [{"instId": "BTC-USDT"}]

@respx.mock
def test_retries_on_50011():
    # 第一次50011，第二次0
    ...

@respx.mock
def test_raises_auth_error_on_50111():
    ...

@respx.mock
def test_time_sync_on_init():
    ...
```

**`test_candle_fetcher.py`**（用 VCR 录制真实响应）：

```python
def test_fetch_one_year_1h_bars(cassette):
    # VCR 录制一次真实响应，回放校验
    ...

def test_resume_from_checkpoint():
    # 模拟中断后续传
    ...
```

### 10.4 烟测用例

**`test_real_okx.py`**：

```python
import pytest

@pytest.mark.smoke
def test_fetch_one_candle_real():
    """真实拉取BTC-USDT 1根K线。CI 跳过，本地手动跑。"""
    client = OKXClient(settings_from_env())
    data = client.get("/api/v5/market/candles", {"instId": "BTC-USDT", "bar": "1H", "limit": "1"})
    assert len(data) == 1
    assert data[0][0].isdigit()
```

### 10.5 覆盖率目标

- `foundation/`: 90%+
- `okx/signer.py`: 100%
- `okx/rate_limiter.py`: 90%+
- `okx/client.py`: 80%+（部分错误分支难触发）
- `storage/`: 90%+
- `fetcher/`: 80%+

---

## 11. CLI 入口

### 11.1 `fetch_candles.py`

```python
# src/quant/cli/fetch_candles.py
"""拉取历史K线并保存到 parquet。

用法:
  python -m quant.cli.fetch_candles --inst BTC-USDT --bar 1H --start 2024-01-01 --end 2024-12-31
"""
import argparse
from datetime import datetime, timezone
from quant.foundation.config import Settings
from quant.foundation.logger import setup_logger
from quant.okx.client import OKXClient
from quant.fetcher.candle_fetcher import CandleFetcher

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--inst", required=True, help="instId, 如 BTC-USDT")
    p.add_argument("--bar", default="1H", help="K线周期，如 1H/1D")
    p.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    p.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    p.add_argument("--config", default="config.toml", help="配置文件路径")
    args = p.parse_args()

    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())

    client = OKXClient(settings.okx)
    fetcher = CandleFetcher(client, settings.storage.data_dir)
    fetcher.fetch(
        inst_id=args.inst,
        bar_size=args.bar,
        start_ts=int(datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000),
        end_ts=int(datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000),
    )
    client.close()

if __name__ == "__main__":
    main()
```

### 11.2 `validate_data.py`、`align_data.py`

`validate_data.py` 和 `align_data.py` 的 CLI 结构与 `fetch_candles.py` 一致：解析参数 → 加载配置 → 调用 `DataValidator.validate()` 或 `DataAligner.align()` → 打印报告/输出对齐后的 parquet。伪代码不重复展开，实施时按 `fetch_candles.py` 模板补全即可。

---

## 12. M2-M6 接口契约

> 仅定义接口签名与数据契约，**不写实现**。各模块启动 spec 时再细化。
>
> 架构背景：采用 TradingView Pine 回测 + webhook 下单（方案B：FastAPI + Redis + worker）。M1 不直接涉及 webhook/worker，但 M2 起将依赖 M1 产出的 `OKXClient`、`RateLimiter`、`Bar`、`Instrument`、`Settings`。本节为下游模块预留接口形状。

### 12.1 M2 回测引擎（Python 副本，与 Pine 双回测）

```python
# M2 待实现
class Strategy(ABC):
    """策略基类。Python 副本必须与 Pine 版本逻辑严格对齐；双回测结果偏离 > 5% 时禁止上实盘。"""
    @abstractmethod
    def on_bar(self, bar: Bar) -> Optional["Signal"]: ...
    @abstractmethod
    def on_init(self, ctx: "Context") -> None: ...

@dataclass(frozen=True)
class Signal:
    """与 webhook alert JSON 字段一一对应（便于真信号→回测 replay）。"""
    strategy_id: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    inst_id: str
    price: Optional[Decimal]  # market 单为 None
    qty: Decimal
    ts: datetime  # 信号生成时间（UTC）

class Backtest:
    def run(
        self,
        strategy: Strategy,
        bars: list[Bar],
        config: "BacktestConfig",
    ) -> "BacktestResult": ...
```

### 12.2 M2-M3 Webhook Server + Worker（方案B核心）

```python
# M2 末启动，M3 用本地脚本模拟 alert 跑通

# webhook/server.py
class WebhookServer:
    """FastAPI 应用工厂。端点 POST /webhook/tv 接收 TV alert。"""
    def create_app(self) -> "FastAPI": ...

# webhook/queue.py
class SignalQueue:
    """Redis LIST + BRPOP 封装。"""
    def push(self, signal: Signal) -> None: ...  # LPUSH signals
    def pop(self, timeout: int = 0) -> Optional[Signal]: ...  # BRPOP
    def push_inflight(self, signal: Signal) -> None: ...  # 处理中暂存，崩溃可恢复
    def remove_inflight(self, signal_id: str) -> None: ...  # 处理成功移除
    def push_dead(self, signal: Signal, reason: str) -> None: ...  # 死信队列

# webhook/dedup.py
class Deduper:
    """Redis SET + hash(signal_json) + TTL 5min。"""
    def is_duplicate(self, signal: Signal) -> bool: ...

# execution/worker.py
class Worker:
    def run(self) -> None:
        """主循环：BRPOP → 风控 → 下单 → 持久化。崩溃时 inflight 队列可恢复。"""
        ...

# execution/executor.py
class Executor:
    def execute(self, signal: Signal) -> str: ...  # 返回 order_id

# execution/risk.py
class RiskChecker:
    def check(self, signal: Signal) -> "RiskDecision": ...  # 通过/拒绝/降仓
```

### 12.3 M3 模拟盘

```python
# M3 待实现（M2 的 webhook/worker 已跑通）
class Broker(ABC):
    @abstractmethod
    def place_order(self, signal: Signal) -> str: ...  # 返回 order_id
    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...
    @abstractmethod
    def get_position(self, inst_id: str) -> "Position": ...

class PaperBroker(Broker):
    """用 OKX 模拟盘 API 实现 Broker。复用 M1 OKXClient，加 x-simulated-trading:1。"""
    ...

class AlertSimulator:
    """本地脚本模拟 TV alert 推送 webhook。读取 Pine 回测的成交记录按时间轴 replay。"""
    def replay(self, bars: list[Bar], strategy: Strategy) -> None: ...

class StrategyState:
    @staticmethod
    def save(state: dict, strategy_id: str) -> None: ...  # SQLite
    @staticmethod
    def load(strategy_id: str) -> dict: ...
```

### 12.4 M4 监控

```python
# M4 待实现
class Monitor:
    """独立进程。监控 OKX 账户 + Redis 队列长度 + worker 心跳 + 死信队列。"""
    def start(self) -> None: ...
    def stop(self) -> None: ...

class Alerter(ABC):
    @abstractmethod
    def send(self, subject: str, body: str) -> None: ...

class EmailAlerter(Alerter):
    """SMTP 邮件告警。"""
    ...

class Heartbeat:
    """worker/monitor 每分钟一次写心跳到 Redis + 本地日志。"""
    def tick(self) -> None: ...
    def check(self, process: str) -> bool: ...  # 检查某进程心跳是否超时
```

### 12.5 M5 实盘

```python
# M5 待实现（接收 TV 真 webhook）
class LiveBroker(Broker):
    """OKX 实盘 API 实现 Broker。继承 M3 接口。"""
    ...

class Reconciler:
    def sync_with_exchange(self) -> None:
        """本地状态与远端对账；不一致以远端为准并告警。worker 启动前先调用。"""
        ...

class StrategyRouter:
    """根据 alert.strategy_id 路由到不同风控参数集。"""
    def route(self, signal: Signal) -> "RiskConfig": ...
```

### 12.6 M6 上云

- systemd unit 文件管 3 进程：`quant-webhook.service`、`quant-worker.service`、`quant-monitor.service`
- 任一进程崩溃自动重启；重启时 worker 先做 reconciliation 再消费
- 配置文件环境化（`.env` + `config.toml` 走环境变量引用）
- 日志持久化到磁盘并 rotation
- 启动时自动恢复策略状态（依赖 F4.7）
- Redis 配置 `maxmemory-policy allkeys-lru` + AOF 持久化（崩溃可恢复）

### 12.7 数据契约总结

M1 产出被下游消费的核心类型：

| 类型 | 消费方 |
|---|---|
| `Bar` | M2 回测、M3/M5 策略输入 |
| `Tick` | M3/M5 实时策略输入 |
| `Instrument` | M3/M5 下单参数校验（minSz/tickSz） |
| `OKXClient` | M3 模拟盘、M5 实盘的所有 API 调用；worker 通过 Executor 间接调用 |
| `RateLimiter` | M3/M5 共用单例 |
| `Settings` | 全局配置；M2 起扩展 `[redis]`、`[webhook]`、`[strategies.*]` 段 |

M1 不实现 `Signal`/`Order`/`Position`/`RiskConfig` 等类型，留待 M2 spec 设计。

---

## 13. 实现顺序

建议按以下顺序实施，每步可独立测试：

1. **Foundation**（2-3天）：`config.py`, `logger.py`, `errors.py`, `types.py`；写 `pyproject.toml`；跑通"加载配置→打印日志"
2. **Models**（1天）：4 个 dataclass；纯数据，零依赖
3. **Signer + 测试**（1天）：与官方示例对齐
4. **RateLimiter + 测试**（2天）：令牌桶数学正确
5. **OKXClient + 集成测试**（3-4天）：用 respx mock 跑通所有错误分支
6. **BarStorage + 测试**（2天）：读写一致性、去重
7. **DataValidator + 测试**（1-2天）
8. **DataAligner + 测试**（1-2天）
9. **CandleFetcher + 测试**（2-3天）：断点续传、补数据
10. **CLI 入口**（1天）
11. **真实烟测**（1天）：拉取 BTC-USDT 真实K线

> 时间是粗略估计，实际按用户节奏推进，不强制。

---

## 14. 验收标准映射

| PRD F编号 | 验收标准 | spec对应章节 | 测试用例 |
|---|---|---|---|
| F1.1 | 拉取BTC-USDT 2024全年1H共8784根；parquet可读；时间戳连续 | §4.3, §6.4 | test_candle_fetcher.py |
| F1.2 | WS连续1小时无断线；断网5秒重连 | §4.4 | test_ws_client.py（M1只烟测） |
| F1.3 | 拉取全部SPOT+SWAP配置；JSON含instId等 | §6.1, §11.2 | test_instrument_fetcher.py |
| F1.4 | 资金费率历史90天；时间戳与值成对 | §5.5, §6 | test_funding_rate.py |
| F1.5 | 1年1H parquet < 200KB；可pd.read_parquet | §6.3, §6.4 | test_bar_storage.py |
| F1.6 | 50011触发睡眠2秒；3次失败抛异常 | §4.2, §4.3, §9.2 | test_okx_client.py |
| F1.7 | 删10根能识别缺失；自动补拉；去重；OHLC非法标记 | §6.5 | test_validator.py |
| F1.8 | BTC+ETH对齐输出8784行；缺失行按策略填充 | §6.6 | test_aligner.py |

---

## 15. 风险与开放问题

### 15.1 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| OKX API 突然限频更严 | M1 烟测失败 | 已在 RateLimiter 抽象配置化，易调 |
| parquet string 列体积大 | 1年K线>200KB | 看实测，必要时换 parquet DECIMAL |
| Decimal 性能瓶颈 | 大数据量回测慢 | M1数据量小可接受；M2回测时优化 |
| OKX 模拟盘 instId 与实盘不同 | M3模拟盘需独立拉配置 | 已在 F1.3 说明 |
| `python-dateutil` 与 `zoneinfo` 重叠 | 依赖冗余 | M1 实施时只用其一 |

### 15.2 开放问题

| 问题 | 决策时机 |
|---|---|
| parquet 是否用 DECIMAL 类型代替 string？ | M1 烟测后看体积 |
| 是否要支持 `polars` 替换 `pandas`？ | M2 启动时评估 |
| WSClient 是否在 M1 实现完整重连逻辑？ | M3 启动时再细化 |
| 是否引入 `apscheduler` 做定时校时？ | M3 启动时决定 |
| `CandleFetcher` 的断点续传存哪里（SQLite/JSON）？ | M1 实施时决定 |

---

## 16. 变更记录

| 日期 | 版本 | 修订人 | 说明 |
|---|---|---|---|
| 2026-10-17 | v1.0 | AI起草 | 初版。基于 PRD v0.4 与 OKX API 速查 v1.0。覆盖 M1 全部详设；M2-M6 仅接口契约 |
| 2026-10-17 | v1.1 | 架构调整 + AI更新 | 适配 PRD v0.5（webhook 方案B：FastAPI+Redis+worker）。M1 本身不变；§7 config.toml 预留 `[redis]`/`[webhook]` 段（M1 不实现）；§12 接口契约重写：M2 加双回测注释、新增 §12.2 WebhookServer/SignalQueue/Deduper/Worker/Executor/RiskChecker、§12.3 加 AlertSimulator、§12.4 Monitor 加队列/心跳监控、§12.5 加 StrategyRouter、§12.6 改 systemd 编排 3 进程+Redis AOF、§12.7 数据契约加 Settings 扩展点说明 |
