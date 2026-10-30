# MS2 回测 + Webhook 框架 详细设计文档

> 文档版本：v1.1
> 创建日期：2026-10-17
> 最后更新：2026-10-30
> 状态：已实现（MS2 已交付，见 PRD §8.1）
> 上游需求：[PRD v0.6](../requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md)（本文档编写时对应 v0.5）
> 上游设计：[MS1 数据底座详设 v1.2](2026-10-17-ms1-data-foundation-design.md)
> 关联参考：[OKX API速查](../reference/OKX_API_REFERENCE.md)
> 设计方案：方案B（FastAPI + Redis + worker），延续 MS1 §12.2 接口契约

> **编号约定（重要）**：本文档标题中的 **MS2** 是**里程碑编号**，对应 PRD v0.6 §8.1 的 MS2「回测 + Webhook」。
> 它与 PRD 的**模块编号** M2（市场监控）、M4（回测分析）**不是一回事**。
> 原文件名 `m2-backtest-webhook-design.md` 已于 2026-10-30 更名为 `ms2-*`。
> 下文正文若出现「M1/M2/M3…M6」，**一律指里程碑**。

---

## 目录

1. [引言](#1-引言)
2. [整体架构](#2-整体架构)
3. [模块清单与依赖关系](#3-模块清单与依赖关系)
4. [回测引擎设计](#4-回测引擎设计)
5. [策略双副本机制](#5-策略双副本机制)
6. [Webhook 服务设计](#6-webhook-服务设计)
7. [信号队列设计](#7-信号队列设计)
8. [去重设计](#8-去重设计)
9. [Worker 设计](#9-worker-设计)
10. [风控设计](#10-风控设计)
11. [状态持久化设计](#11-状态持久化设计)
12. [配置扩展](#12-配置扩展)
13. [错误处理与重试策略](#13-错误处理与重试策略)
14. [测试策略](#14-测试策略)
15. [实现顺序](#15-实现顺序)
16. [验收标准映射](#16-验收标准映射)
17. [风险与开放问题](#17-风险与开放问题)
18. [变更记录](#18-变更记录)

---

## 1. 引言

### 1.1 目的

本设计文档定义量化交易平台 M2 里程碑（回测引擎 + Webhook 框架）的详细设计。M2 在 M1 数据底座之上，构建：

- **回测引擎**：Python 副本，与 TradingView Pine 双回测交叉验证；事件驱动、可配置滑点/手续费、输出绩效指标与 HTML 报告
- **Webhook 框架**：FastAPI server 接收 TV alert、Redis 队列、worker 主循环、风控规则、SQLite 状态持久化的骨架；M3 用本地脚本模拟 alert 跑通完整链路

### 1.2 范围

- **包含**：
  - 策略接口（`Strategy` 基类 + `on_bar/on_tick/on_trade` 回调）
  - 事件驱动回测引擎（`Backtest` 类 + 成交模拟 + 绩效指标 + 报告）
  - 参数寻优（网格搜索 + 禁止未来函数）
  - 样例策略双副本（SMA Cross + RSI）
  - FastAPI webhook server（端点 + 鉴权 + 死信队列）
  - Redis 信号队列（LPUSH/BRPOP + inflight + dead）
  - 信号去重（Redis SET + TTL）
  - Worker 主循环（BRPOP → 风控 → 下单 → 持久化 + 心跳）
  - 风控规则（仓位上限/日亏损/连续亏损保护）
  - SQLite 状态持久化（信号/订单/持仓 + 崩溃恢复骨架）
- **不包含**：
  - 真实下单执行（M3 模拟盘 + M5 实盘）
  - 实盘 reconciliation 对账（M5）
  - 监控告警（M4）
  - VPS 部署（M6）

### 1.3 设计原则

延续 M1 §1.3：

1. **简单优先**：M2 是过渡里程碑，webhook/worker 只搭骨架，不过度设计风控规则与执行细节
2. **接口稳定**：`Strategy`、`Signal`、`Order`、`Position` 等接口需为 M3-M5 留好扩展点
3. **可测试**：回测引擎是 M2 核心，单元测试覆盖率 ≥ 85%；webhook/worker 用 `fakeredis` + `TestClient` 跑通
4. **双副本对齐**：Pine 与 Python 策略必须逻辑严格对齐，双回测偏离 > 5% 禁止上实盘（PRD F2.1）
5. **M1 复用**：直接复用 `Bar`、`Instrument`、`OKXClient`、`RateLimiter`、`Settings`，不重写

### 1.4 假设与依赖

- **M1 已完成**：`Bar`/`Instrument`/`FundingRate`/`OKXClient`/`BarStorage`/`DataAligner` 等可正常使用
- **本地 Redis**：M2 起依赖 Redis，本地用 `brew install redis`，启动 `redis-server`
- **Python 3.11+**：用 `typing.Literal`、`typing.Self`、`decimal.Decimal` 不丢精度
- **TradingView Pine 双副本**：用户已在 TV 上有 Pine 策略源码，Python 副本由本仓库 `strategies/python/` 维护
- **本地时区统一 UTC**：所有时间戳 UTC ms，与 M1 一致

---

## 2. 整体架构

### 2.1 M2 在整体架构中的位置

```
┌──────────────────────────────────────────────────────────────┐
│                     M2 新增模块                                │
│                                                              │
│  ┌────────────┐    ┌────────────┐    ┌────────────┐          │
│  │ strategies │───▶│  backtest  │    │  webhook   │          │
│  │ (Pine+Py)  │    │  (engine)  │    │  (FastAPI) │          │
│  └────────────┘    └────────────┘    └─────────┬──┘          │
│                                                  │            │
│                                                  ▼            │
│  ┌────────────┐    ┌────────────┐    ┌────────────┐          │
│  │   risk     │◀───│  worker    │◀───│   queue    │          │
│  │ (rules)    │    │ (BRPOP)    │    │ (Redis)    │          │
│  └────────────┘    └─────────┬──┘    └────────────┘          │
│                               │                                │
│                               ▼                                │
│                       ┌────────────┐    ┌────────────┐        │
│                       │  executor  │    │ sqlite     │        │
│                       │  (mock M2) │    │  store     │        │
│                       └────────────┘    └────────────┘        │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
            ┌──────────────────────────────────┐
            │             M1 (已完成)            │
            │  OKXClient / Bar / Instrument     │
            │  BarStorage / DataAligner         │
            │  Settings / Logger / Errors       │
            └──────────────────────────────────┘
```

### 2.2 不引入的概念（M2 范围外）

- 实盘 `LiveBroker`（M5）
- 模拟盘 `PaperBroker` 真实下单（M3）
- 监控告警 `Monitor`（M4）
- ML 策略（PRD §5.2 排在 M2 之后）

---

## 3. 模块清单与依赖关系

### 3.1 M2 新增目录结构

```
src/quant/
├── backtest/                # M2 新增：回测引擎
│   ├── __init__.py
│   ├── strategy.py          # Strategy ABC + Context
│   ├── signal.py            # Signal dataclass
│   ├── order.py             # Order / Fill dataclass
│   ├── position.py          # Position dataclass
│   ├── engine.py            # Backtest 事件驱动主循环
│   ├── matcher.py           # 成交模拟（市价/限价/滑点/手续费）
│   ├── metrics.py           # 绩效指标计算
│   ├── report.py            # CSV/HTML 报告生成
│   └── optimizer.py         # 网格参数寻优
├── strategies/              # M2 新增：策略双副本
│   ├── __init__.py
│   ├── pine/                # TradingView Pine 源码（.pine）
│   │   ├── sma_cross.pine
│   │   └── rsi.pine
│   └── python/              # Python 副本
│       ├── __init__.py
│       ├── sma_cross.py
│       └── rsi.py
├── webhook/                 # M2 新增：FastAPI webhook server
│   ├── __init__.py
│   ├── server.py            # WebhookServer 应用工厂
│   ├── parser.py            # alert JSON → Signal
│   └── auth.py              # HMAC 鉴权
├── queue/                   # M2 新增：Redis 队列封装
│   ├── __init__.py
│   └── signal_queue.py      # SignalQueue: LPUSH/BRPOP/inflight/dead
├── execution/               # M2 新增：worker + 执行层
│   ├── __init__.py
│   ├── worker.py            # Worker 主循环
│   └── executor.py          # Executor（M2 mock，M3 接 OKX 模拟盘）
├── risk/                    # M2 新增：风控
│   ├── __init__.py
│   ├── checker.py           # RiskChecker
│   └── config.py            # RiskConfig（从 strategies/*.toml 加载）
└── state/                   # M2 新增：SQLite 状态持久化
    ├── __init__.py
    └── sqlite_store.py      # SQLiteStore: 信号/订单/持仓/心跳

config/
└── strategies/              # M2 新增：策略风控参数
    ├── sma_cross.toml
    └── rsi.toml

tests/
├── test_strategy.py         # Strategy 基类测试
├── test_engine.py           # Backtest 引擎测试
├── test_matcher.py          # 成交模拟测试
├── test_metrics.py          # 绩效指标测试
├── test_report.py            # 报告生成测试
├── test_optimizer.py        # 参数寻优测试
├── strategies/              # 样例策略双副本对齐测试
│   ├── test_sma_cross.py
│   └── test_rsi.py
├── test_webhook_server.py   # webhook FastAPI 测试
├── test_queue.py            # Redis 队列测试（fakeredis）
├── test_dedup.py            # 去重测试
├── test_worker.py           # Worker 主循环测试
├── test_risk.py             # 风控规则测试
└── test_sqlite_store.py     # SQLite 持久化测试
```

### 3.2 M2 新增依赖

`pyproject.toml [project.optional-dependencies]` 已声明 `m2` 组：

```toml
m2 = [
    "fastapi>=0.110",       # webhook server
    "uvicorn[standard]>=0.29",  # ASGI server
    "redis>=5.0",           # 队列 + 去重
]
```

**测试依赖（dev 组追加）**：

```toml
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
    "ruff>=0.5",
    "fakeredis>=2.21",      # M2 新增：Redis mock
    "httpx>=0.27",          # M2 新增：FastAPI TestClient
    "jinja2>=3.1",          # M2 新增：HTML 报告模板
]
```

### 3.3 模块依赖矩阵（M2 新增）

| 模块 | 依赖（M1） | 依赖（M2 新增） |
|---|---|---|
| `backtest/` | `models.Bar`, `foundation.config`, `foundation.logger` | — |
| `strategies/python/` | `models.Bar` | `backtest.Strategy`, `backtest.Signal` |
| `webhook/` | `foundation.errors`, `foundation.logger` | `queue.SignalQueue`, `models.*` |
| `queue/` | — | `foundation.errors`, `backtest.Signal` |
| `execution/` | `okx.OKXClient`（M3 接入） | `queue.SignalQueue`, `risk.RiskChecker`, `state.SQLiteStore` |
| `risk/` | `foundation.config`, `foundation.logger` | — |
| `state/` | `foundation.logger` | — |

**关键约束**：
- `backtest/` **不依赖** `webhook/` / `execution/`：回测引擎可独立运行
- `webhook/` 不依赖 `execution/`：server 只负责入队，下单逻辑在 worker 进程
- `execution/worker.py` 串联 `queue` + `risk` + `state` + `executor`

---

## 4. 回测引擎设计

### 4.1 Strategy 基类与生命周期

```python
# src/quant/backtest/strategy.py
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional

from quant.models.bar import Bar
from quant.backtest.signal import Signal
from quant.backtest.context import Context


class Strategy(ABC):
    """策略基类。Python 副本必须与 Pine 版本逻辑严格对齐。

    生命周期：on_init → 多次 on_bar → on_finish
    on_bar 返回 Optional[Signal]，None 表示无信号
    """

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """策略唯一 ID，与 Pine strategy(...) 的 id 一致。"""

    @abstractmethod
    def on_init(self, ctx: Context) -> None:
        """初始化。加载参数、初始化状态。仅调用一次。"""

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: Context) -> Optional[Signal]:
        """每根 K线回调。返回信号或 None。
        注意：禁止使用未来数据（只能用 bar.ts 及之前的数据）。
        """

    def on_trade(self, trade: "Trade", ctx: Context) -> None:
        """成交回调。默认实现空，子类可重写以更新状态。"""
        return None

    def on_finish(self, ctx: Context) -> None:
        """结束回调。默认实现空。"""
        return None
```

`Context` 提供策略可访问的状态：

```python
# src/quant/backtest/context.py
@dataclass
class Context:
    """策略运行上下文。

    持仓状态、历史 K线窗口（最近 N 根）、参数字典。
    """
    position: Position          # 当前持仓
    bars_window: list[Bar]     # 最近 N 根 K线（含当前 bar）
    params: dict[str, Any]     # 策略参数（从 toml 加载）
    cash: Decimal              # 可用现金
    timestamp: int             # 当前 bar 的 ts
```

### 4.2 Signal / Order / Trade / Position 数据模型

```python
# src/quant/backtest/signal.py
from typing import Literal, Optional
from decimal import Decimal
from datetime import datetime
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Signal:
    """交易信号。与 webhook alert JSON 字段一一对应。

    与 M1 spec §12.1 接口契约一致。
    """
    strategy_id: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    inst_id: str
    qty: Decimal
    ts: int                   # UTC ms，与 M1 一致
    price: Optional[Decimal] = None  # market 单为 None
    signal_id: str = ""       # 由 worker 生成 UUID；空表示回测模式


# src/quant/backtest/order.py
@dataclass(frozen=True, slots=True)
class Order:
    """订单。worker 调用 Executor 创建。"""
    order_id: str
    signal: Signal
    status: Literal["pending", "filled", "canceled", "rejected"]
    created_ts: int


@dataclass(frozen=True, slots=True)
class Fill:
    """成交记录。回测引擎模拟成交产生；M3/M5 由交易所返回。"""
    order_id: str
    inst_id: str
    side: Literal["buy", "sell"]
    qty: Decimal
    price: Decimal
    fee: Decimal
    ts: int


# src/quant/backtest/position.py
@dataclass(slots=True)
class Position:
    """持仓状态。"""
    inst_id: str
    qty: Decimal = Decimal("0")    # 正=多，负=空
    avg_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
```

### 4.3 Backtest 事件驱动主循环

```python
# src/quant/backtest/engine.py
class Backtest:
    """事件驱动回测引擎。

    按时间顺序回放 Bar，每根调 strategy.on_bar，产生 Signal 时调 Matcher 模拟成交。
    """

    def __init__(
        self,
        strategy: Strategy,
        matcher: Matcher,
        initial_cash: Decimal = Decimal("10000"),
        commission_rate: Decimal = Decimal("0.001"),  # 0.1% OKX taker
    ) -> None: ...

    def run(self, bars: list[Bar]) -> BacktestResult:
        """运行回测，返回 BacktestResult。"""
        # 1. 初始化 ctx, position
        # 2. for bar in bars:
        #      ctx.bars_window.append(bar) (保留最近 N 根)
        #      signal = strategy.on_bar(bar, ctx)
        #      if signal: fill = matcher.match(signal, next_bar); ...
        # 3. strategy.on_finish(ctx)
        # 4. return BacktestResult(...)
```

**关键设计**：
- **回放顺序**：`bars` 按时间升序，引擎不重新排序（输入负责）
- **成交时机**：`on_bar` 在 bar 关闭时被调；市价单按**下一根开盘价**成交（`bars[i+1].open`），避免使用未来数据
- **最后一根**：`on_bar` 仍被调，但产生的信号无法成交（无下一根）— 在报告中标 `unfilled_signals`
- **窗口大小**：`ctx.bars_window` 由策略参数 `window_size` 控制，默认 200

### 4.4 成交模拟（Matcher）

```python
# src/quant/backtest/matcher.py
class Matcher:
    """成交模拟器。市价=下一根开盘价；限价=穿透成交；可配滑点。"""

    def __init__(
        self,
        slippage: Decimal = Decimal("0"),     # 滑点 bps，0.0001=1bp
        commission_rate: Decimal = Decimal("0.001"),  # 0.1% taker
    ) -> None: ...

    def match(
        self,
        signal: Signal,
        next_bar: Bar,
    ) -> Fill:
        """根据 signal 产生 Fill。"""
        if signal.order_type == "market":
            price = next_bar.open * (1 + slippage * sign(signal.side))
        elif signal.order_type == "limit":
            # 限价单：下一根 bar 穿透限价即成交
            if signal.side == "buy" and next_bar.low <= signal.price:
                price = signal.price
            elif signal.side == "sell" and next_bar.high >= signal.price:
                price = signal.price
            else:
                raise NoFillError("limit not touched")
        fee = price * signal.qty * commission_rate
        return Fill(...)
```

### 4.5 绩效指标（Metrics）

```python
# src/quant/backtest/metrics.py
@dataclass(frozen=True)
class BacktestResult:
    """回测结果。"""
    trades: list[Fill]
    equity_curve: list[tuple[int, Decimal]]   # [(ts, equity), ...]
    drawdown_curve: list[tuple[int, Decimal]] # [(ts, drawdown %), ...]
    metrics: dict[str, float]


def compute_metrics(
    trades: list[Fill],
    equity_curve: list[tuple[int, Decimal]],
    bars: list[Bar],
) -> dict[str, float]:
    """计算绩效指标。

    返回：{
        "total_return": 0.15,        # 总收益率
        "annual_return": 0.45,       # 年化
        "sharpe": 1.8,
        "max_drawdown": 0.08,        # 最大回撤
        "win_rate": 0.55,            # 胜率
        "profit_factor": 1.6,        # 盈亏比
        "trade_count": 42,
        "avg_holding_bars": 5.3,
    }
    """
```

**计算口径**（与 Pine 对齐）：
- `equity = cash + position.qty * bar.close`，每根 bar 更新
- `sharpe = mean(daily_return) / std(daily_return) * sqrt(365*24)`（加密 7×24）
- `max_drawdown = max(1 - equity / running_max)`
- `win_rate = profitable_trades / total_trades`
- `profit_factor = gross_profit / gross_loss`

### 4.6 报告生成（Report）

```python
# src/quant/backtest/report.py
class ReportGenerator:
    """CSV + HTML 报告生成。"""

    def to_csv(self, result: BacktestResult, path: Path) -> None:
        """交易明细 + equity curve → CSV。"""

    def to_html(self, result: BacktestResult, path: Path) -> None:
        """HTML 报告：收益曲线/回撤曲线/交易明细表。
        用 jinja2 模板 + matplotlib 图表。
        """
```

### 4.7 参数寻优（Optimizer）

```python
# src/quant/backtest/optimizer.py
class GridOptimizer:
    """网格搜索策略参数。

    Args:
        strategy_factory: 接受 params dict 返回 Strategy 实例
        param_grid: {"fast": [5, 10, 20], "slow": [20, 50, 100]}
    """

    def search(
        self,
        bars: list[Bar],
        metric: str = "sharpe",
        n_jobs: int = 1,        # 并行 worker；M2 默认串行
    ) -> list[tuple[dict, dict[str, float]]]:
        """返回排序后的 (params, metrics) 列表。"""

    def _check_future_leak(self, params: dict) -> None:
        """禁止未来函数：参数不允许引用未来时间。"""
```

**约束**：
- 网格穷举（M2 不实现随机搜索/贝叶斯优化，留 M2 后）
- 禁止未来函数：参数只能是常量或依赖历史数据的指标
- 输出按指定 metric 降序

---

## 5. 策略双副本机制

### 5.1 Pine ↔ Python 对齐规则

| 项 | 规则 |
|---|---|
| 指标计算 | `ta.sma(close, n)` ↔ `sum(bars_window[-n:].close) / n`（注意是简单移动平均，不用 EMA） |
| 入场逻辑 | Pine `strategy.entry("long", strategy.long)` ↔ Python `return Signal(side="buy", ...)` |
| 出场逻辑 | Pine `strategy.close("long")` ↔ Python `return Signal(side="sell", ...)` |
| 参数 | Pine `input(14, "RSI Period")` ↔ Python `params["rsi_period"] = 14`（同名同义） |
| 时间戳 | Pine `time` ↔ Python `bar.ts`，都是 UTC ms |
| 数量 | Pine `strategy.entry` 默认 `qty=strategy.equity/close` ↔ Python 默认 `qty=cash/close` |

### 5.2 双回测偏离检测

```python
# tests/strategies/test_sma_cross_align.py
def test_pine_python_deviation():
    """跑同一份 K线，Pine 与 Python 回测结果偏离 < 5%。"""
    # 用户提供 Pine 回测的 trades.csv（从 TV 导出）
    pine_trades = load_pine_trades("tests/fixtures/sma_cross_pine_trades.csv")
    py_result = run_python_backtest(SmaCrossStrategy(), bars)
    # 对齐时间戳后比较总收益、夏普、最大回撤
    deviation = compute_deviation(pine_trades, py_result)
    assert deviation < 0.05
```

### 5.3 SMA Cross 策略

**Pine 版本**（`strategies/pine/sma_cross.pine`）：

```pine
//@version=5
strategy("SMA Cross", overlay=true)
fast_len = input(10, "Fast SMA")
slow_len = input(50, "Slow SMA")
fast = ta.sma(close, fast_len)
slow = ta.sma(close, slow_len)
if ta.crossover(fast, slow)
    strategy.entry("long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close("long")
```

**Python 副本**（`strategies/python/sma_cross.py`）：

```python
class SmaCrossStrategy(Strategy):
    @property
    def strategy_id(self) -> str:
        return "sma_cross"

    def on_init(self, ctx: Context) -> None:
        self.fast_len = ctx.params.get("fast_len", 10)
        self.slow_len = ctx.params.get("slow_len", 50)

    def on_bar(self, bar: Bar, ctx: Context) -> Optional[Signal]:
        if len(ctx.bars_window) < self.slow_len:
            return None
        closes = [b.close for b in ctx.bars_window[-self.slow_len:]]
        fast = sum(closes[-self.fast_len:]) / self.fast_len
        slow = sum(closes) / self.slow_len
        # 前一根的 fast/slow
        prev_closes = [b.close for b in ctx.bars_window[-self.slow_len-1:-1]]
        prev_fast = sum(prev_closes[-self.fast_len:]) / self.fast_len
        prev_slow = sum(prev_closes) / self.slow_len
        if prev_fast <= prev_slow and fast > slow:
            return Signal(strategy_id="sma_cross", side="buy",
                          order_type="market", inst_id=bar.inst_id,
                          qty=ctx.cash / bar.close, ts=bar.ts)
        if prev_fast >= prev_slow and fast < slow:
            return Signal(strategy_id="sma_cross", side="sell",
                          order_type="market", inst_id=bar.inst_id,
                          qty=ctx.position.qty, ts=bar.ts)
        return None
```

### 5.4 RSI 策略

**Pine 版本**（`strategies/pine/rsi.pine`）：

```pine
//@version=5
strategy("RSI", overlay=false)
period = input(14, "RSI Period")
oversold = input(30, "Oversold")
overbought = input(70, "Overbought")
r = ta.rsi(close, period)
if r < oversold
    strategy.entry("long", strategy.long)
if r > overbought
    strategy.close("long")
```

**Python 副本**：用 Wilder 平滑 RSI（与 Pine `ta.rsi` 对齐）。

---

## 6. Webhook 服务设计

### 6.1 端点

```python
# src/quant/webhook/server.py
from fastapi import FastAPI, Request, HTTPException

class WebhookServer:
    """FastAPI 应用工厂。"""

    def __init__(
        self,
        queue: SignalQueue,
        deduper: Deduper,
        secret: str,           # TV alert signing secret
    ) -> None: ...

    def create_app(self) -> FastAPI:
        app = FastAPI(title="quant-trading webhook")

        @app.post("/webhook/tv")
        async def receive_alert(req: Request) -> dict:
            # 1. 验签 HMAC-SHA256
            sig = req.headers.get("X-Signature", "")
            body = await req.body()
            if not self._verify(body, sig):
                raise HTTPException(401, "invalid signature")
            # 2. 解析 JSON → Signal
            try:
                signal = self._parser.parse(body.decode())
            except ParseError as e:
                # 解析失败入死信队列
                await self._queue.push_dead_raw(body.decode(), reason=str(e))
                raise HTTPException(400, f"parse error: {e}")
            # 3. 去重
            if self._deduper.is_duplicate(signal):
                return {"status": "duplicate", "signal_id": signal.signal_id}
            # 4. 入队
            await self._queue.push(signal)
            return {"status": "ok", "signal_id": signal.signal_id}

        @app.get("/health")
        async def health() -> dict:
            return {"status": "ok"}

        return app
```

**契约**（PRD F3.2）：
- 5 秒内 ACK（FastAPI 默认满足）
- 鉴权失败 → HTTP 401
- 解析失败 → 入死信队列 + HTTP 400
- 重复信号 → HTTP 200 + `{"status": "duplicate"}`

### 6.2 Alert JSON 格式

TV alert 配置的 message 字段（JSON 字符串）：

```json
{
  "strategy_id": "sma_cross",
  "side": "buy",
  "order_type": "market",
  "inst_id": "BTC-USDT",
  "qty": "0.001",
  "price": null
}
```

`parser.py` 负责解析 + 字段校验：

```python
# src/quant/webhook/parser.py
class AlertParser:
    def parse(self, body: str) -> Signal:
        data = json.loads(body)
        # 校验必填字段
        for k in ("strategy_id", "side", "order_type", "inst_id", "qty"):
            if k not in data:
                raise ParseError(f"missing field: {k}")
        # 类型转换
        return Signal(
            strategy_id=data["strategy_id"],
            side=data["side"],
            order_type=data["order_type"],
            inst_id=data["inst_id"],
            qty=Decimal(str(data["qty"])),
            price=Decimal(str(data["price"])) if data.get("price") else None,
            ts=int(time.time() * 1000),
            signal_id=str(uuid.uuid4()),
        )
```

### 6.3 HMAC 鉴权

TV webhook 可配置自定义 header `X-Signature`（用 webhook signing secret 对 body 做 HMAC-SHA256）：

```python
# src/quant/webhook/auth.py
import hmac, hashlib

def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

> M2 阶段 secret 可空（TV 免费版不支持签名），由 config 控制。

---

## 7. 信号队列设计

```python
# src/quant/queue/signal_queue.py
import json
from redis import Redis

class SignalQueue:
    """Redis LIST 队列封装。

    三个队列：
    - pending: 待处理（LPUSH/BRPOP）
    - inflight: 处理中（worker 取出后存入；崩溃可恢复）
    - dead: 死信（处理失败或解析失败）

    所有 Signal 序列化为 JSON 存储。
    """

    PENDING_KEY = "signals:pending"
    INFLIGHT_KEY = "signals:inflight"
    DEAD_KEY = "signals:dead"

    def __init__(self, redis: Redis) -> None: ...

    async def push(self, signal: Signal) -> None:
        """LPUSH 到 pending。"""

    async def pop(self, timeout: int = 0) -> Signal | None:
        """BRPOP 从 pending 取出，原子地 push 到 inflight，返回 Signal。"""

    async def remove_inflight(self, signal_id: str) -> None:
        """处理成功后从 inflight 移除。"""

    async def push_dead(self, signal: Signal, reason: str) -> None:
        """处理失败 → 死信队列。存 JSON + reason。"""

    async def push_dead_raw(self, body: str, reason: str) -> None:
        """解析失败的原始 body → 死信队列。"""

    async def recover_inflight(self) -> list[Signal]:
        """进程启动时调用：把 inflight 全部回 pending 重新处理。"""
```

**关键设计**：
- BRPOP 阻塞 0 表示无限等待；M2 用 1s 超时方便心跳检查
- inflight 不设 TTL（崩溃恢复时由 worker 主动 `recover_inflight`）
- dead 队列人工干预，不自动重试

---

## 8. 去重设计

```python
# src/quant/queue/dedup.py
class Deduper:
    """Redis SET + hash + TTL 5min。

    hash = sha256(signal_json_canonical)
    """

    TTL_SECONDS = 300  # 5 分钟

    def __init__(self, redis: Redis) -> None: ...

    def is_duplicate(self, signal: Signal) -> bool:
        """sadd 返回 1 表示新信号（非重复），0 表示重复。"""
        h = self._hash(signal)
        result = self._redis.sadd("signals:dedup", h)
        if result == 1:
            self._redis.expire("signals:dedup", self.TTL_SECONDS)
            return False
        return True
```

**注意**：M2 阶段 `is_duplicate` 在 webhook 进程内调用，与 worker 解耦。

---

## 9. Worker 设计

```python
# src/quant/execution/worker.py
import asyncio, signal
from quant.queue.signal_queue import SignalQueue
from quant.risk.checker import RiskChecker
from quant.state.sqlite_store import SQLiteStore
from quant.execution.executor import Executor

class Worker:
    """主循环：BRPOP → 风控 → 下单 → 持久化。

    每分钟写心跳到 Redis（key: heartbeat:worker）。
    """

    HEARTBEAT_INTERVAL = 60

    def __init__(
        self,
        queue: SignalQueue,
        risk: RiskChecker,
        executor: Executor,
        store: SQLiteStore,
        redis: Redis,
    ) -> None: ...

    async def run(self) -> None:
        """主循环。SIGTERM/SIGINT 触发优雅退出。"""
        await self._recover_inflight()
        asyncio.create_task(self._heartbeat_loop())
        while self._running:
            signal = await self._queue.pop(timeout=1)
            if signal is None:
                continue
            try:
                await self._process(signal)
            except Exception as e:
                _log.exception(f"process failed: {signal.signal_id}")
                await self._queue.push_dead(signal, reason=str(e))

    async def _process(self, signal: Signal) -> None:
        # 1. 风控
        decision = self._risk.check(signal)
        if decision.rejected:
            await self._store.save_signal(signal, status="rejected")
            return
        # 2. 下单
        order = await self._executor.execute(signal, decision.adjusted_qty)
        # 3. 持久化
        await self._store.save_signal(signal, status="executed")
        await self._store.save_order(order)
        # 4. 从 inflight 移除
        await self._queue.remove_inflight(signal.signal_id)

    async def _heartbeat_loop(self) -> None:
        while self._running:
            self._redis.set("heartbeat:worker", int(time.time()), ex=120)
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)
```

**M2 Executor 为 mock**：

```python
# src/quant/execution/executor.py
class Executor:
    """M2 阶段：mock，只返回 Order 对象，不真实下单。
    M3 阶段：PaperBroker 接 OKX 模拟盘；M5：LiveBroker 接实盘。"""

    async def execute(self, signal: Signal, qty: Decimal) -> Order:
        _log.info(f"[MOCK] execute signal={signal.signal_id}")
        return Order(
            order_id=str(uuid.uuid4()),
            signal=signal,
            status="filled",
            created_ts=int(time.time() * 1000),
        )
```

---

## 10. 风控设计

```python
# src/quant/risk/config.py
from pydantic import BaseModel

class RiskConfig(BaseModel):
    """单策略风控参数。从 config/strategies/{strategy_id}.toml 加载。"""
    strategy_id: str
    max_position_pct: Decimal = Decimal("0.3")    # 单品种最大仓位 30%
    max_total_pct: Decimal = Decimal("0.8")        # 总仓位上限 80%
    max_order_qty: Decimal = Decimal("10000")     # 单笔最大下单量（USDT）
    daily_loss_limit: Decimal = Decimal("0.05")   # 日亏损上限 5%
    consecutive_loss_limit: int = 3               # 连续亏损保护


# src/quant/risk/checker.py
@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    adjusted_qty: Decimal          # 可能因仓位上限被下调
    reason: str = ""


class RiskChecker:
    def __init__(
        self,
        config: RiskConfig,
        store: SQLiteStore,    # 查历史信号/持仓/盈亏
    ) -> None: ...

    def check(self, signal: Signal) -> RiskDecision:
        """检查信号。任一规则不通过 → approved=False。"""
        # 1. 单品种持仓上限
        # 2. 总仓位上限
        # 3. 单笔下单量
        # 4. 日亏损（从 SQLite 查今日累计亏损）
        # 5. 连续亏损（从 SQLite 查最近 N 笔交易盈亏）
```

**M2 范围**：仅实现规则骨架，不接 OKX 模拟盘账户数据（M3 接入）。

---

## 11. 状态持久化设计

### 11.1 SQLite schema

```sql
-- 数据库：data/state/quant.db
CREATE TABLE signals (
    signal_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    inst_id TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT,
    ts INTEGER NOT NULL,
    status TEXT NOT NULL,        -- pending/executed/rejected/dead
    created_at INTEGER NOT NULL
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    signal_id TEXT REFERENCES signals(signal_id),
    status TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    filled_ts INTEGER,
    filled_price TEXT,
    filled_qty TEXT,
    fee TEXT
);

CREATE TABLE positions (
    inst_id TEXT PRIMARY KEY,
    qty TEXT NOT NULL,
    avg_price TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE heartbeat (
    process TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);
```

### 11.2 SQLiteStore 接口

```python
# src/quant/state/sqlite_store.py
class SQLiteStore:
    def __init__(self, db_path: Path) -> None: ...

    def save_signal(self, signal: Signal, status: str) -> None: ...
    def get_signal(self, signal_id: str) -> dict | None: ...

    def save_order(self, order: Order) -> None: ...
    def update_order_fill(self, order_id: str, fill: Fill) -> None: ...

    def get_position(self, inst_id: str) -> Position: ...
    def update_position(self, position: Position) -> None: ...

    def get_today_realized_pnl(self) -> Decimal: ...
    def get_recent_trades(self, n: int) -> list[Fill]: ...

    def save_heartbeat(self, process: str, ts: int) -> None: ...
    def get_heartbeat(self, process: str) -> int | None: ...
```

### 11.3 崩溃恢复

Worker 启动时调 `queue.recover_inflight()`，把 inflight 队列的信号回 pending 重新处理。SQLite 状态视为权威：worker 处理前先查 SQLite 看是否已 executed，避免重复下单。

---

## 12. 配置扩展

`config/config.toml` 新增段落：

```toml
[redis]
url = "redis://localhost:6379/0"
key_prefix = "quant"

[webhook]
host = "127.0.0.1"
port = 8000
secret = "${TV_WEBHOOK_SECRET:-}"    # TV 免费版可空

[backtest]
initial_cash = 10000
commission_rate = 0.001              # 0.1% OKX taker
slippage_bps = 5                      # 5 bp
window_size = 200                     # 默认 K线窗口

[state]
db_path = "./data/state/quant.db"
```

`config/strategies/sma_cross.toml`：

```toml
[strategy]
strategy_id = "sma_cross"
class = "quant.strategies.python.sma_cross.SmaCrossStrategy"

[params]
fast_len = 10
slow_len = 50

[risk]
max_position_pct = 0.3
max_total_pct = 0.8
max_order_qty = 10000
daily_loss_limit = 0.05
consecutive_loss_limit = 3
```

---

## 13. 错误处理与重试策略

延续 M1 §9 异常体系。M2 新增：

| 异常 | 触发场景 | 处理 |
|---|---|---|
| `ParseError` | webhook alert JSON 解析失败 | 入死信队列 + HTTP 400 |
| `NoFillError` | 回测限价单未穿透 | 该信号标记 unfilled，不重试 |
| `RiskRejectError` | 风控校验失败 | 信号入 SQLite status=rejected |
| `QueueError` | Redis 不可达 | worker 退避重试 3 次后退出 |
| `StateError` | SQLite 写入失败 | 重试 3 次后告警 |

worker 主循环异常**不上抛**，捕获后入死信队列继续循环，保证持续运行。

---

## 14. 测试策略

延续 M1 §10 测试金字塔。

### 14.1 单元测试（目标 ≥ 85% 覆盖）

| 模块 | 关键测试点 |
|---|---|
| `backtest/strategy.py` | `Strategy` ABC 强制实现校验 |
| `backtest/engine.py` | 事件循环顺序、最后一根 unfilled、position 跟踪 |
| `backtest/matcher.py` | 市价/限价成交、滑点计算、NoFillError |
| `backtest/metrics.py` | 各指标数学正确（用已知数据验证） |
| `backtest/report.py` | CSV/HTML 文件生成 |
| `backtest/optimizer.py` | 网格穷举、未来函数检测 |
| `webhook/parser.py` | 缺字段/类型错/边界值 |
| `webhook/auth.py` | HMAC 正确/错误签名 |
| `queue/signal_queue.py` | push/pop/inflight/dead/recover（fakeredis） |
| `queue/dedup.py` | 重复检测、TTL |
| `risk/checker.py` | 各规则触发 |
| `state/sqlite_store.py` | CRUD、崩溃恢复 |

### 14.2 集成测试

| 场景 | 验证 |
|---|---|
| 端到端 webhook → queue → worker | 用 fakeredis + TestClient，POST 一个 alert，验证 worker 消费并写 SQLite |
| SMA Cross 回测 + Pine 对齐 | 用真实 BTC-USDT 1H 数据跑回测，与 Pine 导出的 trades.csv 偏离 < 5% |
| 参数寻优 | 给定 param_grid，验证输出按指定 metric 排序 |

### 14.3 烟测（真实环境）

| 场景 | 验证 |
|---|---|
| 真实 Redis 队列 | `redis-server` + worker 进程，POST 一个 alert，验证端到端 |
| 真实 SQLite 持久化 | worker 崩溃后重启，inflight 队列恢复 |
| 真实 K线回测 | 拉 1 个月 BTC-USDT 1H，跑 SMA Cross，输出 HTML 报告 |

---

## 15. 实现顺序

建议按以下顺序实施，每步可独立测试：

1. **Strategy + 数据模型**（M2-2）：`Strategy` ABC + `Signal`/`Order`/`Fill`/`Position`
2. **Backtest 引擎 + Matcher**（M2-3）：事件循环 + 成交模拟
3. **绩效指标 + 报告**（M2-4/5）：metrics + HTML 报告
4. **SMA Cross 样例策略**（M2-7）：Python + Pine 双副本
5. **RSI 样例策略**（M2-8）
6. **参数寻优**（M2-6）：网格搜索
7. **Webhook server + Parser + Auth**（M2-9）：FastAPI
8. **SignalQueue + Deduper**（M2-10/11）：Redis 队列
9. **SQLiteStore**（M2-14）：状态持久化
10. **RiskChecker**（M2-13）：风控规则
11. **Worker + Executor mock**（M2-12）：主循环
12. **全量测试 + 报告**（M2-15）

> 每步写完代码必须做真实测试、有测试场景跑通再继续下一步；修复 bug 后必须复测防止回归（延续 M1 节奏）。

---

## 16. 验收标准映射

| PRD F编号 | 验收标准 | spec 对应 | 测试用例 |
|---|---|---|---|
| F2.1 | `Strategy` ABC 强制实现；Pine/Python 双副本对齐 < 5% 偏离 | §4.1, §5 | test_strategy.py, test_sma_cross_align.py |
| F2.2 | 事件驱动回放；市价/限价/滑点可配 | §4.3, §4.4 | test_engine.py, test_matcher.py |
| F2.3 | 市价=下一根开盘；限价=穿透；滑点 | §4.4 | test_matcher.py |
| F2.4 | 7 个指标全输出 | §4.5 | test_metrics.py |
| F2.5 | HTML/CSV 报告生成 | §4.6 | test_report.py |
| F2.6 | 网格搜索；禁止未来函数 | §4.7 | test_optimizer.py |
| F3.2 | FastAPI server 5s ACK；HMAC 鉴权；死信队列 | §6 | test_webhook_server.py |
| F3.3 | Redis LIST 队列；BRPOP；inflight 恢复；心跳 | §7, §9 | test_queue.py, test_worker.py |
| F3.4 | 5 条风控规则全实现 | §10 | test_risk.py |
| F3.7 | SQLite 表结构正确；崩溃恢复 | §11 | test_sqlite_store.py |

---

## 17. 风险与开放问题

### 17.1 已识别风险

| 风险 | 等级 | 应对 |
|---|---|---|
| Pine 与 Python 回测偏离 > 5% | 高 | M2 必须在样例策略上跑通双回测对齐测试；偏离 > 5% 禁止推进 M3 |
| Redis 队列丢失（崩溃） | 中 | inflight 队列 + SQLite 双保险；worker 启动 recover |
| TV alert JSON 字段不稳定 | 中 | parser 严格校验 + 缺字段入死信；不静默容忍 |
| 回测未来函数 | 高 | `on_bar` 只能访问 `ctx.bars_window`；optimizer 显式禁止参数引用未来 |
| HMAC 鉴权 TV 免费版不支持 | 低 | secret 可空，配置开关；M5 实盘强制开启 |

### 17.2 开放问题

1. **Pine 回测 trades.csv 来源**：M2 实施前用户需提供至少一个策略的 TV 导出 trades.csv，用于双回测对齐
2. **Redis 部署形态**：本地用 `brew install redis` 即可；VPS 部署留 M6
3. **Worker 并行度**：M2 单进程；多进程方案留 M5 实盘前评估
4. **策略参数 toml 加载**：M2 用 `dynaconf` 或 `tomllib` 手动加载，留实施时决定

### 17.3 实现状态对账（2026-10-30 复核）

本节记录 MS2 交付后设计与代码的实际差异，供后续维护者对照，**不回改正文历史章节**。

**已按设计实现**

`Strategy` 基类（`on_init`/`on_bar`/`on_trade`/`protective_exit`/`on_finish`）、事件驱动回测、撮合口径「本根收盘出信号 → 下一根开盘成交」、绩效指标、CSV/HTML 报告、网格寻优、FastAPI webhook、Redis 队列（LPUSH/BRPOP + inflight + dead）、信号去重、worker 主循环（BRPOP → 风控 → 下单 → 持久化 + 心跳）、5 条风控规则、SQLite 状态表、双副本策略。

**与设计的偏差**

| 项 | 设计 | 实际 | 影响 |
|---|---|---|---|
| 风控规则顺序 | 日亏损 → 连亏 → 熔断 → 单笔 → 单品种 → 总仓位 | `RiskChecker.check` 内为 日亏损 → 连亏 → 单笔名义 → 单品种 → 总仓位；**熔断不在 checker 内**，是独立开关（`kill_switch`） | 语义等价，但读代码时容易误以为熔断缺失；PRD M7 的「规则顺序」应理解为「跨组件的求值顺序」 |
| `max_order_qty` 语义 | 注释写「单笔最大下单量（USDT）」 | 实际是**单笔最大名义价值（USDT）**，超限时按 `max_qty = max_order_qty / price` 下调数量 | 字段名沿用历史未改；PRD M7 已加注明 |
| kill switch | 未设计 | 已实现：CLI `quant kill-switch`、看板按钮（仅写标志）、monitor `kill_switch` 告警 | **看板按钮不撤单**，撤单只在 CLI `--on` 分支；PRD M7-4 已标 🟡 并列入 MS4 补齐 |
| 订单对账 `Reconciler` | 未设计 | 已实现：worker 启动 + 运行期定时对账，挂单超 `order_timeout_seconds` 批量撤单 | 属 M8 增量；「三项对账」（订单/持仓/资金）仍归 MS5（M8-17/M8-18） |
| 服务端仓位计算 | 未设计 | 未实现，`Signal.qty` 与 webhook `qty` 仍为必填 | PRD M8-14 已标 ⬜，归 MS4 |
| walk-forward | 未设计 | 已实现 `backtest/walkforward.py` + `quant walk-forward` | 见 MS4-C 详设 |

**开放问题裁决**

| 问题 | 裁决 |
|---|---|
| Pine 回测 trades.csv 来源 | 未获取；`PineBaseline` 改为登记已有回测结果做对比，不依赖 TV 导出（见 MS4-C） |
| Redis 部署形态 | 本地 brew 安装；VPS 留 MS6 |
| Worker 并行度 | 仍单进程；多绑定改造见 MS4-C |
| 策略参数 toml 加载 | 用 `tomllib` 手动加载，未引入 dynaconf |

---

## 18. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-17 | 初版。涵盖回测引擎（§4-5）+ webhook/queue/worker/risk/state 框架（§6-11） |
| v1.1 | 2026-10-30 | 里程碑编号 M2 → **MS2**，文件更名 `m2-*` → `ms2-*`；新增 §17.3 实现状态对账（6 处偏差、4 项开放问题裁决）；状态改为「已实现」 |
