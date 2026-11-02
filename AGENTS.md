# AGENTS.md

> 本文件是**任何 AI/工程师接手本仓库时的第一入口**。先读完这里，再按「阅读顺序」展开。
> 最后更新：2026-11-02

## 1. 这是什么项目

个人用的加密货币量化交易平台，**只服务一个人、不对外**：

- 行情与交易：OKX（模拟盘已跑通，实盘代码就绪、待运维切 Key）
- 回测主战场：TradingView Pine；Python 回测用于交叉验证并量化偏离度
- 执行层：FastAPI webhook → Redis 队列 → worker → 风控 → OKX 下单
- 看板：React + TS + Vite + ECharts，只读为主，写操作有守卫

一句话：**数据（K线/费率/OI）→ 策略（Pine+Python 双副本）→ 回测/寻优 → webhook/engine 出信号 → 风控 → 下单 → 对账 → 监控告警 → 看板**。

## 2. 阅读顺序（务必按序，最多花 30 分钟）

| 顺序 | 文档 | 解决什么问题 |
|---|---|---|
| 1 | 本文件 | 边界、硬规则、命令 |
| 2 | [docs/2026-11-02-project-handoff.md](docs/2026-11-02-project-handoff.md) | 架构、数据流、schema、Redis 键、坑 |
| 3 | [docs/requirements/2026-11-02-feature-inventory-and-completion.md](docs/requirements/2026-11-02-feature-inventory-and-completion.md) | **当前每个功能做到哪了**、还剩什么 |
| 4 | [PRD v0.6](docs/requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md) | 「应该做什么」的唯一权威（153 条需求） |
| 5 | `docs/specs/2026-10-30-ms4*-design.md` | 「怎么做的」细节（按模块选读） |
| 6 | [RUNBOOK](docs/reference/RUNBOOK_PAPER_TRADING.md) | 怎么启动、怎么切实盘、怎么应急 |
| 7 | `交易术语详解.md` | 业务术语（滑点、盈亏比、资金费率等） |

**不要**凭 PRD 的 ✅/🟡 标记直接开工：PRD 记录的是需求状态，代码细节以下面第 4 节和清单文档为准。

## 3. 环境与命令

```bash
# 安装（注意：README 的快速开始只写了 m1，实际跑看板还需要 m2）
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[m1,m2,dev]"

# 配置（模板已在仓库；真实文件不入 git）
# config/config.toml、config/strategies/{sma_cross,rsi}.toml、.env 已由模板复制生成
# 只需编辑 .env 填 OKX_API_KEY / OKX_SECRET / OKX_PASSPHRASE（模拟盘 Key）
# 新增策略：cp config/strategies/sma_cross.toml.example config/strategies/<id>.toml

# 后端：一条命令拉起全部常驻进程（推荐）
quant supervisor --strategy sma_cross --broker okx --td-mode cash --paper-mode --ws
# 单独起：quant dashboard（:8080）、quant serve-webhook（:8000）、quant run-worker、quant monitor、quant run-engine

# 前端
cd web && npm install
npm run dev        # 开发
npm run build      # 产物到 web/dist，dashboard 从这里挂静态页
```

### 必须会跑的校验（提交前）

```bash
python3 -m ruff check src tests/<你改的测试文件>     # 全仓有既有 W293，不要顺手改无关文件
python3 -m pytest -q                                # 期望 1644 passed，0 failed
cd web && npm run typecheck && npm run build        # 或 npx tsc --noEmit
```

`tests/smoke/` 不是 pytest 用例（没有 `test_` 函数，只有 `__main__` 脚本），跑 pytest 不会执行；需要真实 OKX 凭证时手动 `python3 tests/smoke/xxx.py`。

## 4. 代码地图（改之前先定位）

| 你要做的事 | 去哪里 |
|---|---|
| 行情下载/校验/对齐 | `src/quant/fetcher/` `storage/` `models/` |
| 回测/寻优/walk-forward/批量 | `src/quant/backtest/`（`engine.py` 是主循环） |
| 策略与参数 | `src/quant/strategies/` + `config/strategies/*.toml` |
| 实盘信号引擎 | `src/quant/engine/live.py` |
| 风控 | `src/quant/risk/`（`checker.py` 规则、`circuit_breaker.py` 熔断） |
| 下单执行 | `src/quant/execution/`（`worker.py` 主流程） |
| 告警/监控 | `src/quant/monitor/` |
| 看板 API | `src/quant/dashboard/app.py`（路由）+ `service.py`（聚合）+ `tasks.py`（回测任务） |
| SQLite | `src/quant/state/sqlite_store.py`（**schema 在这里**） |
| 前端 | `web/src/App.tsx`（所有页面组件）、`web/src/api.ts`（所有请求与格式化） |

