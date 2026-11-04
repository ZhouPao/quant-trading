# 功能清单与完成度分析

> 生成日期：2026-11-02
> 依据：[PRD v0.6](2026-10-30-quant-trading-platform-requirements-v0.6.md) 的 153 条需求（M1–M10），逐条对照当前代码核对
> 核对范围：`src/quant/**`（后端）、`web/src/**`（前端）、`config/config.toml.example`、`tests/**`

## 0. 口径说明

| 标记 | 含义 |
|---|---|
| ✅ | 前后端均可用（若需求本身不涉及前端，则后端可用即 ✅） |
| 🟡 | 只做了一部分：后端有、前端没接，或只覆盖部分场景 |
| ⬜ | 未开始（无代码） |
| 🚫 | 本期明确不做（PRD 已声明） |

**「后端」列**写的是实现文件或服务，**「前端」列**写的是看板上的具体卡片/交互；标「—」表示该需求天然不涉及看板（如 CLI、数据落盘、内部契约）。

**状态以代码为准**。PRD 中原有 6 处标记滞后于代码，其中 4 处已于 2026-11-02 同步修正（M1、M8、M9、M7-17），剩余 2 处仍列在第 6 节。

---

## 1. 总览

| # | 模块 | 条目 | ✅ | 🟡 | ⬜ | 🚫 | 后端 | 前端 |
|---|---|---|---|---|---|---|---|---|
| M1 | 首页总览 | 10 | 10 | 0 | 0 | 0 | 完整 | 齐全 |
| M2 | 市场监控 | 15 | 14 | 0 | 0 | 1 | 完整 | 齐全（M2-13 不做） |
| M3 | 策略研究 | 18 | 17 | 1 | 0 | 0 | 完整 | 齐全（M3-13 仍 🟡） |
| M4 | 回测分析 | 16 | 15 | 1 | 0 | 0 | 完整 | 齐全（M4-8 未进告警体系） |
| M5 | 因子库 | 15 | 0 | 0 | 15 | 0 | 无 | 占位 |
| M6 | 组合管理 | 14 | 0 | 0 | 14 | 0 | 无 | 占位 |
| M7 | 风险控制 | 17 | 17 | 0 | 0 | 0 | 完整 | 齐全 |
| M8 | 交易执行 | 19 | 16 | 3 | 0 | 0 | 缺 1 条（订单筛选） | 缺订单/成交筛选；2 条为运维部署 |
| M9 | 日志告警 | 14 | 13 | 0 | 0 | 1 | 完整 | 齐全（M9-6 不做） |
| M10 | 系统设置 | 15 | 14 | 0 | 0 | 1 | 完整（M10-4 本期不做） | 配置/凭证/进程/备份/自检齐全 |
| **合计** | | **153** | **115** | **6** | **29** | **3** | | |

> 排除 3 条明确不做后，可做 150 条，已完成 115 条（**76.7%**）。剩余 29 条全部属
> MS7+（M5 因子库 15 条、M6 组合管理 14 条）。

按里程碑看剩余工作：

| 分组 | 数量 | 内容 |
|---|---|---|
| MS5 收尾 | 0 条 ⬜ + 4 条 🟡 | 无未开始项；🟡 为 M3-13、M4-8、M8-7、M8-8（另 M8-12 来源筛选待加列） |
| MS6 | 2 条 ⬜ | M10-6 进程启停、M10-7 备份与自检 |
| MS7+ | 29 条 ⬜ | M5 因子库 15 条、M6 组合管理 14 条 |

---

## 2. 前后端全景

### 2.1 看板页面（10 个 Tab）

| Tab | 状态 | 主要内容 | 主要接口 |
|---|---|---|---|
| 首页总览 | ✅ live | 账户权益与三档曲线、风险指标、今日盈亏、持仓摘要、告警摘要、告警趋势、今日事件流、数据新鲜度、进程健康 | `/api/overview` `/api/equity` `/api/today-pnl` `/api/timeline` `/api/freshness` `/api/processes` `/api/alerts/stats` |
| 市场监控 | ✅ live | K 线图 + 成交量、7 类指标叠加、周期切换、成交/持仓标注、数据质量 | `/api/candles` `/api/quality` |
| 策略研究 | ✅ live | 引擎状态、参数表单、参数来源、变更历史与回滚、启停矩阵、最近评估、最近回测、参数版本对比 | `/api/strategies/*` `/api/backtest/*` |
| 回测分析 | ✅ live | 单次/寻优/批量三种提交、任务进度与取消、历史筛选、详情指标、权益/回撤曲线、成交明细、月度收益、多组对比、Pine 对比、模拟盘日报 | `/api/backtest/*` `/api/optimize` `/api/reports` |
| 因子库 | ⬜ todo | 仅占位说明 | — |
| 组合管理 | ⬜ todo | 仅占位说明 | — |
| 风险控制 | ✅ live | 限额与来源、今日触发、在线调整阈值、覆盖列表与清除、熔断列表与人工恢复、紧急停止 | `/api/risk-limits` `/api/risk/overrides` `/api/risk/circuits/*` `/api/kill-switch` |
| 交易执行 | ✅ live | 手工下单、持仓与减仓/平仓、最近订单（撤/改）、最近成交、死信、对账差异、成交盈亏 | `/api/manual/orders` `/api/positions/*` `/api/orders` `/api/fills` `/api/dead-letters` `/api/reconciliations` `/api/pnl` |
| 日志告警 | ✅ live | 告警列表（级别/规则/状态/时间区间筛选）、已读、静音与解除、停用规则提示 | `/api/alerts` `/api/alerts/*` |
| 系统设置 | ⬜ todo | 仅占位说明 | — |

