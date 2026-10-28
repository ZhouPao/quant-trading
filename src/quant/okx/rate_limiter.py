"""OKX 限频器（spec §4.2）。

按端点组维护独立令牌桶，阻塞 acquire。
命中 50011 时调用 penalize 强制阻塞 2 秒。

端点组配置来自 docs/reference/OKX_API_REFERENCE.md 限频表：
- public:   20 req/2s per IP（行情、产品）
- account:  10 req/2s per UID（账户）
- history:  10 req/2s per UID（订单历史）
- trade_*:  60 req/2s per UID+instId
- algo:     20 req/2s per UID
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock

from quant.foundation.errors import RateLimitTimeoutError


@dataclass(frozen=True, slots=True)
class BucketConfig:
    """令牌桶配置。"""

    capacity: int  # 桶容量
    refill_per_sec: float  # 每秒补充速率


# 端点组配置（与 OKX_API_REFERENCE.md 一致）
GROUP_CONFIG: dict[str, BucketConfig] = {
    "public": BucketConfig(capacity=20, refill_per_sec=10.0),
    "account": BucketConfig(capacity=10, refill_per_sec=5.0),
    "trade_order": BucketConfig(capacity=60, refill_per_sec=30.0),
    "trade_batch": BucketConfig(capacity=300, refill_per_sec=150.0),
    "trade_cancel": BucketConfig(capacity=60, refill_per_sec=30.0),
    "algo": BucketConfig(capacity=20, refill_per_sec=10.0),
    "history": BucketConfig(capacity=10, refill_per_sec=5.0),
}


class _Bucket:
    """单个令牌桶实现（线程安全）。"""

    def __init__(self, name: str, cfg: BucketConfig) -> None:
        self._name = name
        self._cfg = cfg
        self._tokens = float(cfg.capacity)
        self._last = time.monotonic()
        self._lock = Lock()
        self._penalized_until = 0.0

    def acquire(self, timeout: float = 10.0) -> None:
        """阻塞直到获取 1 个令牌；超时抛 RateLimitTimeoutError。"""
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                now = time.monotonic()
                # 1. 处于惩罚期，先等惩罚结束
                if now < self._penalized_until:
                    wait = self._penalized_until - now
                    if now + wait > deadline:
                        raise RateLimitTimeoutError(group=self._name, timeout=timeout)
                    time.sleep(wait)
                    continue
                # 2. 补充令牌
                self._refill(now)
                # 3. 有令牌，扣减并返回
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # 4. 令牌不足，等待补充
                need = 1.0 - self._tokens
                wait = need / self._cfg.refill_per_sec
                if now + wait > deadline:
                    raise RateLimitTimeoutError(group=self._name, timeout=timeout)
                time.sleep(wait)

    def penalize(self, seconds: float = 2.0) -> None:
        """命中 50011 时强制阻塞该桶并清空令牌。"""
        with self._lock:
            self._penalized_until = max(self._penalized_until, time.monotonic() + seconds)
            self._tokens = 0.0

    def _refill(self, now: float) -> None:
        """根据时间流逝补充令牌。"""
        # 惩罚期内不补充令牌
        if now < self._penalized_until:
            return
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(
                float(self._cfg.capacity),
                self._tokens + elapsed * self._cfg.refill_per_sec,
            )
            self._last = now

    @property
    def tokens(self) -> float:
        """当前可用令牌数（仅供调试/测试）。"""
        with self._lock:
            self._refill(time.monotonic())
            return self._tokens


class RateLimiter:
    """端点组分桶限频器。"""

    def __init__(self, config: dict[str, BucketConfig] | None = None) -> None:
        cfg = config or GROUP_CONFIG
        self._buckets = {name: _Bucket(name, c) for name, c in cfg.items()}

    def acquire(self, group: str, timeout: float = 10.0) -> None:
        """阻塞直到获取 group 的 1 个令牌。"""
        if group not in self._buckets:
            raise ValueError(f"unknown group: {group}")
        self._buckets[group].acquire(timeout)

    def penalize(self, group: str, seconds: float = 2.0) -> None:
        """命中 50011 时强制阻塞该桶 seconds 秒。"""
        if group not in self._buckets:
            raise ValueError(f"unknown group: {group}")
        self._buckets[group].penalize(seconds)

    def tokens(self, group: str) -> float:
        """查询某组当前可用令牌数（仅供调试）。"""
        if group not in self._buckets:
            raise ValueError(f"unknown group: {group}")
        return self._buckets[group].tokens


__all__ = ["RateLimiter", "BucketConfig", "GROUP_CONFIG"]