## 5. 已知问题（接手先知道，别踩）

| 问题 | 事实 | 处理建议 |
|---|---|---|
| `optimize_jobs` 表不存在 | PRD 有模型，代码只有注释「留到各自切片再加迁移」 | 做 M4-13 寻优进度前先加迁移 |
| ruff 全仓 6 条 W293 | `tests/test_open_interest_fetcher.py` 空白行含空格，属既有问题 | 只 lint 你改的文件，不要顺手格式化全仓 |
| 前端 build 有 chunk > 500 kB 警告 | ECharts 体积，既有 | 可忽略 |
| `.env` 里的 OKX 三件套为空时启动即报错 | `_expand_env` 把空字符串视为未设置（`env var OKX_API_KEY not set`） | 属预期行为：必须填模拟盘 Key 才能启动 |

## 6. 硬规则（违反会出事，尤其是钱）

1. **金额/数量用 `Decimal`，SQLite 存 TEXT**；绩效指标用 REAL。不要把金额转 float。
2. **schema 变更只能追加迁移**：在 `sqlite_store.py` 的 `_MIGRATIONS` 末尾加一段，**绝不修改历史建表语句**（老库已执行过，改了等于永远缺列）。`SCHEMA_VERSION = 1 + len(_MIGRATIONS)` 自动变。
3. **新增指标列要改两处**：迁移加列 + `_METRIC_COLUMNS` 白名单。漏白名单会被 `save_backtest_metrics` **静默丢弃**。
4. **回测四条路径必须同时透传新参数**：单次、寻优（含进程池 worker）、walk-forward、批量。
5. **写接口必须 `_guard_write(request, token)`**（本机 Host + `X-Dashboard-Request: 1`）；前端必须用 `writeToken()` 带自定义头。
6. **新增 HTTP 接口必须同步 `web/src/api.ts`**，否则前端调不到；反之亦然。
7. **读取失败必须显式降级**（`degraded: ["sqlite" | "redis" | ...]`），**不允许**把缺失数据静默渲染成 0 或「—」。
8. **风控规则顺序固定**：日亏损 → 连亏 → 熔断（三条硬停止）→ 单笔名义 → 单品种比例 → 总仓位。新增规则要写 `risk_events`，并考虑是否发布告警。
9. **不要跨策略复用 checker**：多策略路由时每个 `strategy_id` 有独立 `RiskChecker`/`RiskLimits`。
10. **时间口径**：存储一律 UTC；风控「日亏损」按 UTC+8 切日（`day_start_ms_utc8`）；看板按浏览器本地时区展示。K 线时间戳是**开盘时间**。
11. **资金费率符号**：`cash -= rate × qty × price`，`qty` 带符号，所以 `rate > 0` 多头付、空头收。
12. **金额类指标前端要 4 位小数**（`AMOUNT_METRICS`），否则几分钱会被 `toFixed(2)` 抹成 0.00。
13. **下单幂等**：`clOrdId` 由 `signal_id` 派生；超时先按 `clOrdId` 查，查到就复用，禁止直接重发。
14. **不要新增第二条下单路径**：手工单也必须走 `ManualTrader` → 同一套风控/审计/状态机。

## 7. 改动完成的标准

- [ ] 行为改动配测试（放在对应 `tests/test_*.py`，风格跟现有文件）
- [ ] `ruff` 通过（只跑改动文件也可）
- [ ] `pytest -q` 不新增失败（基线：1644 passed / 0 failed）
- [ ] 改前端则 `npm run typecheck` + `npm run build` 通过
- [ ] 改 schema 则补「存量库升级」测试（参考 `test_sqlite_store.py::test_v8_db_gains_funding_cost_column`）
- [ ] 改指标/接口则同步 `api.ts`、`METRIC_LABELS`、`METRIC_ORDER`、`METRIC_HINTS`
- [ ] 更新 PRD 的对应条目状态 + 追加「变更记录」一行
- [ ] 更新 `docs/2026-11-02-feature-inventory-and-completion.md` 的完成度

## 8. 当前状态与建议起点

完成度：153 条需求中 ✅101 / 🟡7 / ⬜43 / 🚫2。M1、M2、M7、M8（代码侧）、M9 已完整；M3/M4 有少量缺口；M10 基本未开始；M5/M6 未开始（MS7+）。

建议顺序：

1. M8-12：给 `/api/orders`、`/api/fills` 补筛选参数 + 前端筛选条。
2. M4-6：看板加 walk-forward 提交入口与逐段表（后端已完整）。
3. M4-8：日报 FAIL 接入 `AlertService.publish`。
4. M4-13：加 `optimize_jobs` 迁移 + 寻优进度轮询。
5. M10-1/2/9：设置页从占位改为「只读配置视图 + 凭证健康」。

详细依据见清单文档第 4、7 节。
