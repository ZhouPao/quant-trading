# 项目交接知识文档

> 面向接手本仓库的工程师 / AI Agent。目标：读完能**画出系统结构、找到任何功能的代码位置、知道哪些坑不能踩**。
> 生成日期：2026-11-02　对应代码：schema v9、153 条需求
> 配套文档：[AGENTS.md](../AGENTS.md)（硬规则速查）、[功能清单与完成度](requirements/2026-11-02-feature-inventory-and-completion.md)、[PRD v0.6](requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md)

---

## 1. 项目定位与边界

**是什么**：一个人用的加密货币量化交易平台。OKX 是唯一交易所，TradingView Pine 是回测主战场，Python 负责回测交叉验证、信号执行、风控、监控与看板。

**不是什么**（PRD 已明确的 Non-Goals，不要自行扩展）：

- 不做多用户/RBAC、不做对外 SaaS、不做多交易所
- 不做 tick 级回测、不做可视化策略编辑器、不自动对接 TradingView（只支持导入指标）
- 不做算法单（TWAP/冰山）、不做跨交易所路由
- 不做日志全文检索（M9-6 明确不做）、不接 Prometheus/ELK

**核心不变量**：系统的价值是「可解释 + 可复现」。任何让数字来路不明、让回测与实盘口径不一致、让同一事件写两条记录的改动，都视为错误。

---

## 2. 运行形态

### 2.1 七个常驻进程

| 进程 | 命令 | 端口/存储 | 职责 |
|---|---|---|---|
| supervisor | `quant supervisor` | Redis `quant:process:supervisor` | 拉起并守护其余进程，指数退避重启（2/4/8…封顶 60s，稳定 60s 后重置） |
| candles | `quant sync-candles` | `data/candles/` | 增量同步 K 线，顺带补缺口 |
| webhook | `quant serve-webhook` | `:8000` `/webhook/tv` | 接收 TradingView alert，5 秒 ACK，解析失败入死信 |
| worker | `quant run-worker --ws` | Redis 队列 + SQLite | 消费信号 → 仓位计算 → 风控 → 预检 → 下单 → 落库；启动先 `recover_inflight` 再 `reconcile` |
| engine | `quant run-engine` | Redis 状态键 | 本地策略引擎，逐绑定评估已收盘 bar，直接产信号（不依赖 TradingView） |
| monitor | `quant monitor` | Redis 快照 + SQLite | 账户/持仓/队列快照、12 条告警规则、日报、保留清理、存活邮件 |
| dashboard | `quant dashboard` | `:8080` | 只读看板 API + 静态页（`web/dist`），写操作有守卫 |

前端产物：`cd web && npm run build` → `web/dist`，由 dashboard 挂载。

### 2.2 数据流

```
行情：OKX REST ──> fetcher ──> DataValidator/Quality ──> data/candles/**.parquet
                                             └──────> data/quality/**.json ──> 看板「数据质量」

信号：TradingView ──> webhook(:8000) ──> Redis quant:signals
      engine(live.py) ──────────────────> Redis quant:signals
                                   │
                                   ▼
                        worker（BRPOPLPUSH → inflight）
                                   │
                    PositionSizer → RiskChecker → PreflightChecker
                                   │
                              OkxBroker.execute
                                   │
                 signals/orders/fills/positions 落 SQLite
                                   │
                         Reconcile（启动 + 周期）
                                   │
                       monitor 快照 / 告警 / 日报 ──> dashboard

回测：dashboard tasks.py / CLI ──> Backtest / GridOptimizer / WalkForward / BatchSweep
                                   └──> backtest_runs + backtest_metrics + data/backtests/{run_id}/*.parquet
```

### 2.3 回测任务模型

- 提交即登记 `backtest_runs`（`status=pending`）并返回 `run_id`，HTTP 立刻返回
- `dashboard/tasks.py` 单线程 + FIFO，同时只跑 1 个重任务（CPU 密集）
- 状态：`pending → running → done / failed / canceled`；`running` 取消时在下一根 bar 退出
- `kind`：`single` / `optimize` / `walk_forward` / `batch`；`rerun` 按 `kind` 分支
- 产物：`equity.parquet` `drawdown.parquet` `trades.parquet`、`segments.parquet`（walk-forward）、`batch.parquet`（batch）、`report.html`（可选）

---

## 3. 代码地图

```
src/quant/
├── foundation/     配置(Settings)、日志(脱敏)、错误、心跳、时间口径
├── models/         Bar / Tick / Instrument / FundingRate / OpenInterest（不可变 dataclass）
├── okx/            签名、REST 客户端、限频令牌桶、WS(tickers)、实盘自检(live_check)
├── fetcher/        拉取编排：K线(翻页+断点)、产品、资金费率(实时+历史)、持仓量
├── storage/        parquet 读写、校验、质量报告、多品种对齐、保留清理
├── backtest/       engine(主循环) / matcher(成交) / metrics / report / optimizer / walkforward / batch / funding / pine_compare
├── strategies/     loader(读 toml) / params(PARAM_SPEC 校验) / bindings(启停) / python/*.py / pine/*.pine
├── engine/         live.py：实盘/模拟盘逐绑定评估
├── risk/           checker(6 规则) / limits(合并) / events / overrides(在线阈值) / circuit_breaker / preflight / pnl
├── webhook/        server / auth(三处 token) / parser
├── queue/          Redis 信号队列、去重
├── execution/      worker(主流程) / broker / okx_broker / executor / sizing / order_state / dead_letter / reconciler / kill_switch
├── monitor/        monitor(轮询) / snapshot / alert_service / alerter(邮件)
├── dashboard/      app(路由) / service(聚合) / tasks(回测任务) / backtest(参数校验) / manual(手工单)
├── state/          sqlite_store(schema+全部读写) / order_state(状态机)
├── report/         daily(模拟盘偏离日报)
├── analysis/       deviation(成交对比)
├── indicators/     support_resistance（注意：不是因子框架）
└── cli/            23 个子命令
```

前端：

```
web/src/
├── App.tsx         所有页面组件与 10 个 Tab（单文件很大，按函数名定位）
├── api.ts          全部请求封装 + 指标格式化（METRIC_*、metricText、metricTone）
├── chart/          CandleChart / IndicatorBar / indicators.ts / options.ts / settings.ts
└── styles.css
```

**定位技巧**：看板某卡片出问题，先在 `api.ts` 找请求函数 → 找对应 `/api/...` → 在 `app.py` 找路由 → 进 `service.py` 找组装逻辑 → 进 `sqlite_store.py` 看 SQL。

---

## 4. 存储

### 4.1 SQLite（`data/state/quant.db`，schema v9，17 张表）

| 分组 | 表 |
|---|---|
| 交易 | `signals` `orders` `fills` `positions` `equity_snapshots` |
| 风控 | `risk_events` `risk_overrides` |
| 告警 | `alerts` `alert_silences` |
| 策略 | `strategy_params` `strategy_param_changes` `strategy_bindings` |
| 回测 | `backtest_runs` `backtest_metrics` `backtest_artifacts` |
| 可靠性 | `dead_letters` `reconciliations` |

**迁移机制**：`sqlite_store.py` 里 `_SCHEMA`（v1 基线）+ `_MIGRATIONS: list[str]`，启动时按 `PRAGMA user_version` 逐段执行，`SCHEMA_VERSION = 1 + len(_MIGRATIONS)`。

新增列/表的标准做法：

```python
# 1) 在 _MIGRATIONS 末尾追加一段（不要改历史语句）
"""
ALTER TABLE xxx ADD COLUMN yyy TEXT;
""",
# 2) 若该列参与指标落库，同步 _METRIC_COLUMNS
# 3) 补一条「存量库升级」测试：手建旧版表 + user_version=N，打开后断言新列存在且老数据不丢
```

**已知限制**：人工把 `user_version` 回退后重启会因列已存在而迁移失败；正常升级路径不受影响。

### 4.2 文件布局

```
data/
├── candles/{inst_type}/{inst_id}/{bar_size}/{year}.parquet   # zstd, 按 UTC 年分片, 同 ts 保留最新
├── funding_rates/{inst}.json                                 # 按 ts 去重
├── open_interest/{inst}.json
├── instruments/*.json
├── quality/{inst}/{bar}.json                                 # 最近一次校验报告（覆盖写）
├── .checkpoints/{inst}_{bar}.json                            # 断点 last_ts
├── backtests/{run_id}/{equity,drawdown,trades,segments,batch}.parquet + report.html
└── state/quant.db
```

规则：K 线只读**已收盘** bar（`confirm=1`）；字段 snake_case，与 OKX 原始字段一一对应（`vol_quote ↔ volCcyQuote`）。

### 4.3 Redis 键

| 键 | 类型 | 用途 |
|---|---|---|
| `quant:signals` / `:inflight` / `:dead` | LIST | 待处理 / 处理中 / 死信 |
| `quant:dedup:{hash}` | STRING+TTL 300s | 信号去重 |
| `quant:kill-switch` | STRING | 全局紧急停止 |
| `quant:risk:circuit:{inst}` | STRING | 单品种熔断（默认不设 TTL，待人工确认） |
| `quant:risk:overrides` | HASH `{strategy}:{param}` | 在线阈值覆盖（唯一生效源） |
| `quant:risk:limits:{strategy}` | STRING | worker 发布的生效限额（只读） |
| `quant:heartbeat:{name}` / `quant:process:{name}` | STRING | 进程心跳 / pid·重启次数 |
| `quant:heartbeat:monitor:last` | STRING（无 TTL） | 上次心跳，用于检测长停机 |
| `quant:ws:status:worker` | STRING | WS 连接状态与连续失败次数（TTL 同心跳） |
| `quant:engine:status` / `quant:engine:last-bar` | STRING | engine 状态与水位 |
| `quant:strategy:bindings` | HASH | 启停矩阵缓存 |
| `quant:dashboard:snapshot` | STRING | monitor 账户快照 |
| `quant:dashboard:alerts` | LIST | 告警实时缓存（权威在 SQLite） |
| `quant:monitor:alive-last` / `quant:retention:last` / `quant:daily-report:last` | STRING | 去重时间戳 |

---

## 5. 核心链路细节

### 5.1 信号 → 订单（`execution/worker.py`）

1. `BRPOPLPUSH quant:signals → quant:signals:inflight`（先移入 inflight，崩溃可恢复）
2. `hash(signal_json)` + SETNX 去重
3. 按 `strategy_id` 取该策略的 `RiskChecker` 与生效限额；**未声明的策略直接进死信（stage=route），绝不用别的策略阈值下单**
4. `PositionSizer`（仅 `qty` 缺省时）：`权益 × 比例 ÷ 价格` → `max_order_qty` 截断 → 按 `lotSz` 向下取整；合约按 `ctVal` 折张数，`minSz` 比张数；写 `decision="sized"` 的 `risk_events`
5. `RiskChecker.check()` → 六条规则；`halt` 类直接拒单并发布 `risk_halt` 告警 + 启用 kill switch
6. `PreflightChecker`（仅直连 OKX 且开启时）：产品配置、`lotSz` 精度、`minSz`、最小名义金额、现货可用余额（合约跳过余额校验）
7. `OkxBroker.execute()`：`clOrdId` 由 `signal_id` 派生；`NetworkError` 先按 `clOrdId` 查，查到复用，查不到才退避重试（≤3 次）
8. `update_order_fill` 按状态机推进（`pending → live → partially_filled → filled/canceled`，禁回退）
9. 写 `signals/orders/fills/positions`，移出 inflight

**风控规则顺序（固定）**：日亏损 → 连亏 → 熔断（硬停止）→ 单笔名义价值 → 单品种比例 → 总仓位比例（后三条可减量）。

### 5.2 回测（`backtest/engine.py`）

- 事件驱动逐 bar 回放；本根收盘出信号，**下一根开盘成交**；`protective_exit` 允许 bar 内成交；滑点 `slippage_bps`
- 权益口径：`equity = cash + qty × close` 逐根记录；年化 `(1+r)^(365/时长)-1`；夏普用日收益 `mean/std × sqrt(365)`
- 资金费率（`funding.py`）：结算点若已持仓则 `cash -= rate × qty × 结算价`；qty 带符号；结算价用该 bar 开盘价近似；同一根 bar 的结算发生在成交之前
- 三条扩展路径必须透传同一份 `FundingSchedule` 与参数：`GridOptimizer`（含进程池 worker）、`WalkForward`、`BatchSweep`
- walk-forward：IS 选参、OOS 只评估；OOS 数据不得回流影响选参（有单测固定）
- 批量：同一 `(品种, 周期)` 的 K 线只读一次并缓存；单格失败不中断，全部失败才报错

### 5.3 告警（`monitor/alert_service.py`）

```
触发方 → publish
  → 查静音（规则/规则+品种/全局 rule='*'）
  → 查冷却（同规则 600s；kill_switch 与 INFO 恢复通知不参与）
  → 落 SQLite alerts（权威）
  → 推 Redis 缓存
  → 邮件（enabled 且非 silenced）
  → 日志
```

已接业务事件：熔断、`risk_halt`、`backtest_failed`、`reconcile_mismatch`；死信沿用 monitor 的 `dead_letter` 计数。

