"""Foundation: 配置、日志、错误、通用类型（spec §7-§9）。

模块依赖：无内部依赖，是其他模块的底座。
"""
from quant.foundation.config import Settings
from quant.foundation.errors import (
    AuthError,
    InsufficientBalanceError,
    NetworkError,
    QuantError,
    RateLimitError,
    RateLimitTimeoutError,
    ServerError,
    ValidationError,
)
from quant.foundation.logger import get_logger, setup_logger

__all__ = [
    "Settings",
    "setup_logger",
    "get_logger",
    "QuantError",
    "NetworkError",
    "RateLimitError",
    "RateLimitTimeoutError",
    "AuthError",
    "ServerError",
    "InsufficientBalanceError",
    "ValidationError",
]
