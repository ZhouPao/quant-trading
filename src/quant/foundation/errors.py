"""Foundation errors: 项目异常体系（spec §9.1）。

所有项目异常都继承自 QuantError，方便上层统一捕获与日志记录。
"""
from __future__ import annotations


class QuantError(Exception):
    """所有项目异常的基类。"""


class NetworkError(QuantError):
    """网络异常（连接超时/DNS失败）。"""


class RateLimitError(QuantError):
    """OKX 返回 50011（服务端限频）。"""


class RateLimitTimeoutError(RateLimitError):
    """本地限频器 acquire 超时。"""

    def __init__(self, group: str, timeout: float) -> None:
        super().__init__(f"acquire {group} timeout after {timeout}s")
        self.group = group
        self.timeout = timeout


class AuthError(QuantError):
    """鉴权失败：50101/50111/50113/50105。"""

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


class ServerError(QuantError):
    """OKX 服务端 5xx。"""

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


class InsufficientBalanceError(QuantError):
    """51008：余额不足。"""


class ValidationError(QuantError):
    """数据校验失败（缺失/重复/OHLC非法）。"""


class NoFillError(QuantError):
    """回测限价单未穿透，未成交（spec §4.4）。"""


class ParseError(QuantError):
    """webhook alert JSON 解析/字段校验失败（spec §6.2）。"""


class RiskRejectError(QuantError):
    """风控校验失败（spec §10）。"""


class QueueError(QuantError):
    """Redis 队列不可达或操作失败（spec §13）。"""


class StateError(QuantError):
    """SQLite 状态读写失败（spec §13）。"""


class OrderError(QuantError):
    """交易所拒单（下单接口 sCode != 0 或状态异常）。"""

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg
