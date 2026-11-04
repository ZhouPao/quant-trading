"""Context 策略运行上下文（spec §4.1）。

提供策略可访问的状态：持仓、K线窗口、参数、现金、当前时间戳。
引擎在每次调 on_bar 前更新 ctx。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from quant.backtest.position import Position
from quant.models.bar import Bar


@dataclass(slots=True)
class Context:
    """策略运行上下文。

    持仓状态、历史 K线窗口（最近 N 根，含当前 bar）、参数字典、可用现金、当前时间戳。

    引擎每根 bar 调 on_bar 前更新 ctx.timestamp 与 ctx.bars_window；
    on_bar 返回 Signal 后引擎调 Matcher 产生 Fill，再调 Position.apply_fill 更新 ctx.position。

    Attributes:
        position: 当前持仓状态
        bars_window: 最近 N 根 K线列表（含当前 bar），按时间升序；窗口大小由策略参数控制
        params: 策略参数字典（从 toml 加载），策略在 on_init 中读取
        cash: 可用现金（计价币金额）
        timestamp: 当前 bar 的时间戳 UTC ms
    """

    position: Position
    bars_window: list[Bar] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    cash: Decimal = Decimal("0")
    timestamp: int = 0
    #: 最近一次 on_bar 返回 None 的具体原因（M3-5）：策略写入，引擎在
    #: 无信号时透出到 EngineTick.reason，便于回答「为什么没开仓」。
    #: 选字段而不是改 on_bar 返回类型：现有一百多处策略实现无需改动。
    last_block_reason: str = ""

    @property
    def equity_at_price(self) -> Decimal:
        """以最近 bar 收盘价估值的总权益 = cash + position market_value。

        注意：bars_window 必须非空才能调用。
        """
        if not self.bars_window:
            raise ValueError("bars_window is empty; cannot compute equity")
        last_bar = self.bars_window[-1]
        return self.cash + self.position.market_value(last_bar.close)

    def current_bar(self) -> Bar:
        """获取当前（最新一根）bar。

        bars_window 必须非空。
        """
        if not self.bars_window:
            raise ValueError("bars_window is empty; no current bar")
        return self.bars_window[-1]


__all__ = ["Context"]