### 2.2 HTTP 接口（58 个业务端点 + `/api/health` + 静态页）

| 分组 | 端点 |
|---|---|
| 总览/行情 | `GET /api/overview` `/api/positions` `/api/equity` `/api/engine` `/api/freshness` `/api/timeline` `/api/processes` `/api/pnl` `/api/today-pnl` `/api/reports` `/api/candles` `/api/quality` |
| 策略 | `GET /api/strategies` `GET/PUT /api/strategies/{id}/params` `GET /api/strategies/{id}/param-changes` `POST /api/strategies/{id}/param-changes/{cid}/rollback` `GET/PUT /api/strategies/{id}/bindings[/{inst}]` `GET /api/strategies/{id}/evaluations` |
| 回测 | `POST /api/backtest` `/api/walkforward` `/api/backtest/batch` `/api/optimize`；`GET /api/backtest/runs` `/api/backtest/compare` `/api/backtest/{id}` `/equity` `/trades` `/segments` `/batch`；`POST /api/backtest/{id}/cancel` `/rerun` `/pine` |
| 风控 | `GET /api/risk-limits`；`GET/POST /api/risk/overrides`；`DELETE /api/risk/overrides/{strategy}/{param}`；`POST /api/kill-switch`；`POST /api/risk/circuits/{inst}/resume` |
| 执行 | `GET /api/orders` `/api/fills` `/api/dead-letters` `/api/reconciliations`；`POST /api/manual/orders` `POST /api/manual/orders/{id}/cancel` `/amend`；`POST /api/positions/reduce` `/flatten`；`POST /api/dead-letters[/{id}]/replay` `POST /api/dead-letters/{id}/discard` `POST /api/reconciliations/{id}/resolve` |
| 告警 | `GET /api/alerts` `/api/alerts/stats`；`POST /api/alerts/{id}/ack` `POST /api/alerts/silence` `DELETE /api/alerts/silence/{id}` |

写操作统一要求本机 `Host` + `X-Dashboard-Request: 1`（可选 token）；前端通过 `writeToken()` 统一携带。

### 2.3 后端包

| 包 | 职责 | 关键文件 |
|---|---|---|
| `okx/` | 签名、REST、限频、WebSocket、实盘自检 | `client.py` `signer.py` `rate_limiter.py` `ws_client.py` `live_check.py` |
| `fetcher/` | K 线/产品/资金费率/持仓量拉取与断点 | `candle_fetcher.py` `instrument_fetcher.py` `funding_rate_fetcher.py` `open_interest_fetcher.py` `checkpoint.py` |
| `storage/` | parquet 落盘、校验、多品种对齐、质量报告、保留清理 | `bar_storage.py` `validator.py` `quality.py` `aligner.py` `retention.py` |
| `backtest/` | 引擎、撮合、指标、报告、寻优、walk-forward、批量、资金费率、Pine 对比 | `engine.py` `matcher.py` `metrics.py` `report.py` `optimizer.py` `walkforward.py` `batch.py` `funding.py` `pine_compare.py` |
| `strategies/` | 策略加载、PARAM_SPEC、参数合并、启停绑定 | `loader.py` `params.py` `bindings.py` `python/*` `pine/*` |
| `engine/` | 实盘/模拟盘信号评估循环 | `live.py` |
| `risk/` | 两层限额、6 条规则、事件落库、在线覆盖、熔断、预检、仓位盈亏 | `checker.py` `limits.py` `events.py` `overrides.py` `circuit_breaker.py` `preflight.py` `pnl.py` |
| `webhook/` | TradingView 接收、鉴权、解析 | `server.py` `auth.py` `parser.py` |
| `queue/` | Redis 信号队列与去重 | `signal_queue.py` |
| `execution/` | worker、broker、状态机、幂等、仓位计算、死信、对账、kill switch、手工交易 | `worker.py` `okx_broker.py` `executor.py` `sizing.py` `dead_letter.py` `reconciler.py` `kill_switch.py` |
| `monitor/` | 监控轮询、快照、12 条告警规则、邮件 | `monitor.py` `snapshot.py` `alert_service.py` `alerter.py` |
| `dashboard/` | 只读 API、回测任务、手工交易、写守卫 | `app.py` `service.py` `backtest.py` `tasks.py` `manual.py` |
| `state/` | SQLite 全量状态（schema v9）、订单状态机 | `sqlite_store.py` `order_state.py` |
| `report/` | 模拟盘日报与偏离度 | `daily.py` |
| `analysis/` | 成交对比与偏离计算 | `deviation.py` |
| `cli/` | 23 个子命令 | `__init__.py` 及各子命令模块 |

### 2.4 CLI 子命令（23 个）

`backtest` `optimize` `walk-forward` `fetch-candles` `fetch-instruments` `fetch-funding-rates` `fetch-open-interest` `validate-data` `check-data` `align-data` `serve-webhook` `run-engine` `prune` `run-worker` `simulate-alert` `monitor` `kill-switch` `dashboard` `sync-candles` `test-email` `check-live` `supervisor` `paper-report`

> 注：`backtest`/`optimize`/`walk-forward` 共用 `_add_backtest_args` 注册参数，命令名各自独立。

---

## 3. 模块明细

### 3.1 首页总览（M1）—— 10/10 ✅