### 5.4 看板前后端契约

- 写接口：本机 `Host` + `X-Dashboard-Request: 1`（可选 token）；前端所有写请求走 `writeToken()`
- 每个聚合接口返回 `degraded: string[]`；卡片必须显示「数据截至 HH:MM」与降级项，**不允许静默当 0**
- 时间：后端存 UTC；前端 `clock()`/`stamp()` 按浏览器本地时区格式化
- 指标：`METRIC_LABELS`（文案）、`METRIC_ORDER`（卡片顺序）、`METRIC_HINTS`（提示）、`metricText`（格式化）、`metricTone`（颜色）五处要同步；金额类在 `AMOUNT_METRICS` 里按 4 位小数
- 参数写入：`PUT /api/strategies/{id}/params` 会校验 `PARAM_SPEC` 的 type/min/max/choices；未声明 `PARAM_SPEC` 的策略只读

---

## 6. 配置

配置文件 `config/config.toml`（不入 git），语法支持 `${ENV}` 与 `${ENV:-default}`。

| 段 | 关键项 |
|---|---|
| `[okx]` | `demo`（true=模拟盘）、三件套走环境变量 |
| `[storage]` | `data_dir`、`retention_days/enabled/at`、`dead_letter_persist` |
| `[redis]` | `url`、队列键、`dedup_ttl_seconds` |
| `[webhook]` | `host/port/path`、`auth_token`、`hmac_secret`、`ack_timeout_seconds` |
| `[backtest]` | 初始资金、费率、滑点、`window_size`、`task_workers`、`max_combos`、`funding_rate_enabled` |
| `[params]` | `resolve_from_db`（回滚开关：关掉只看 toml） |
| `[state]` | `db_path` |
| `[risk]` | 全局默认阈值、`snapshot_max_age_ms`、`circuit_*`、`preflight_*` |
| `[execution]` | 对账间隔、挂单超时、`server_side_sizing`、`default_size_pct` |
| `[candles]` / `[quality]` | 增量同步与校验窗口 |
| `[engine]` | `strategy/inst/bar`、`warmup_bars`、`multi_binding`、`bindings_fail_mode` |
| `[dashboard]` | `host/port/web_dir`、`token`（示例文件未列出，默认空=只允许本机） |
| `[monitor]` | `alert_*`、`disabled_rules`、`risk_limits_key/risk_overrides_key` |
| `[report]` | 日报时间、阈值、输出目录、邮件开关 |

### 6.3 策略 toml（`config/strategies/<id>.toml`，不入 git）

仓库内已提供两份模板：`sma_cross.toml.example`、`rsi.toml.example`，复制为同名 `.toml` 即可使用。

```toml
[strategy]
class = "quant.strategies.python.sma_cross.SmaCross"

[params]
fast_len = 10
slow_len = 100

[risk]            # 可选，与全局 [risk] 取严（取更小）
max_position_pct = 0.3
```

策略类需声明 `PARAM_SPEC: dict[str, ParamSpec]` 才可在线编辑参数；回调为 `on_init / on_bar / on_trade / protective_exit / on_finish`。

---

## 7. 已知缺口与陷阱

### 7.1 真实缺陷（优先修）

| 问题 | 根因 | 修法 |
|---|---|---|
| 无 `optimize_jobs` 表 | 只有注释，没有迁移 | 做 M4-13 时补迁移 + 进度列 |

> 2026-11-02 已修：`POST /api/risk/overrides` 500（`RiskOverrideRequest` 缺 `by` 字段）。
> 模型补 `by: str = "dashboard"` 后，`tests/test_risk_overrides.py` 24 条全通过，全量 1644 passed。

### 7.2 设计取舍（不要「顺手优化」）

| 取舍 | 原因 |
|---|---|
| 批量回测的汇总 `metrics` 故意留空 | 批量没有单一代表指标，塞某个格子的数字会让历史列表出现来路不明的值 |
| 熔断键默认不设 TTL | 冷却结束需人工确认；`circuit_resume_requires_confirm=false` 才自动恢复 |
| 现货跳过余额预检、合约不跳过 | 杠杆下的可用保证金无法由本地余额推导，交给交易所 51008 |
| 本地持仓多于交易所才自动下调 | 否则卖出会因可用量不足被拒；本地少于交易所（外部转入）一律人工确认 |
| `max_order_qty` 实际是名义价值（USDT） | 历史字段名，不要再按数量理解 |
| 未声明策略的信号进死信而非拒单 | 补齐配置后可重放，不丢信号 |
| Python 回测不做 tick 级 | 与 OKX 分钟级数据能力匹配，避免假精度 |

