"""Foundation logger: 日志器（spec §7.4 + §8）。

特性：
- 控制台 + 文件双输出
- 文件按大小/时间轮转
- 自动脱敏 secret/passphrase/api_key（含 JSON 字段形式）
- UTC 时间戳

使用：
    from quant.foundation.logger import setup_logger, get_logger
    setup_logger(level="INFO", file="./logs/quant.log", rotation="10 MB", retention="30 days")
    log = get_logger("okx.client")
    log.info("GET /api/v5/market/history-candles 200, 100 bars in 320ms")
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

# 脱敏字段集合（小写比较）
_SENSITIVE_KEYS = (
    "secret", "passphrase", "api_key", "apisecret", "apikey",
    # OKX HTTP 头大写形式
    "ok-access-key", "ok-access-passphrase", "ok-access-sign",
)

# 匹配 JSON 形式：  "api_key": "xxxx"  /  'api_key': 'xxxx'（Python dict repr）
_JSON_PATTERN = re.compile(
    r'([\'"](?:' + "|".join(_SENSITIVE_KEYS) + r')[\'"]:\s*[\'"])([^\'"]+)([\'"])',
    re.IGNORECASE,
)

# 匹配 kv 形式：  api_key=xxxx  /  secret:xxxx  /  passphrase is xxxx
_KV_PATTERN = re.compile(
    r"\b(" + "|".join(_SENSITIVE_KEYS) + r")(\s*(?:[=:]|\bis\b)\s*)([^\s,;'\"]+)",
    re.IGNORECASE,
)


def _redact_message(msg: str) -> str:
    """替换消息中出现的敏感值为 ***REDACTED***。"""
    msg = _JSON_PATTERN.sub(lambda m: f'{m.group(1)}***REDACTED***{m.group(3)}', msg)
    msg = _KV_PATTERN.sub(lambda m: f'{m.group(1)}{m.group(2)}***REDACTED***', msg)
    return msg


def _redact_filter(record: dict[str, Any]) -> bool:
    """loguru patcher：在写入前替换敏感字段。"""
    try:
        record["message"] = _redact_message(record["message"])
    except Exception:
        pass
    return True


def setup_logger(
    level: str = "INFO",
    file: str | Path = "./logs/quant-trading.log",
    rotation: str = "10 MB",
    retention: str = "30 days",
) -> None:
    """初始化全局日志器。

    必须在使用 logger 之前调用一次。重复调用会先 remove 旧 handler。
    """
    logger.remove()
    # 控制台输出：人友好的本地时间格式
    logger.add(
        lambda msg: print(msg, end=""),
        level=level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS}|{level:<5}|{module}|{message}"
        ),
        filter=_redact_filter,
        enqueue=True,
    )
    # 文件输出：带 UTC 标识 + 轮转 + 保留
    logger.add(
        str(file),
        level=level,
        rotation=rotation,
        retention=retention,
        format=(
            "{time:YYYY-MM-DDTHH:mm:ss.SSSZ}|{level:<5}|{module}|{message}"
        ),
        filter=_redact_filter,
        enqueue=True,
        encoding="utf-8",
    )


def get_logger(name: str):
    """返回一个绑定 name 的 logger 实例。"""
    return logger.bind(name=name)


__all__ = ["setup_logger", "get_logger", "_redact_message"]