| 编号 | 功能 | 后端 | 前端 | 状态 |
|---|---|---|---|---|
| M1-1 | 账户总览卡 | `service.overview()` 汇总 Redis 快照 | 首页「账户权益」卡：余额/可用/冻结/未实现/保证金率 | ✅ |
| M1-2 | 持仓摘要 | `service.positions()` | 「持仓摘要」表，可跳转交易执行 | ✅ |
| M1-3 | 风险摘要 | `service.overview().metrics` | 「风险指标」四卡（回撤/撤单率/强平距离/队列） | ✅ |
| M1-4 | 权益曲线 | `equity_snapshots` + `service.equity(hours)` | 24h/7d/30d 三档切换 | ✅ |
| M1-5 | 7 进程健康 | `heartbeat` 约定 + supervisor 写 `quant:process:*`；worker 上报 WS 状态 | 「进程健康」7 格网格 + engine 周期/最近评估/落后根数；worker 显示 WS 状态 | ✅ |
| M1-6 | 今日事件流 | `store.daily_timeline()` 四表 UNION ALL；`GET /api/timeline` | 类型多选筛选、点击跳转对应模块、降级标注 | ✅ |
| M1-7 | 数据新鲜度 | `GET /api/freshness`（`lag_bars` + `missing_bars`，阈值 1 根） | 「数据新鲜度」卡，区分「engine 落后 N 根」「本地缺 N 根」 | ✅ |
| M1-8 | 今日盈亏汇总 | `store.get_today_pnl_by_inst()` + `service.today_pnl()` | 「今日盈亏」卡：汇总 + 按品种明细表 | ✅ |
| M1-9 | 告警摘要卡 | `GET /api/alerts` + `/api/alerts/stats` | 「告警摘要」按级别计数 + Top5 规则，点击跳转 | ✅ |
| M1-10 | 降级与时效标注 | 各接口返回 `degraded[]` | 每卡显示「数据截至 HH:MM」与降级项 | ✅ |

### 3.2 市场监控（M2）—— 14 ✅ / 1 🚫

| 编号 | 功能 | 后端 | 前端 | 状态 |
|---|---|---|---|---|
| M2-1 | 历史 K 线下载 | `CandleFetcher` 翻页 + 断点；`sync-candles` | — | ✅ |
| M2-2 | 实时行情订阅 | `WSClient` 仅订阅 tickers，退避 5s→60s ±20%，状态写 `quant:ws:status:worker` | 进程健康卡显示 WS 连接/失败次数/重连倒计时 | ✅ |
| M2-3 | 产品配置拉取 | `InstrumentFetcher`，现货/合约分开 | — | ✅ |
| M2-4 | 资金费率 | `FundingRateFetcher.fetch_history()` + `fetch-funding-rates --start/--end` | — | ✅ |
| M2-5 | 数据存储 | `BarStorage` 按品种/周期/年份 parquet，snake_case | — | ✅ |
| M2-6 | 限频管理 | `RateLimiter` 端点组令牌桶，50011 退避重试 | — | ✅ |
| M2-7 | 完整性校验 | `DataValidator`：缺口补拉 ≤3 次、去重、OHLC 合法性 | 数据质量卡展示结果 | ✅ |
| M2-8 | 多品种对齐 | `DataAligner` + `align-data` | — | ✅ |
| M2-9 | K 线图表页 | `GET /api/candles` | ECharts 蜡烛图 + 成交量、缩放、十字光标、成交/持仓标注 | ✅ |
| M2-10 | 技术指标叠加 | — | `IndicatorBar`：MA/BOLL/SuperTrend/撑压/VOL/MACD/KDJ，参数可改并本地保存 | ✅ |
| M2-11 | 支撑压力位 | `indicators/support_resistance.py` 同口径参数 | 图上档位线 + 参数配置 | ✅ |
| M2-12 | 周期切换 | `available_bars` 按本地已下载周期返回 | 周期按钮组（含 1m，动态取后端） | ✅ |
| M2-13 | 自选与对比 | `DataAligner` 具备能力 | 明确不做 | 🚫 |
| M2-14 | 持仓量 | `OpenInterestFetcher` + `fetch-open-interest` | —（供后续因子使用） | ✅ |
| M2-15 | 数据质量报告 | `storage/quality.py` + `GET /api/quality` | 「数据质量」卡 + 明细（缺失段/已修复/未修复） | ✅ |

### 3.3 策略研究（M3）—— 15 ✅ / 1 🟡 / 2 ⬜

