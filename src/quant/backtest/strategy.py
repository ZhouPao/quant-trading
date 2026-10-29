"""Strategy 策略基类（spec §4.1）。

定义策略生命周期：on_init → 多次 on_bar → on_finish。
Python 副本必须与 Pine 版本逻辑严格对齐（PRD F2.1）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING

from quant.backtest.context import Context
from quant.backtest.signal import Signal
from quant.models.bar import Bar

if TYPE_CHECKING:
    from quant.backtest.order import Fill


class Strategy(ABC):
    """策略基类。

    Python 副本必须与 Pine 版本逻辑严格对齐（PRD F2.1）。

    生命周期（spec §4.1）：
    1. on_init(ctx)：初始化。加载参数、初始化状态。仅调用一次。
    2. on_bar(bar, ctx) -> Optional[Signal]：每根 K线回调。
       返回 Signal 表示产生交易信号；返回 None 表示无信号。
       注意：禁止使用未来数据（只能用 bar.ts 及之前的数据）。
    3. on_trade(trade, ctx)：成交回调。默认空实现，子类可重写以更新状态。
    4. protective_exit(bar, ctx)：持仓中的保护性出场价位，默认不触发。
    5. on_finish(ctx)：结束回调。默认空实现。

    子类必须实现 strategy_id / on_init / on_bar；其余为可选钩子。
    """

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """策略唯一 ID，与 Pine strategy(...) 的 id 一致。"""

    @abstractmethod
    def on_init(self, ctx: Context) -> None:
        """初始化。加载参数、初始化状态。仅调用一次。

        Args:
            ctx: 运行上下文。策略可在此读取 ctx.params 加载参数，
                 初始化内部状态（如移动平均窗口）。
        """

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: Context) -> Signal | None:
        """每根 K线回调。返回信号或 None。

        禁止使用未来数据：只能访问 bar 及之前的数据。
        ctx.bars_window[-1] 即当前 bar；ctx.bars_window[-2] 即前一根。

        Args:
            bar: 当前 K线
            ctx: 运行上下文（已更新 timestamp 与 bars_window）

        Returns:
            Signal 实例表示产生交易信号；None 表示无信号
        """

    def on_trade(self, trade: Fill, ctx: Context) -> None:
        """成交回调。默认空实现，子类可重写以更新策略状态。

        引擎在 Matcher 模拟成交后调用，传入 Fill。策略可在此：
        - 更新自定义状态（如跟踪最高价、加减仓次数）
        - 记录成交日志
        - 触发后续逻辑（如移动止损）

        Args:
            trade: 成交记录
            ctx: 运行上下文（position 已更新）
        """
        return None

    def protective_exit(self, bar: Bar, ctx: Context) -> Decimal | None:
        """bar 内保护性出场价位。默认空实现，不做任何出场。

        持仓期间每根 bar 调用一次（成交当根的下一根起，且当根开盘的
        挂起信号已成交之后）。返回非 None 表示该价位在 bar 内被触及，
        引擎按此价位立即成交、不套滑点，并跳过本根的 on_bar。

        约定：
        - 只有多仓需要实现；引擎仅在 position.is_long 时调用
        - 同一根 bar 内止损与止盈同时触及，实现方须返回止损价（保守）
        - 实现方须自行保证价位在开仓时冻结，避免使用未来数据

        Args:
            bar: 当前 K 线
            ctx: 运行上下文（position 已更新）

        Returns:
            触发的出场价位；未触发返回 None
        """
        return None

    def on_finish(self, ctx: Context) -> None:
        """结束回调。默认空实现。

        引擎在所有 bar 处理完后调用。策略可在此：
        - 输出最终状态
        - 清理资源
        """
        return None


__all__ = ["Strategy"]