### 7.3 容易踩的实现坑

1. **改 v5 建表语句** → 存量库永远缺列。只能追加迁移。
2. **忘了 `_METRIC_COLUMNS`** → 指标写库静默丢弃，页面永远看不到。
3. **新增参数只改了单次回测** → 寻优/walk-forward/批量口径不一致，对比失真。
4. **前端新增请求忘了 `writeToken()`** → 400 `missing X-Dashboard-Request header`。
5. **读取失败不标 `degraded`** → 页面显示 0，误判为「今天没交易」。
6. **金额用 float** → 精度丢失；SQLite 金额列是 TEXT 的 Decimal 字符串。
7. **把 `tests/smoke/` 当 pytest 用例** → 它们没有 `test_` 函数，跑 pytest 不会执行。
8. **全仓 ruff 格式化** → 会碰到既有的 `test_open_interest_fetcher.py` W293，制造无关 diff。

---

## 8. 当前状态（2026-11-02）

| 模块 | 状态 | 说明 |
|---|---|---|
| M1 首页总览 | ✅ | 10 条全落地 |
| M2 市场监控 | ✅ | 14 条落地，自选对比明确不做 |
| M3 策略研究 | 🟡 | 缺 notebooks、双副本一致性；信号预览仅粗粒度 |
| M4 回测分析 | 🟡 | 后端完整；缺 walk-forward 看板入口、报告导出、寻优进度 |
| M5 因子库 | ⬜ | 未开始（MS7+） |
| M6 组合管理 | ⬜ | 未开始（MS7+） |
| M7 风险控制 | ✅ | 17 条全落地 |
| M8 交易执行 | ✅ | 代码侧完成；实盘 Key/公网反代属运维；订单/成交缺筛选 |
| M9 日志告警 | ✅ | 13 条落地，日志检索明确不做 |
| M10 系统设置 | 🟡 | 仅保留/时间/访问控制可用，其余 12 条未开始 |

**下一步建议**（按投入产出）：

1. 修 §7.1 的 `by` 字段 bug
2. M8-12 订单/成交筛选
3. M4-6 walk-forward 看板入口
4. M4-8 日报 FAIL 接告警
5. M4-13 寻优进度（含 `optimize_jobs` 迁移）
6. M10-1/2/9 只读配置视图 + 凭证健康
7. M3-13 双副本一致性 → M3-7 notebooks 示例
8. MS6（备份/`doctor`/进程启停）、MS7+（因子库/组合管理）

依据与逐条明细见 [功能清单与完成度](requirements/2026-11-02-feature-inventory-and-completion.md)。

---

## 9. 一次典型改动的完整清单

以「给执行页加订单筛选」为例，确认你没有漏掉任何接线点：

1. **存储层**：`sqlite_store.py` 的查询方法加 `inst/strategy/status/source` 过滤（参数化 SQL，不要拼串）
2. **服务层**：`dashboard/service.py` 透传参数
3. **路由层**：`dashboard/app.py` 用 `Query(None)` 接收，注意 `MAX_LIMIT`
4. **前端 API**：`web/src/api.ts` 加参数
5. **前端页面**：`App.tsx` 的 `OrdersPanel` 加筛选条（受控状态 + 重置）
6. **测试**：`tests/test_dashboard.py` 或 `test_dashboard_backtest.py` 覆盖「筛选命中」与「无匹配」
7. **校验**：ruff + pytest + `npm run typecheck` + `npm run build`
8. **文档**：PRD 对应条目补说明 + 变更记录追加一行；清单文档改状态

---

## 10. 交给下一位之前的环境自查

```bash
redis-cli ping                      # PONG
quant kill-switch --status          # 应为关闭
ls config/config.toml               # 存在且 [okx] demo 符合预期
ls config/strategies/*.toml         # 至少一个策略
ls web/dist/index.html              # 前端已构建
quant check-live                    # 若 demo=false，必须无 [FAIL]
python3 -m pytest -q                # 1644 passed / 0 failed
```

数据核对（可选，验证链路是否真通）：

```bash
quant backtest --strategy sma_cross --inst BTC-USDT --bar 1H \
  --start 2026-10-01 --end 2026-10-31 --funding
# 详情页应与 SQLite backtest_metrics.funding_cost 一致（同一 run_id）
```