| 编号 | 功能 | 后端 | 前端 | 状态 |
|---|---|---|---|---|
| M3-1 | 策略接口规范 | `backtest/strategy.py` 五个回调；实盘/回测共用 | — | ✅ |
| M3-2 | 策略登记 | `loader.py` 扫描 `config/strategies/*.toml`；`GET /api/strategies` | 策略下拉与引擎面板 | ✅ |
| M3-3 | 参数配置 | `params.py` 校验类型/范围/枚举；`PUT /params` | 参数编辑表单，非法值显示字段与区间 | ✅ |
| M3-4 | 策略启停 | `bindings.py` + `/bindings` 接口 | 启停矩阵逐格开关 | ✅ |
| M3-5 | 信号预览 | `Context.last_block_reason`：SMA/RSI 写入「预热/趋势过滤/无支撑/资金不足/RSI 区间」等具体原因，引擎透出到 `status.reason` | 引擎卡「状态」旁显示原因（已有 `engine-reason` UI） | ✅ |
| M3-6 | 双副本管理 | `strategies/pine/*.pine` + `strategies/python/*.py`，git 管理 | — | ✅ |
| M3-7 | 研究工作流 | `quant research export`（K线/成交/回测，parquet+JSON+manifest） | `notebooks/explore.ipynb` 模板 + README（只读导出目录） | ✅ |
| M3-8 | 参数版本与变更记录 | `strategy_param_changes` 只追加 | 「参数变更历史」表（含操作人/原因/commit） | ✅ |
| M3-9 | 声明式参数元信息 | `PARAM_SPEC`；`sma_cross`/`rsi` 已声明 | 未声明策略显示「参数只读」 | ✅ |
| M3-10 | 参数两层合并 | toml 默认 ← DB 覆盖，回测与实盘共用 | 「参数来源」标注 `(覆盖)` | ✅ |
| M3-11 | 启停矩阵 | 键为 `strategy_id × inst_id`，`bar` 不参与键 | 矩阵表 | ✅ |
| M3-12 | 启停即时生效 | Redis 缓存，engine 每轮读取 | 开关即时提交 | ✅ |
| M3-13 | 双副本一致性校验 | `tests/strategies/test_pine_python_consistency.py` 显式登记 Pine↔toml / PARAM_SPEC↔toml 漂移（测试级，无 CLI） | 无 | 🟡 |
| M3-14 | 参数变更回滚 | `rollback_param()` 写新记录，不删历史 | 「回滚」按钮（首条 old_value 为空时禁用） | ✅ |
| M3-15 | 策略详情页 | `/evaluations`（Redis 定长列表） | 参数/启停/最近 10 次评估/回测入口/变更历史 | ✅ |
| M3-16 | 策略一键回测 | 复用回测服务，参数不额外覆盖 | 「用当前参数回测」按钮带 preset 跳转并提交 | ✅ |
| M3-17 | 详情页回测结果回显 | `/api/backtest/runs?strategy=` | 最近 5 次回测表 + 「查看」载入历史结果 | ✅ |
| M3-18 | 参数版本回测对比 | `/api/backtest/compare` + `/equity` | 同品种同周期同区间分组对比、最优标注、曲线懒加载 | ✅ |

### 3.4 回测分析（M4）—— 15 ✅ / 1 🟡

| 编号 | 功能 | 后端 | 前端 | 状态 |
|---|---|---|---|---|
| M4-1 | 回测引擎 | `backtest/engine.py` 事件驱动、逐 bar 回放 | 详情指标与曲线 | ✅ |
| M4-2 | 成交模拟 | `matcher.py`：收盘出信号、下一根开盘成交；`protective_exit` bar 内成交；滑点可配 | — | ✅ |
| M4-3 | 绩效指标 | `metrics.py` 全套指标 | 指标卡 | ✅ |
| M4-4 | 回测报告 | `report.py` 输出 CSV + HTML（内联 SVG）；`GET /artifacts/{kind}` 白名单下载 | 详情页「产物下载」链接 | ✅ |
| M4-5 | 参数寻优 | `optimizer.py` 网格搜索、非法组合跳过、`max_combos` | 「寻优」模式 + 最优参数与结果表 | ✅ |
| M4-6 | Walk-forward | `walkforward.py` IS 选参/OOS 评估；`quant walkforward`；`GET /segments` | 「Walk-forward」模式 + 表单 + 逐段 IS/OOS 表 | ✅ |
| M4-7 | 批量回测 | `batch.py` 展平/逐格/排序/取消/失败隔离；`POST /api/backtest/batch` | 「批量回测」模式 + 汇总表（失败格带原因） | ✅ |
| M4-8 | 与实盘偏离 | `report/daily.py` 计算偏离并判 PASS/FAIL | 「模拟盘日报」表显示偏离与阈值，**未进告警体系** | 🟡 |
| M4-9 | 结果持久化 | 4 张表 + 产物 parquet；`/runs` `/detail` `/equity` `/trades` | 历史列表与详情，曲线/成交按需加载 | ✅ |
| M4-10 | 历史列表与重跑 | `/runs` 支持策略/品种/周期/状态/时间筛选；`/rerun` | 筛选 + 查看/重跑/取消 | ✅ |
| M4-11 | 多组结果对比 | `/api/backtest/compare` 校验同品种同周期 | 勾选 2–4 条并排曲线 + 差异表 + 最优高亮 | ✅ |
| M4-12 | 后台任务与进度 | `tasks.py` 单 worker FIFO；`pending/running/done/failed/canceled` | 每秒轮询状态条 + 取消按钮 | ✅ |
| M4-13 | 寻优进度与并发 | `n_jobs` 进程池生效；后端登记式 + 进度落 SQLite（P2）+ 进度查询 GET（P3）；前端 `submitOptimize` 提交 + `followOptimizeJob` 1s 轮询 + 进度条（P4） | 进度在看板可见，已闭环 | 🟢 |
| M4-14 | 扩展指标 | Calmar/Sortino/月度/连亏次数与金额/暴露度全部落库 | 指标卡 + 「月度收益」表 | ✅ |
| M4-15 | 永续资金费率成本 | `funding.py`；单次/寻优/walk-forward/批量统一透传；`metrics.funding_cost` 落库（schema v9） | 详情指标区「资金费率成本」卡（4 位小数、正负着色） | ✅ |
| M4-16 | Pine 结果导入对比 | `pine_compare.py` + `POST /pine`；偏离 >5% 拒绝启用 | `PineComparePanel` 粘贴框 + 逐项对比（超限标红） | ✅ |

### 3.5 因子库（M5）—— 0/15 ⬜

