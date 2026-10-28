"""OKX 客户端层（spec §4）。

包含：
- Signer: HMAC-SHA256 签名
- RateLimiter: 令牌桶限频
- OKXClient: REST 客户端（自动签名/限频/重试/时间同步）
- WSClient: WebSocket 客户端（自动重连/订阅恢复）
"""
from quant.okx.client import OKXClient
from quant.okx.rate_limiter import BucketConfig, RateLimiter
from quant.okx.signer import sign
from quant.okx.ws_client import WSClient, parse_ticker

__all__ = [
    "OKXClient",
    "RateLimiter",
    "BucketConfig",
    "sign",
    "WSClient",
    "parse_ticker",
]
