# MS4-C 回测落库与策略参数启停 详细设计文档

> 文档版本：v1.0
> 创建日期：2026-10-30
> 状态：待实施
> 上游需求：[PRD v0.6](../requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md) §4.2（M2 市场监控 M2-15）、§4.3（M3 策略研究）、§4.4（M4 回测分析）、§8.1（MS4 范围）
> 上游设计：[MS2 回测 + Webhook v1.1](2026-10-17-ms2-backtest-webhook-design.md) §4-5、[MS1 数据底座 v1.2](2026-10-17-ms1-data-foundation-design.md) §6.5
> 相邻切片：[MS4-A 告警与首页](2026-10-30-ms4a-alerts-overview-design.md)（`AlertService`）、[MS4-B 风控与执行](2026-10-30-ms4b-risk-execution-design.md)（`risk_events` 风格）
> 覆盖条目：M2-15、M3-9 ~ M3-12、M4-6、M4-9 ~ M4-14

---

## 目录

1. [引言](#1-引言)
2. [现状与缺口](#2-现状与缺口)
3. [策略参数体系](#3-策略参数体系)
4. [启停矩阵与引擎改造](#4-启停矩阵与引擎改造)
5. [回测任务与结果落库](#5-回测任务与结果落库)
6. [寻优并发与进度](#6-寻优并发与进度)
7. [扩展指标](#7-扩展指标)
8. [Walk-forward 落库](#8-walk-forward-落库)
9. [数据质量报告](#9-数据质量报告)
10. [存储设计](#10-存储设计)
11. [API 契约](#11-api-契约)
12. [配置扩展](#12-配置扩展)
13. [迁移与兼容](#13-迁移与兼容)
14. [错误处理与降级](#14-错误处理与降级)
15. [测试策略](#15-测试策略)
16. [实现顺序](#16-实现顺序)
17. [验收标准映射](#17-验收标准映射)
18. [风险与开放问题](#18-风险与开放问题)
19. [变更记录](#19-变更记录)

---

## 1. 引言

### 1.1 目的

MS4-C 有三个独立但相互依赖的目标：

1. **参数有一个真相源**。当前回测用请求里的 `params` 字符串、实盘用 toml，两条路径各读各的，没有「回测用 A、实盘用 B」的自动拦截。引入 `PARAM_SPEC` + DB 覆盖后，两条路径共用一个 `resolve_params`。
2. **回测结果不再是一次性**。当前看板回测**算完即扔**（前端存 localStorage，上限 20 条），无法对比、无法回溯、无法复现。
3. **引擎从单组合变成多组合**。「策略 × 品种」启停矩阵是 M3-4/M3-11/M3-12 的共同前置，而引擎现在是 `[engine] strategy` + `[engine] inst` 的单一配置。

### 1.2 范围

- **包含**：`PARAM_SPEC`、参数两层合并与变更历史、启停矩阵与引擎多绑定、回测任务队列与落库、寻优并发与进度、7 个扩展指标、walk-forward 落库、数据质量报告
- **不包含**：批量回测（M4-7，MS5）、资金费率成本（M4-15）、Pine 结果导入对比（M4-16）、策略详情页（M3-15）、参数回滚（M3-14）、双副本一致性校验（M3-13）

---

## 2. 现状与缺口

| 现状 | 位置 | 缺口 |
|---|---|---|
| 策略类无参数元信息，参数靠 `__init__` 默认值 + toml `[params]` | [loader.py](../../src/quant/strategies/loader.py) | 无法渲染表单、无法校验范围、无法判断「能不能在线改」 |
| `GET /api/strategies` 只返回 `{id, params}` | [backtest.py](../../src/quant/dashboard/backtest.py) `_scan_strategies` | 无类型、无范围、无默认值来源 |
| 回测参数从 HTTP 请求的 `params` 字符串解析 | `dashboard/backtest.py` `parse_params` | 与实盘 toml 无关联，可能「回测用 A、实盘用 B」 |
| 引擎单进程单策略单品种 | [config.py](../../src/quant/foundation/config.py) `EngineSettings` | 无绑定表、无 `strategy × inst` 矩阵 |
| 无任何启停键 | 实测无 `quant:strategy:enabled` | 启停需重启进程 |
| 回测算完即扔，不落盘 | `dashboard/backtest.py` 文件头注释明确「不写 CSV/HTML、不写 SQLite」 | 无历史、无对比、无复现 |
| 回测在请求内同步跑，`Semaphore(1)` 串行 | 同上 | 长回测会阻塞请求；无进度、无取消 |
| `MAX_COMBOS=500` 只在看板路径 | `dashboard/backtest.py` | CLI `quant optimize` 无上限 |
| 寻优 `n_jobs` 未真正并行 | [optimizer.py](../../src/quant/backtest/optimizer.py) | 单线程；无进度 |
| 指标只有 8 项 | [metrics.py](../../src/quant/backtest/metrics.py) | 缺 Calmar/Sortino/月度表/连亏/暴露度 |
| `ValidationReport` 只在内存中，`summary()` 后即丢 | [validator.py](../../src/quant/storage/validator.py) | 无落盘、无「已修复/未修复」区分、无 API |

---

## 3. 策略参数体系

### 3.1 `PARAM_SPEC`（M3-9）

```python
# src/quant/backtest/strategy.py

@dataclass(frozen=True, slots=True)
class ParamSpec:
    type: str                       # int / float / decimal / bool / str / choice
    default: Any
    min: float | Decimal | None = None
    max: float | Decimal | None = None
    choices: tuple[str, ...] | None = None
    description: str = ""
    unit: str = ""                  # 根 / % / USDT

class Strategy(ABC):
    PARAM_SPEC: ClassVar[dict[str, ParamSpec]] = {}
```

示例（`sma_cross`）：

```python
PARAM_SPEC = {
    "fast": ParamSpec("int", 10, min=2, max=200, description="快线周期", unit="根"),
    "slow": ParamSpec("int", 100, min=3, max=500, description="慢线周期", unit="根"),
    "ema_filter": ParamSpec("bool", True, description="启用 EMA 趋势过滤"),
    "ema_len": ParamSpec("int", 200, min=20, max=500, unit="根"),
    "ema_band_atr": ParamSpec("float", 0.0, min=0.0, max=5.0, description="ATR 带宽倍数"),
    "atr_len": ParamSpec("int", 14, min=2, max=100, unit="根"),
}
```

**约束**：

- `PARAM_SPEC` 为空或缺失 → 该策略**不可在线编辑**（只读），但回测仍可用 toml 值。
- 交叉校验（如 `fast < slow`）不在 `ParamSpec` 里表达，由策略类可选实现 `validate_params(params) -> list[str]`。这是刻意的：把任意逻辑塞进声明式 spec 会变成半个表达式引擎。

### 3.2 两层合并（M3-10）

```
toml 默认（config/strategies/{id}.toml [params]）
        ↓ 被覆盖
DB 覆盖（strategy_params 表）
```

```python
def resolve_params(
    strategy_id: str, config_dir: Path, store: SQLiteStore
) -> tuple[dict[str, Any], dict[str, str]]:
    """返回 (合并后参数, 每个字段的来源)。"""
    _, base = load_strategy_class(strategy_id, config_dir)
    params = dict(base)
    sources = {k: "toml" for k in base}
    for row in store.get_strategy_params(strategy_id):
        # 按 PARAM_SPEC 转换类型，非法值跳过并告警
        params[row["param"]] = _coerce(row)
        sources[row["param"]] = "db"
    return params, sources
```

**回测与实盘必须共用这一个函数**：

| 场景 | 现在 | 之后 |
|---|---|---|
| 实盘 engine | `load_strategy(...)` 读 toml | `resolve_params(...)` |
| 回测（未显式传参） | 请求 `params` 为空 → toml | `resolve_params(...)` |
| 回测（显式传参） | 请求 `params` 字符串 | 请求值优先，`sources` 全标 `request` |

显式传参优先是为了兼容「回测时扫一遍参数找最优」的既有用法。但**保存为覆盖**是另一个动作（§3.3），不会因为跑了一次带参回测就改变实盘。

### 3.3 变更与审计（M3-8，M3-14 的前置）

每次保存覆盖都写两条：`strategy_params`（当前值）+ `strategy_param_changes`（历史）。历史行记录 `git_commit`（`git rev-parse --short HEAD`，取不到则留空），这样「3 周前把 slow 从 100 改成 150」能对应到当时的代码版本。

---

## 4. 启停矩阵与引擎改造

### 4.1 绑定表（M3-11）

```sql
CREATE TABLE IF NOT EXISTS strategy_bindings (
    strategy_id TEXT NOT NULL,
    inst_id     TEXT NOT NULL,
    bar         TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (strategy_id, inst_id)
);
```

**主键只有 `(strategy_id, inst_id)`，`bar` 不在主键里** —— 这是 M3-11 的硬要求：同一策略同一品种只允许一个周期，否则两个周期会对同一持仓重复下单。

插入时若已存在不同 `bar`，**拒绝**并提示「该组合已绑定 1H，请先解绑」。

### 4.2 引擎改造

现状：

```toml
[engine]
strategy = "sma_cross"
inst = "BTC-USDT-SWAP"
bar = "1H"
```

之后：

```
每轮循环：
  bindings = read_bindings()          # 从 Redis 缓存
  for b in bindings where b.enabled:
      strat = self._strategies[b.strategy_id]   # 每组合一个实例
      strat.on_bar(bar, ctx_for(b))
```

四个必须处理的隔离问题：

| 问题 | 方案 |
|---|---|
| **策略实例状态** | 每个绑定一个独立实例（`Strategy()` 不可共享，策略可能持有内部状态如「上次信号方向」） |
| **`ctx` 隔离** | 每绑定一个 `Context`；`bars_window` 按该绑定的 `bar` 与 `window_size` 独立裁剪 |
| **Redis 键** | 现有 `last_bar_key`/`status_key` 已带 `:{strategy}:{inst}` 后缀，无需改 |
| **风控参数** | 按 `strategy_id` 解析（同一策略的不同品种共享风控），符合 M7-2「参数按策略取值」 |

`Context` 是 `slots=True` 的 dataclass，若 MS4 后续要加 `factors` 字段（M5），**必须同步改这里与回测引擎两处构造**（已在 PRD M5 前置条件中标注）。

### 4.3 启停即时生效（M3-12）

```
看板 PUT /api/strategies/{id}/bindings/{inst}  {enabled: false}
      ├─ 1. 写 SQLite strategy_bindings
      ├─ 2. 重建 Redis hash quant:strategy:bindings
      └─ 3. 返回

engine 每轮（默认 60s）：
      └─ HGETALL quant:strategy:bindings → 过滤 enabled=1
```

- 不重启进程，最多延迟一个轮询周期。
- **停止后已有持仓默认保留**。理由：自动平仓是高风险动作，误点一次开关就清仓不可接受。平仓是显式操作（执行页 M8-13）。页面上明确提示「已停止新开仓，当前持仓保留」。
- Redis hash 丢失时 → 回落读 SQLite 并重建缓存。**不能因为 Redis 挂了就认为「全部禁用」**（那会导致静默停止交易），也不能认为「全部启用」（违反用户意图）。配置 `bindings_fail_mode = "db"`（默认）/ `"halt"`。

---

## 5. 回测任务与结果落库

### 5.1 任务模型

```python
@dataclass(frozen=True, slots=True)
class BacktestRun:
    run_id: str                 # {yyyymmdd}-{strategy}-{inst}-{seq}
    strategy_id: str
    inst_id: str
    bar: str
    start_ts: int
    end_ts: int
    params: dict[str, Any]
    kind: str                   # backtest / optimize / walkforward
    status: str                 # pending / running / done / failed / canceled
    created_at: int
    started_at: int | None
    finished_at: int | None
    error: str | None
    git_commit: str | None
```

### 5.2 `TaskRunner`

```python
class TaskRunner:
    """回测任务队列：单消费者、可取消、结果落盘。"""

    def __init__(self, store, settings, workers: int = 1) -> None: ...

    async def submit(self, req: BacktestRequest) -> str:
        """登记 pending 并唤醒消费者，立即返回 run_id。"""

    async def status(self, run_id: str) -> dict: ...

    async def cancel(self, run_id: str) -> str:
        """pending → 直接 canceled；running → 置取消标志，下一根 bar 退出。"""
```

关键决策：

| 决策 | 理由 |
|---|---|
| **单消费者（`workers=1`）** | 回测是 CPU 密集，并发只会互相拖慢；用 `ProcessPool` 做并行的是寻优内部的组合，不是任务之间 |
| **取消粒度是「一根 bar」** | 回测循环里检查 Redis 标志 `quant:backtest:cancel:{run_id}`，每根 bar 检查一次的成本可忽略（< 1µs vs 回测本身的 µs 级） |
| **状态写 SQLite，进度写 Redis** | 状态是权威（重启可恢复），进度是瞬时（`quant:backtest:progress:{run_id}`，TTL 300s） |
| **重启恢复** | 启动时把 `running` 的任务标记为 `failed`（`error="interrupted"`），不自动重跑——重跑可能产生重复产物 |

### 5.3 产物

```
data/backtests/{run_id}/
├── equity.parquet        # [ts, equity, drawdown]
├── trades.parquet        # 成交明细
├── segments.parquet      # 仅 walkforward：逐段 IS/OOS
└── report.html           # 复用现有 report.py 生成
```

`backtest_artifacts` 登记 `kind → path`，API 只返回**元数据**（kind/大小/生成时间），大文件通过单独端点下载。

### 5.4 历史与对比（M4-10/M4-11）

- 列表：按 `strategy_id/inst_id/bar/时间区间/status/kind` 筛选，服务端分页。
- 重跑：`POST /api/backtest/{run_id}/rerun` → 读旧 run 的 params 与区间，**生成新 `run_id`**，不覆盖旧结果。
- 对比：`GET /api/backtest/compare?ids=a,b,c`，**服务端校验三者 `inst_id` 与 `bar` 一致**，不一致返回 400 并指出哪两个不匹配。

> 前端已有的 localStorage 历史保留一个版本作为降级路径（服务端不可用时仍能看到最近 20 条），但**不再是主存储**。

---

## 6. 寻优并发与进度

### 6.1 进程池

```python
class GridOptimizer:
    def run(self, ..., n_jobs: int = 1, progress: Callable[[int, int], None] | None = None):
        if n_jobs <= 1:
            return self._run_serial(...)
        with ProcessPoolExecutor(max_workers=n_jobs) as pool:
            futures = {pool.submit(_run_one, task): task for task in tasks}
            for i, fut in enumerate(as_completed(futures), 1):
                results.append(fut.result())
                if progress:
                    progress(i, len(tasks))
```

两个实现陷阱：

1. **策略实例必须可 pickle，或改为在子进程内按类路径重建**。当前策略是普通类，`Strategy` 实例含 `Decimal` 等，通常可 pickle；但若将来加锁/连接会失败。**决策：传类路径字符串而非实例**，子进程 `import` 后自行实例化，最稳。
2. **组合数上限对 CLI 也生效**。`MAX_COMBOS` 移到 `settings.backtest.max_combos`（默认 500），`quant optimize` 与看板共用。超限直接报错并提示「用 `--max-combos` 显式放开」。

### 6.2 进度

`optimize_jobs` 表记录 `total`/`completed`，`progress` 回调每完成一个组合就更新一次（`asyncio.to_thread` 写库有开销，改为**每 10 个或每 2 秒**写一次）。

---

## 7. 扩展指标

| 指标 | 定义 | 边界 |
|---|---|---|
| `calmar` | `annual_return / |max_drawdown|` | `max_drawdown == 0` → `None` |
| `sortino` | `mean(超额收益) / downside_deviation`，下行标准差只算负收益 | 无负收益 → `None`（不返回 inf） |
| `monthly_returns` | 按自然月聚合的收益率序列 | 存 JSON 列，月份数 = 回测跨度 |
| `max_consecutive_losses` | 连续亏损交易的最大笔数 | 0 笔交易 → 0 |
| `max_consecutive_loss_amount` | 上述连续段的最大累计亏损额 | 同上 |
| `exposure_ratio` | 有持仓的 bar 数 / 总 bar 数 | 无持仓 → 0 |
| `turnover` | 总成交名义 / 平均权益 | 供参考，PRD 未强制 |

**统一约定**：无法计算时返回 `None` 而不是 0 或 `inf`。理由：`None` 在 UI 显示 `—`，0 会被误读为「算出来是 0」。这与 PRD M1-10「不允许把缺失静默渲染成 0」一致。

---

## 8. Walk-forward 落库

`WalkForwardResult` 已实现（[walkforward.py](../../src/quant/backtest/walkforward.py)），本切片只补落库：

- `backtest_runs.kind = "walkforward"`，`params` 存网格配置。
- `backtest_metrics` 存汇总：`compounded_return`、`profitable_segments`、`avg_oos_sharpe`、`total_oos_trades`。
- `segments.parquet` 存逐段：`index`、`is_range`、`oos_range`、`params`（JSON）、`is_metrics`（JSON）、`oos_metrics`（JSON）。
- `modal_params` 单独存到 `backtest_metrics.monthly_returns`? **不行**，语义不符。新增一列 `modal_params TEXT`（JSON）。这是对 §10.2 表结构的一个补充。

---

## 9. 数据质量报告

### 9.1 产出

```json
{
  "inst_id": "BTC-USDT-SWAP",
  "bar_size": "1H",
  "generated_at": 1793374800000,
  "checked_range": [1793000000000, 1793374800000],
  "summary": {"expected": 105, "present": 103, "gaps_open": 1, "gaps_repaired": 1},
  "gaps": [
    {"from": 1793100000000, "to": 1793110000000, "bars": 2,
     "status": "repaired", "repaired_at": 1793120000000},
    {"from": 1793300000000, "to": 1793300000000, "bars": 1, "status": "open"}
  ],
  "invalid_ohlc": [{"ts": 1793200000000, "reason": "high < low"}]
}
```

路径：`data/quality/{inst_id}/{bar_size}/latest.json`（按目录分层，避免 `inst_id` 里的 `-` 与文件名解析冲突）。

### 9.2 与现有 `ValidationReport` 的差异

`ValidationReport` 是**一次校验的内存结果**，`MissingRecord` 只有 `from_ts/to_ts/count`，没有 `status`。质量报告需要**跨次累计**哪些缺口已被补齐：

```python
def merge_report(prev: dict | None, report: ValidationReport, now: int) -> dict:
    """把本次校验结果并入上次报告：本次不再缺的 gap 标 repaired。"""
    prev_gaps = {g["from"]: g for g in (prev or {}).get("gaps", [])}
    current = {m.from_ts: m for m in report.missing}
    out = []
    for from_ts, gap in prev_gaps.items():
        if from_ts in current:
            out.append({**gap, "status": "open"})       # 仍缺
        elif gap["status"] == "open":
            out.append({**gap, "status": "repaired", "repaired_at": now})
        else:
            out.append(gap)
    for from_ts, m in current.items():
        if from_ts not in prev_gaps:
            out.append({"from": m.from_ts, "to": m.to_ts, "bars": m.count, "status": "open"})
    return {..., "gaps": out}
```

### 9.3 触发点

1. **candles 每轮拉取后**：对本次涉及的区间跑 `validate` + `merge_report` + 落盘。
2. **`quant check-data --inst --bar --start --end`**：手动全量校验。
3. **未修复项告警**：`gaps_open > 0` 时调 `AlertService.publish(rule="data_quality", level="WARNING")`，按 `(inst, bar)` 冷却 1 小时（数据缺口不会分钟级自愈）。

### 9.4 API

`GET /api/quality?inst_id=&bar_size=` → 单条或列表（不传参数返回全部 `latest.json` 的摘要）。

---

## 10. 存储设计

沿用 `PRAGMA user_version` 迁移。**版本占用**：MS4-A 用 v2/v3，MS4-B 用 v4/v5，本切片用 **v6**。

### 10.1 `strategy_params`

```sql
CREATE TABLE IF NOT EXISTS strategy_params (
    strategy_id TEXT NOT NULL,
    param       TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (strategy_id, param)
);
```

### 10.2 `strategy_param_changes`

```sql
CREATE TABLE IF NOT EXISTS strategy_param_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    param       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT NOT NULL,
    reason      TEXT,
    changed_by  TEXT NOT NULL,
    changed_at  INTEGER NOT NULL,
    git_commit  TEXT
);
CREATE INDEX IF NOT EXISTS idx_param_changes ON strategy_param_changes(strategy_id, changed_at DESC);
```

### 10.3 `strategy_bindings`

见 §4.1。

### 10.4 `backtest_runs` / `backtest_metrics` / `backtest_artifacts` / `optimize_jobs`

```sql
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id      TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    inst_id     TEXT NOT NULL,
    bar         TEXT NOT NULL,
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL,
    params      TEXT NOT NULL,          -- JSON
    kind        TEXT NOT NULL,          -- backtest / optimize / walkforward
    status      TEXT NOT NULL,          -- pending / running / done / failed / canceled
    created_at  INTEGER NOT NULL,
    started_at  INTEGER,
    finished_at INTEGER,
    error       TEXT,
    git_commit  TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON backtest_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_filter ON backtest_runs(strategy_id, inst_id, bar, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON backtest_runs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_metrics (
    run_id          TEXT PRIMARY KEY REFERENCES backtest_runs(run_id),
    total_return    TEXT, annual_return TEXT, sharpe TEXT, max_drawdown TEXT,
    win_rate        TEXT, profit_factor TEXT, trade_count INTEGER,
    avg_holding_bars TEXT,
    -- M4-14 扩展
    calmar TEXT, sortino TEXT,
    max_consecutive_losses INTEGER,
    max_consecutive_loss_amount TEXT,
    exposure_ratio TEXT,
    monthly_returns TEXT,               -- JSON：[{"month":"2026-01","ret":0.03}]
    -- walkforward 汇总（M4-6）
    compounded_return TEXT, profitable_segments INTEGER,
    avg_oos_sharpe TEXT, total_oos_trades INTEGER, modal_params TEXT,
    -- Pine 对比（M4-16，列先建，MS5 落数据）
    pine_total_return TEXT, pine_sharpe TEXT, pine_max_drawdown TEXT, pine_deviation TEXT
);

CREATE TABLE IF NOT EXISTS backtest_artifacts (
    run_id TEXT NOT NULL,
    kind   TEXT NOT NULL,               -- equity / drawdown / trades / segments / report_html
    path   TEXT NOT NULL,
    size   INTEGER,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, kind)
);

CREATE TABLE IF NOT EXISTS optimize_jobs (
    job_id      TEXT PRIMARY KEY,
    run_id      TEXT REFERENCES backtest_runs(run_id),
    grid        TEXT NOT NULL,          -- JSON
    metric      TEXT NOT NULL,
    n_jobs      INTEGER NOT NULL,
    total       INTEGER NOT NULL,
    completed   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    finished_at INTEGER
);
```

> **关于 `PineBaseline`**：PRD §4.4.2 列了 5 个模型但只建 4 张表。`PineBaseline` 不单独建表，复用 `backtest_metrics` 的 4 个 `pine_*` 列（一次回测最多对应一份 Pine 基线，1:1 关系放同表更简单）。M4-16 在 MS5 实现时只需填这 4 列。

---

## 11. API 契约

### 11.1 策略参数

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/strategies` | 扩展：返回 `param_spec[]`、`values[]`、`sources[]`、`editable` |
| GET | `/api/strategies/{id}/params` | 生效参数 + 来源（`toml`/`db`） |
| PUT | `/api/strategies/{id}/params` | body `{values: {}, reason, by}`；按 `PARAM_SPEC` 校验 |
| GET | `/api/strategies/{id}/param-changes` | 变更历史，分页 |
| GET | `/api/strategies/{id}/bindings` | 绑定矩阵 |
| PUT | `/api/strategies/{id}/bindings/{inst_id}` | body `{enabled, bar?}`；`bar` 变更需先解绑 |

### 11.2 回测

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/backtest` | **改为异步**：返回 `{run_id, status:"pending"}`（原为同步返回结果） |
| POST | `/api/optimize` | 同上，返回 `{run_id, job_id}` |
| POST | `/api/walkforward` | 新增，返回 `{run_id}` |
| GET | `/api/backtest/{run_id}` | 状态 + 指标 + 产物清单（不含曲线数据） |
| GET | `/api/backtest/{run_id}/equity` | 降采样权益序列 |
| GET | `/api/backtest/{run_id}/trades` | 成交明细（分页） |
| POST | `/api/backtest/{run_id}/cancel` | 取消 |
| POST | `/api/backtest/{run_id}/rerun` | 复制参数重跑，返回新 `run_id` |
| GET | `/api/backtest/runs` | 历史列表，筛选 + 分页 |
| GET | `/api/backtest/compare?ids=` | 多组对比，校验同 `inst_id` + `bar` |
| GET | `/api/optimize/{job_id}` | 寻优进度 + top N 结果 |

> **破坏性变更**：`POST /api/backtest` 从「同步返回结果」改为「返回 run_id」会破坏现有前端。对策：保留 `?sync=1` 参数走旧路径一个版本，前端切换后移除。这是本切片唯一的前端契约破坏点，需在实施时优先切前端。

### 11.3 数据质量

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/quality` | 参数 `inst_id`/`bar_size`，不传返回全部摘要 |

---

## 12. 配置扩展

```toml
[backtest]
max_combos = 500              # 新：看板与 CLI 共用上限
n_jobs = 1                    # 新：寻优进程数默认值
task_workers = 1              # 新：回测任务消费者数

[engine]
# 多绑定改造后，以下两项降级为「初始绑定」的兼容入口
strategy = "sma_cross"
inst = "BTC-USDT-SWAP"
bar = "1H"
bindings_fail_mode = "db"     # 新：Redis 不可用时 db / halt
poll_interval_seconds = 60    # 新：多绑定轮询周期

[quality]
enabled = true                # 新：candles 每轮是否生成质量报告
alert_cooldown_seconds = 3600 # 新：未修复项告警冷却
```

> `[engine] strategy/inst/bar` 保留是为了平滑迁移：首次启动时若 `strategy_bindings` 为空，用这三项创建一条初始绑定，之后以表为准。

---

## 13. 迁移与兼容

| 阶段 | 动作 | 回滚 |
|---|---|---|
| 1 | v6 建全部表 | 删表 |
| 2 | `PARAM_SPEC` 加到 `sma_cross`/`rsi`/`sr_exit` | 移除类属性，行为回到 toml |
| 3 | `resolve_params` 上线，engine 与回测接入 | 开关 `params.resolve_from_db=false` |
| 4 | 参数写入 API + 变更历史 + 前端表单 | 前端回退，接口保留 |
| 5 | 绑定表 + 引擎多绑定 | 开关 `engine.multi_binding=false` 回到单组合 |
| 6 | 任务队列 + 回测落库（`?sync=1` 兼容） | 前端切回 sync |
| 7 | 扩展指标 | 纯增量，无回滚需求 |
| 8 | 寻优并发 + 进度 | `n_jobs=1` 即回到串行 |
| 9 | walk-forward 落库 | 纯增量 |
| 10 | 数据质量报告 + API + 告警 | `quality.enabled=false` |

**第 5 步是风险最高的一步**（引擎是交易主路径）。建议先以「绑定表只有一条记录」的方式验证一轮，确认行为与现有单组合完全一致，再开第二组合。

---

## 14. 错误处理与降级

| 故障 | 行为 |
|---|---|
| `PARAM_SPEC` 与 toml 参数不一致 | 加载时报错并拒绝启动该策略（避免「参数名打错但静默用默认值」） |
| DB 覆盖值类型/范围非法（手工改库） | 忽略该覆盖并告警，回落 toml 值 |
| Redis 绑定缓存丢失 | 回落 SQLite 重建；失败时按 `bindings_fail_mode` 处理 |
| 回测进程崩溃 | `running` 标记 `failed`，产物目录保留供排查 |
| 磁盘不足写产物 | 任务标 `failed`，`error="disk full"`，不写半截文件 |
| 寻优子进程异常 | 该组合跳过并计数，最后报告 `skipped=N`，不整单失败 |
| 质量报告首次运行 | `prev=None`，所有缺口都是 `open` |
| `GET /api/backtest/{run_id}` 不存在 | 404，不用 500 |

---

## 15. 测试策略

| 层 | 用例 |
|---|---|
| `PARAM_SPEC` | 三类型校验；越界拒绝并给区间；未声明 spec → `editable=false`；spec 与 toml 参数名不一致报错 |
| 参数合并 | toml → db 覆盖；来源标注正确；非法 DB 值被忽略并回落；回测显式传参优先且 `source=request` |
| 参数变更 | 每次保存写两条记录；`git_commit` 取到/取不到两路径 |
| 绑定 | 同 `(strategy,inst)` 重复插入不同 `bar` 被拒；启停写库 + Redis 一致；Redis 丢失回落 DB |
| 引擎多绑定 | 两个绑定各自独立 `ctx` 与策略实例；停用一个不影响另一个；`last_bar_key` 不串 |
| 任务队列 | submit 立即返回；pending 可直接取消；running 在下一根 bar 退出；重启把 running 置 failed |
| 落库 | 指标 JSON 往返一致；`None` 保持 `NULL` 不写 0；artifacts 路径存在 |
| 对比校验 | 同 inst+bar 通过；不同 inst 返回 400 且指出差异项 |
| 指标 | 7 个新指标各算一个已知答案；`max_drawdown=0` 时 `calmar=None`；无负收益时 `sortino=None` |
| 寻优并发 | `n_jobs=1` 与 `n_jobs=4` 结果集相同；进度总数正确；组合上限对 CLI 生效 |
| 质量报告 | 首次全 open；修复后转 repaired 并记 `repaired_at`；`gaps_open>0` 触发告警且冷却 |
| 兼容 | `?sync=1` 仍同步返回；`multi_binding=false` 时行为与现状一致 |

**须用单测锁定的口径**：`sortino` 的下行标准差定义（是「负收益的标准差」还是「半方差的平方根」）、`exposure_ratio` 的分母（全部 bar 还是交易日 bar）、`calmar` 用的年化方式（与 `annual_return` 一致）。

---

## 16. 实现顺序

1. **v6 迁移 + store 方法**
2. **`PARAM_SPEC` + `resolve_params`**，engine 与回测接入（`source` 只读展示）
3. **参数写入 API + 变更历史 + 前端参数表单**
4. **绑定表 + 引擎多绑定**（最高风险，单独验证）
5. **启停 API + 前端矩阵**
6. **`TaskRunner` + 回测落库 + 异步 API（含 `?sync=1` 兼容）**
7. **历史列表 / 重跑 / 对比**
8. **扩展指标**
9. **寻优并发 + 进度**
10. **walk-forward 落库**
11. **数据质量报告 + `/api/quality` + 告警**

第 2 步是「一个真相源」的兑现点；第 6 步是「结果不再是一次性」的兑现点。两者可以并行开发（分别对应策略线与回测线）。

---

## 17. 验收标准映射

| 条目 | 验收信号 | 落点 |
|---|---|---|
| M2-15 数据质量报告 | `data/quality` 有文件；API 可查；未修复项告警 | §9 |
| M3-9 声明式参数元信息 | 三个策略均有 `PARAM_SPEC`；未声明的不可编辑 | §3.1 |
| M3-10 参数两层合并 | 回测与实盘同一 `resolve_params`；页面可看来源 | §3.2 |
| M3-11 启停矩阵 | `strategy×inst` 为键；同组合换周期被拒 | §4.1 |
| M3-12 启停即时生效 | 切换后 ≤1 个周期生效，无需重启 | §4.3 |
| M4-6 walk-forward | 已实现，本切片补落库与逐段产物 | §8 |
| M4-9 结果持久化 | 重启后历史可查；指标/产物入库 | §5, §10.4 |
| M4-10 历史与重跑 | 筛选分页；重跑生成新 run_id 不覆盖 | §5.4 |
| M4-11 多组对比 | 并列曲线与指标差；非同品种同周期被拒 | §5.4 |
| M4-12 后台任务与进度 | 提交即返回 run_id；可取消 | §5.2 |
| M4-13 寻优并发与进度 | `n_jobs` 真并行；进度上报；上限可配 | §6 |
| M4-14 扩展指标 | 7 项指标落库并在页面展示；无法计算时为 `—` | §7 |

---

## 18. 风险与开放问题

### 18.1 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 引擎多绑定改造引入交易 bug | 真实资金损失 | 先只跑一条绑定验证；加单测覆盖 `ctx` 隔离；`multi_binding=false` 可回退 |
| `POST /api/backtest` 改异步破坏前端 | 页面报错 | 保留 `?sync=1` 一个版本；先切前端再删兼容 |
| 进程池 pickle 失败 | 寻优直接不可用 | 传类路径在子进程重建，不传实例 |
| 指标定义漂移 | 跨版本结果不可比 | 单测锁定 3 个易错指标的口径；`backtest_metrics` 加 `metrics_version` 列（见开放问题） |
| 质量报告与 candles 耦合 | 拉数据变慢 | 校验是 O(n) 内存计算，落盘 io 很小；`quality.enabled=false` 可关 |

### 18.2 开放问题

| 问题 | 决策时机 |
|---|---|
| `backtest_metrics` 是否加 `metrics_version` 以标记指标口径版本 | 实施时定；倾向加，成本一个列 |
| 回测产物是否需要自动清理（`data/backtests` 会持续增长） | 归 M10-13 保留策略，本切片先不删 |
| 绑定矩阵是否需要「批量启停」（一次切多个品种） | 前端需求驱动，后端先只支持单条 |
| 引擎多绑定时，同一策略多品种的风控总额是否需要按品种拆分 | 归 M7-2 口径（账户级汇总），本切片不改 |
| 参数回滚（M3-14）复用 `strategy_param_changes` 是否够 | MS5 实施时确认 |

---

## 19. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-30 | 初版。`PARAM_SPEC` 与参数两层合并（回测/实盘同一真相源）；`strategy_bindings` 与引擎多绑定改造；回测任务队列与 4 张表落库；寻优进程池并发与进度；7 个扩展指标（无法计算返回 `None`）；walk-forward 落库；`data/quality` 数据质量报告与告警 |