| 编号 | 功能 | 现状 |
|---|---|---|
| M5-1 ~ M5-15 | 因子注册、计算契约、参数版本、缓存、增量更新、横截面标准化、IC/RankIC/ICIR、分层收益、衰减、换手、相关性、缺失极值、`FactorProvider`、一致性校验、因子页 | 全部未开始；`src/quant/indicators/` 只有支撑压力，不是因子框架；`notebooks/` 为空 |

### 3.6 组合管理（M6）—— 0/14 ⬜

| 编号 | 功能 | 现状 |
|---|---|---|
| M6-1 ~ M6-14 | 组合配置、单元管理、四种分配、权重归一与上限、组合风控、日历/阈值再平衡、冷却期、调仓单、失败回滚、快照、归因、组合回测、单策略兼容、组合页 | 全部未开始；无 `portfolio` 包、无配置目录、无表 |

### 3.7 风险控制（M7）—— 17/17 ✅

| 编号 | 功能 | 后端 | 前端 | 状态 |
|---|---|---|---|---|
| M7-1 | 单笔/单品种限制 | `checker.py` 超限先减量、≤0 拒单 | 限额表 + 今日触发 | ✅ |
| M7-2 | 总仓位上限 | 账户级汇总全部品种 | 限额行 | ✅ |
| M7-3 | 日亏损/连亏保护 | UTC+8 切日；`halt=true` | 限额行 + 触发记录 | ✅ |
| M7-4 | 紧急停止 | kill switch 停策略 + 撤挂单，可勾选仅停止 | 「紧急停止」卡 + 二次确认 | ✅ |
| M7-5 | 实时风险指标 | monitor 快照 | 首页「风险指标」 | ✅ |
| M7-6 | 极端行情熔断 | 单根振幅超阈值按品种熔断，落事件 + 告警，冷却后转待确认 | 熔断列表 | ✅ |
| M7-7 | 下单前预检 | `preflight.py`：产品配置/精度/minSz/最小名义/现货余额 | 拒单原因在告警与审计可见 | ✅ |
| M7-8 | 阈值在线调整 | `overrides.py` 写 Redis + `risk_overrides`，心跳重读 | 在线调整表单 + 生效覆盖列表 + 清除 | ✅ |
| M7-9 | 风控规则页 | `risk_rule_stats()` 按规则聚合当日触发 | 规则行显示来源、今日次数、最近详情 | ✅ |
| M7-10 | 两层参数合并 | `resolve_limits()` 取严并标注来源 | 来源标签 `global/strategy/override` | ✅ |
| M7-11 | 风控事件落库 | `risk_events` 含 hits/权益/调整前后数量 | 触发详情展示 | ✅ |
| M7-12 | 熔断恢复策略 | 冷却结束待人工确认；`POST /resume` 写事件 + INFO 告警 | 「确认恢复」按钮 | ✅ |
| M7-13 | 熔断与 kill switch 分离 | 两个独立 Redis 键 | 两个独立卡片 | ✅ |
| M7-14 | 阈值变更审计 | `risk_overrides` 只追加、撤销置标记；审计失败回滚 Redis | 「变更历史」 | ✅ |
| M7-15 | 权益口径统一 | 只用账户快照 `total_equity`，默认禁止 `initial_equity` 回退 | 显示快照时效 | ✅ |
| M7-16 | 规则边界单测 | `tests/test_risk.py` 覆盖等值/空持仓/零权益/多规则 | — | ✅ |
| M7-17 | 风控与告警联动 | 熔断直接 publish；worker `_publish_halt()` 发 `risk_halt` ERROR 并启用 kill switch | 告警列表可见 | ✅ |

### 3.8 交易执行（M8）—— 16 ✅ / 3 🟡

| 编号 | 功能 | 后端 | 前端 | 状态 |
|---|---|---|---|---|
| M8-1 | 模拟盘接入 | `x-simulated-trading` 由 `[okx] demo` 控制 | 顶栏 MODE 徽标 | ✅ |
| M8-2 | Webhook server | FastAPI + 5s ACK + 解析失败入死信 + hash 去重 | — | ✅ |
| M8-3 | 信号队列与 worker | Redis BRPOPLPUSH + inflight + 心跳 | 进程健康卡 | ✅ |
| M8-4 | 订单管理 | 下单/撤单/改单/批量/状态轮询/异常撤单 | 订单操作列 | ✅ |
| M8-5 | 状态持久化 | signals/orders/fills/positions 落 SQLite，重启恢复 | 列表可见 | ✅ |
| M8-6 | 启动对账 | worker 启动先 `recover_inflight` 再 `reconcile` | 对账差异卡 | ✅ |
| M8-7 | 实盘接入 | 代码完成：`quant check-live` 校验权限/子账户/IP；worker `demo=false` 自检 | 顶栏实盘红色 MODE | 🟡 待运维换 Key/IP |
| M8-8 | 真 webhook 接入 | 代码完成：三种 token 位置、公网无鉴权拒绝启动、真实 IP 日志、多策略路由 | 无（运维/部署） | 🟡 待公网反代与 TV 模板 |
| M8-9 | 异常处理与重试 | 重试 3 次、状态不明先查、余额不足停止 | 告警可见 | ✅ |
| M8-10 | 操作审计 | 下单/撤单记录时间/品种/价格/数量/策略/来源/原因 | 订单与死信列表 | ✅ |
| M8-11 | 多进程编排 | `supervisor` 托管 6 进程，指数退避重启，进程表 + 重启计数 | 进程健康卡 | ✅ |
| M8-12 | 执行页展示 | `/api/orders` 支持 inst/strategy/status，`/api/fills` 支持 inst | 订单表筛选栏（品种/策略/状态）+ 成交表品种筛选 | 🟡（来源筛选需 `orders.source` 列；持仓表未加筛选） |
| M8-13 | 减仓与平仓 | `reduce_only=true`，比例或指定数量 | 持仓行「减仓/平仓」+ 二次确认 | ✅ |
| M8-14 | 服务端仓位计算 | `sizing.py` 权益×比例→上限截断→lotSz/ctVal 取整，写 `sized` 事件 | 响应显示实际下单量 | ✅ |
| M8-15 | 订单状态机 | `order_state.can_transition` 单向推进，部分成交累加与均价重算 | 状态标签 | ✅ |
| M8-16 | 死信查看与重放 | `DeadLetterService.list/replay/discard` + 幂等检查 | 死信卡：单条/批量重放、丢弃 | ✅ |
| M8-17 | 手工下单与撤改 | `ManualTrader` 复用风控/审计/状态机；撤单幂等；改单仅限价 | 手工下单卡 + 撤/改操作 | ✅ |
| M8-18 | 三项对账 | 订单/持仓/余额，`auto` 自动校正、`manual` 待确认 + 告警 | 对账差异卡（类型筛选、只看未处理、标记处理） | ✅ |
| M8-19 | 下单幂等与重试边界 | `clOrdId` 派生、超时先查、重复 clOrdId 复用、≤3 次 | — | ✅ |

