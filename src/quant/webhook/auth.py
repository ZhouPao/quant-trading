"""Webhook 鉴权（spec §6.3）。

两种方式，配置启用哪种由 server 决定：
- shared token：X-Auth-Token 头与配置值常量时间比较
- HMAC-SHA256：X-Signature 头 = hex(HMAC(secret, raw_body))
"""
from __future__ import annotations

import hashlib
import hmac


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """校验 HMAC-SHA256 签名（常量时间比较）。"""
    if not signature or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_token(token: str, expected: str) -> bool:
    """校验共享 token（常量时间比较）。空 expected 表示不校验。"""
    if not expected:
        return True
    if not token:
        return False
    return hmac.compare_digest(token, expected)


__all__ = ["verify_signature", "verify_token"]
