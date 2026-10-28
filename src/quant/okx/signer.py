"""OKX 签名器（spec §4.1）。

OKX V5 API 使用 HMAC-SHA256 签名认证，签名内容为：
    timestamp + METHOD + request_path + body

签名结果 Base64 编码后附在 HTTP 头 `OK-ACCESS-SIGN`。

纯函数无状态，便于单元测试。
"""
from __future__ import annotations

import base64
import hashlib
import hmac


def sign(
    timestamp: str,
    method: str,
    request_path: str,
    body: str,
    secret: str,
) -> str:
    """生成 OKX V5 签名。

    Args:
        timestamp: ISO 8601 UTC 毫秒，如 "2024-01-15T10:30:00.123Z"
        method: HTTP 方法大小写不敏感，内部转大写
        request_path: 含 query string，如 "/api/v5/market/candles?instId=BTC-USDT"
        body: POST body 字符串；GET 为 ""
        secret: API Secret

    Returns:
        Base64 编码的 HMAC-SHA256 签名字符串
    """
    prehash = timestamp + method.upper() + request_path + body
    digest = hmac.new(
        secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


__all__ = ["sign"]