### 3.9 日志告警（M9）—— 13 ✅ / 1 🚫

| 编号 | 功能 | 后端 | 前端 | 状态 |
|---|---|---|---|---|
| M9-1 | 统一日志规范 | 单文件 + 10MB 轮转 + 30 天；`get_logger` 绑定名作前缀 | — | ✅ |
| M9-2 | 告警规则 | 12 条规则；`[monitor].disabled_rules` 规则级启停 | 日志告警页显示「已停用」 | ✅ |
| M9-3 | 邮件告警 | `EmailAlerter` 三要素齐备才发；`quant test-email` | —（运维配置） | ✅ |
| M9-4 | 监控自身心跳 | TTL 心跳 + 无 TTL 上次心跳键，重启检测中断 | 进程健康卡 | ✅ |
| M9-5 | 告警列表页 | `/api/alerts` 支持级别/规则/状态/时间区间 | 四项筛选 + 时间区间 + 总数与降级 | ✅ |
| M9-6 | 日志检索 | 明确不做，走 `grep` | — | 🚫 |
| M9-7 | 告警确认与静音 | ack/silence/unsilence；`silenced` 真正落库 | 标记已读、静音表单、生效静音列表 | ✅ |
| M9-8 | 告警持久化 | `alerts` 表为权威存储，Redis 仅缓存 | 历史可查 | ✅ |
| M9-9 | 去重与冷却 | 同规则 600s 冷却；kill switch 防刷屏；INFO 不受限 | — | ✅ |
| M9-10 | 静音粒度 | 规则/规则+品种/全局（`rule='*'`）三档 | 三档静音表单 | ✅ |
| M9-11 | 与业务事件联动 | 熔断、`risk_halt`、`backtest_failed`、`reconcile_mismatch` 四条已接 | 告警列表可见 | ✅ |
| M9-12 | 告警保留策略 | 按级别 90/30/7 天，`purge_alerts` 随保留任务执行 | — | ✅ |
| M9-13 | 存活邮件 | 每 `alive_email_hours` 一封，时间戳存 Redis | — | ✅ |
| M9-14 | 告警趋势统计 | `GET /api/alerts/stats?days=7/30` | 首页「告警趋势」卡（7/30 切换、按级别拆色） | ✅ |

### 3.10 系统设置（M10）—— 14 ✅ / 0 🟡 / 0 ⬜ / 1 🚫

| 编号 | 功能 | 后端 | 前端 | 状态 |
|---|---|---|---|---|
| M10-1 | 配置查看与编辑 | `GET /api/config` 脱敏生效配置（`config_view.py`） | 设置页只读表格 | ✅ |
| M10-2 | 凭证管理 | `GET /api/credentials`：OKX 权限/子账户/IP + SMTP 配置，不回显密钥，60s 缓存 | 设置页「凭证状态」卡 | ✅ |
| M10-3 | 数据保留策略 | 全局天数 + `retention_by_bar` 按周期分档 + 告警分档 + `dead_letters`/`funding` 清理 | — | ✅ |
| M10-4 | 通知设置 | 无热改 SMTP 接口 | — | 🚫 本期不做（产品决定） |
| M10-5 | 时间口径 | 内部/存储 UTC；K 线取开盘时间 | 展示按浏览器本地时区格式化（国内机器即 UTC+8） | ✅ |
| M10-6 | 进程与自启 | supervisor 命令通道（stop/start + 审计）；API 默认仅 demo 允许 | 进程卡启停按钮 + 回执 | ✅ |
| M10-7 | 部署与备份 | `quant backup` / `restore` / `doctor` | — | ✅ |
| M10-8 | 访问控制 | 本机 Host + `X-Dashboard-Request` + 可选 token；默认仅监听本机 | 写操作自动带守卫头 | ✅ |
| M10-9 | 配置来源标注 | 每项标 `toml`/`env`/`default` | 设置页「来源」列 | ✅ |
| M10-10 | 热改白名单 | `reload` + `reload_hint` + `GET /api/config/hot-reload` | 设置页「生效方式」列 + 图例 | ✅ |
| M10-11 | 凭证到期提醒 | OKX key 无固定到期；由 M10-2 的权限/子账户/IP 探测覆盖 | 同 M10-2 | ✅（由 M10-2 覆盖） |
| M10-12 | 备份完整性校验 | `restore` 前置校验 manifest/size/sha256，版本高于代码拒绝 | — | ✅ |
| M10-13 | 保留策略分档 | `retention_by_bar` 按周期覆盖，未列出的用全局天数 | — | ✅ |
| M10-14 | 进程命令审计 | `quant:control:audit` 定长 1000，含 command_id/operator/status/error | 进程卡最近 5 条回执 | ✅ |
| M10-15 | 部署自检 | `quant doctor`：SQLite/Redis/凭证/SMTP/新鲜度/磁盘，`--json`/`--strict` | — | ✅ |

