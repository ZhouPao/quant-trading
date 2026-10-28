"""Backtest: Python 回测引擎（与 TradingView Pine 双回测）。M2 起实现。"""
from quant.backtest.context import Context
from quant.backtest.engine import Backtest, BacktestResult
from quant.backtest.matcher import Matcher
from quant.backtest.optimizer import GridOptimizer
from quant.backtest.order import Fill, Order, OrderStatus
from quant.backtest.position import Position
from quant.backtest.report import ReportGenerator
from quant.backtest.signal import OrderType, Side, Signal
from quant.backtest.strategy import Strategy

__all__ = [
    "Signal",
    "Side",
    "OrderType",
    "Order",
    "Fill",
    "OrderStatus",
    "Position",
    "Context",
    "Strategy",
    "Matcher",
    "Backtest",
    "BacktestResult",
    "ReportGenerator",
    "GridOptimizer",
]