---

## 4. 未完成与部分完成清单

### 4.1 🟡 部分完成（7 条）

| 编号 | 已完成 | 还缺什么 | 影响 |
|---|---|---|---|
| M3-5 信号预览 | 引擎状态、bar 时间、原因串 | 策略内部拦截条件（趋势过滤/撑压）无法区分 | 排查「为什么没信号」只能看代码 |
| M4-8 实盘偏离 | 日报计算 + PASS/FAIL + 邮件 | 未进 `alerts` 告警体系，看板只能看日报 | 偏离超标不会触发告警 |
| M4-13 寻优进度 | ~~进度未上报、接口同步~~ 已闭环 | 后端登记式 + 进度落 SQLite（P2）、进度查询 GET（P3）、前端 1s 轮询 + 进度条（P4） | 已闭环（大网格寻优有实时进度） |
| M8-7 实盘接入 | 代码、自检、模式标识 | 申请子账户 Key、绑定 IP、关提币、`demo=false` | 只能跑模拟盘 |
| M8-8 真 webhook | 鉴权、多策略路由、IP 日志 | 公网域名/反代/TLS、TV alert 模板 | 只能本机/内网触发 |
| M8-12 执行页 | 订单/成交/持仓三张表 | 品种/策略/状态/来源筛选（前端与 `/api/orders` 均无） | 单量大时定位订单靠肉眼 |
| ~~M10-3 保留策略~~ | 全局天数 + 告警分档 + `quant prune` | **已在 2-6 完成**：按周期分档 + dead_letters/funding | 已闭环 |

### 4.2 ⬜ 未开始（43 条）

| 范围 | 条目 | 说明 |
|---|---|---|
| MS5 | M3-7（M3-13 为 🟡，M10 除 M10-4 外均已完成） | 研究工作流；M10-4 通知设置本期不做；M10-11 由 M10-2 覆盖 |
| MS6 | M10-6、M10-7 | 看板进程启停、备份与 `doctor`（上云/systemd 属运维） |
| MS7+ | M5-1 ~ M5-15、M6-1 ~ M6-14（29 条） | 因子库与组合管理，PRD 已排到 MS7+，不阻塞实盘 |

### 4.3 代码层缺口（PRD 未单列，但影响使用）

| 缺口 | 现状 | 建议 |
|---|---|---|
| Walk-forward 无看板入口 | 后端与 CLI 完整，前端只有历史标签 | 回测面板加「滚动验证」模式，详情加逐段表 |
| 回测报告无法从看板导出 | `report.py` 生成 CSV/HTML，页面只展示曲线与成交 | 详情页加「下载 CSV/HTML」按钮（产物已有路径） |
| 订单/成交无筛选 | `/api/orders`、`/api/fills` 只有 `limit` | 补 `inst/strategy/status/source` 查询参数与前端筛选条 |
| 设置页整体占位 | M10 12 条未开始 | 按 MS5 清单先做只读配置视图与凭证健康 |

### 4.4 代码层修复记录（2026-11-02）

| 日期 | 问题 | 修复 | 影响 |
|---|---|---|---|
| 2026-11-02 | 存量库 `user_version=8` 且 `funding_cost` 列已存在，v9 迁移 `ADD COLUMN` 非幂等 → 打开即 `duplicate column`，5 个子进程秒退、看板打不开 | `_apply_migrations` 加 `try/except`，`duplicate column` 视为已应用跳过；不改历史 DDL | 存量库可正常打开，6 进程自愈拉满；**完成度不变** |

---

### 4.5 代码层补全记录（2026-11-03）

| 日期 | 问题 | 补全 | 影响 |
|---|---|---|---|
| 2026-11-03 | `optimize_jobs` 表从未创建（AGENTS.md 已知问题 #1；本文 §后续优先级曾误记「已有表」） | `_MIGRATIONS` 追加 v9→v10 幂等建表段，照 spec §10.4 原样建表 | `SCHEMA_VERSION`→10；存量库升级自动补表；M4-13 进度上报的落库基础就绪（A2 仍待做） |
| 2026-11-03 | 寻优仍同步、无进度上报（M4-13） | A2-P2：`start_optimize` 登记式任务 + `optimize_jobs.completed` 按 10 组合/2s 节流落 SQLite，`/api/optimize` 立即返回 job_id；复用 `asyncio.Semaphore(1)` 保持单消费者 | 进度跨重启可查（结果暂存内存待 P3 取回）；取消按钮 / Redis 实时轮询留后续期 |
| 2026-11-03 | 寻优结果无法按 job 取回（M4-13 闭环缺口）| A2-P3：`BacktestService.get_optimize_job` 读 optimize_jobs 并解析 grid、附结果/错误；新增 `GET /api/optimize/{job_id}`（done 带完整结果，404 未知）；同步 `api.ts` 的 `OptimizeJobResponse`/`getOptimizeJob` | 后端进度查询契约就绪，前端轮询 UI（P4）待做 |
| 2026-11-03 | 前端仍同步等寻优结果、页面无反馈（M4-13 最后一环）| A2-P4：`runOptimize`→`submitOptimize`（返 job_id）；新增 `followOptimizeJob` 1s 轮询 GET（照抄 `followRun` 模式）+ `OptimizeProgress` 进度条；`styles.css` 加进度条样式 | 看板可见实时进度（completed/total + 百分比），M4-13 闭环；`web/dist` 被 .gitignore 忽略，产物不入仓 |
| 2026-11-03 | 分期各自通过但缺跨期联调（P1–P4 联手缺陷风险）| 新增 `tests/test_optimize_integration.py`（真实 SQLite+HTTP 全链 + 重启降级守卫）+ `web/src/api/optimize.api.test.ts`（mock fetch 契约，前后端两侧） | P1–P4 契约与边界已从前后端两侧闭环；浏览器 UI e2e 未做 |

---

## 5. 里程碑对照

| 里程碑 | 状态 | 说明 |
|---|---|---|
| MS1 数据底座 | ✅ 完成 | M2-1 ~ M2-8 |
| MS2 回测 + Webhook | ✅ 完成 | M4-1 ~ M4-5、M8-2 ~ M8-6、M7-1 ~ M7-3、M3-1 |
| MS3 模拟盘 | ✅ 完成 | M8-1、M8-4 ~ M8-6、M8-12、M8-13；完整链路已跑通 |
| MS4 监控与补齐 | ✅ 基本完成 | 首页、告警、风控、回测、批量、Pine、资金费率、执行可靠性均已落地；本表在 PRD 中仍写「进行中」 |
| MS5 收尾 | 🟡 进行中 | 12 条 ⬜ + 5 条 🟡，另有 M8-12 前端筛选待补（MS5 清单里已完成的条目见下） |
| MS6 上云 | ⬜ 未开始 | M10-6、M10-7 |
| MS7+ | ⬜ 未开始 | M5、M6 |

> **注意**：PRD §8.1 的 MS5 清单仍列着大量已完成的条目（M1-8/M1-9、M2-14、M3-3/M3-4/M3-8/M3-14 ~ M3-18、M7-6 ~ M7-17、M9-10/12/14 等）。按代码核对，MS5 真正剩余只有上表 12 条 ⬜ 与 5 条 🟡。

---

## 6. 与 PRD 状态不一致的条目

已于 2026-11-02 同步修正的 4 处（PRD §3 模块总览与 M7-17 行）：

| 编号 | 修正前 | 修正为 |
|---|---|---|
| M1 首页总览 | 🟡 部分实现 | ✅ 基本实现 |
| M7-17 风控与告警联动 | 🟡（说 halt 告警未接） | ✅（worker `_publish_halt()` 已发 `risk_halt`） |
| M8 交易执行 | 🟡（说缺仓位计算、状态机、死信重放、对账） | ✅ 基本实现 |
| M9 日志告警 | 🟡 部分实现 | ✅ 基本实现 |
| M7 风险控制（连带） | 🟡 部分实现 | ✅ 基本实现 |

仍待处理的不一致（属实现缺口，不是文档滞后）：

| 编号 | PRD 标记 | 代码实际 | 依据 |
|---|---|---|---|
| M4-6 | ✅ | 后端/CLI ✅，看板无入口 | `api.ts` 无 walk-forward 提交函数；App 仅历史标签 |
| M8-12 | ✅ | 三张表 ✅，筛选 ❌ | [app.py `GET /api/orders`](../../src/quant/dashboard/app.py#L279-L284) 只有 `limit` |

---

## 7. 建议的收尾顺序

1. **M8-12 加筛选**（低成本、高频使用）：接口补 4 个查询参数，前端加筛选条。
2. **M4-6 加 walk-forward 入口**（后端已完整，只差表单与逐段表）。
3. **M4-8 接告警**：日报 FAIL 时 `AlertService.publish`，复用 M9-11 去重。
4. **M4-13 寻优进度**（A2 范围，分阶段，**已闭环**）：`optimize_jobs` 表由 A1（v10 迁移）创建（闭环 AGENTS.md 已知问题 #1）；P1 加 save/update/finish/get 落库方法；P2 后端登记式任务 + `completed` 每 10 组合/2s 节流落 SQLite、`/api/optimize` 立即返回 job_id；P3 加 `GET /api/optimize/{job_id}` 进度查询（done 带完整结果）；P4 前端 `submitOptimize` 提交 + `followOptimizeJob` 1s 轮询 + 进度条。**仍可增强（未做）**：取消按钮（按范围未做）、结果持久化与重启恢复（`fail_interrupted_optimize_jobs`）、（可选 P5）Redis 实时轮询。
5. **M10 只读配置视图 + 凭证健康**（M10-1/M10-2/M10-9）：设置页从占位变为可用，不涉及热改风险。
6. **M3-13 双副本一致性**：解析 `.pine` 的 `input.*` 与 `PARAM_SPEC` 比对，差异进告警。
7. **M3-7 notebooks 示例**：加载本地 parquet 的最小示例即可。
8. MS6（备份/`doctor`/进程启停）与 MS7+（因子库/组合管理）按 PRD 排期推进。

---

## 8. 一句话结论

M1、M2、M7、M9 已完整可用，M3 与 M8 只剩少数收口项，M4 后端完整但前端缺 walk-forward 入口与报告导出；**M10 系统设置与 M5/M6 尚未开始**。系统在模拟盘侧已具备端到端能力，实盘化主要卡在运维动作（Key/IP/公网反代）而非代码。
