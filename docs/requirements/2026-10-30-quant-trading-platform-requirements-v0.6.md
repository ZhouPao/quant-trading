# 量化交易平台需求文档（PRD）v0.6

> 文档版本：v0.6（按产品模块重构：10 个模块，替代 v0.5 的 F1–F5 横向功能划分）
> 创建日期：2026-10-30
> 上一版本：[v0.5](2026-10-17-quant-trading-platform-requirements.md)（保留作历史，不再维护）
> 文档所有者：用户本人
> 适用范围：个人量化交易工具，本机单用户，不涉及对外服务

---

## 1. 项目概述

### 1.1 项目定位

面向个人使用的加密货币量化交易平台，覆盖「数据 → 研究 → 回测 → 执行 → 监控 → 复盘」全链路。

- **执行层**：在本机常驻 7 个进程（supervisor / candles / webhook / worker / engine / monitor / dashboard），通过 OKX API 拉行情、下单、落库。
- **研究层**：Python 策略 + 双回测（TradingView Pine 为主、Python 回测做交叉验证）。
- **展示层**：本机 Web 看板（React + ECharts），按 10 个模块组织页面。
- **边界**：仅本人使用，不做注册登录、权限体系、多租户、对外 API 售卖、券商/股票/外汇接入。

### 1.2 项目目标

| 优先级 | 目标 | 衡量标准 |
|---|---|---|
| P0 | 从 OKX 稳定采集行情与历史数据 | 单次可拉 1 年以上 1H K 线，无缺字段、无断点 |
| P0 | 历史数据上完成策略回测并输出绩效 | 夏普/最大回撤/胜率等指标齐全；与 Pine 回测偏离度可量化 |
| P0 | Python 端提供 webhook server 接收 TradingView alert | 5 秒内 ACK；信号不丢；重复信号可去重 |
| P0 | 看板可查看账户、持仓、成交、回测结果 | 打开看板 3 秒内出首屏数据 |
| P1 | OKX 模拟盘跑通完整执行链路 | 与回测结果偏离度可量化 |
| P1 | OKX 实盘小资金（< 1000 USDT）自动执行 | 7×24 稳定运行，异常自动告警 |
| P2 | 因子库与组合管理 | 支撑多因子研究与多策略资金分配 |

### 1.3 非目标（Non-Goals）

- 不做多用户、注册登录、角色权限、租户隔离
- 不做对外服务的 API / 数据售卖
- 不接券商、股票、外汇、期货（OKX 之外的交易所）
- 不做高频交易（HFT）级低延迟优化
- 不做策略商城、跟单、社区等社交功能
- 不自研回测引擎替代 Pine；Pine 是回测主战场，Python 回测做交叉验证
- 不做移动端 App；看板只保证桌面浏览器可用

> v0.5 中「不做 Web UI 界面」一条已废止：本项目已包含本机 Web 看板，且 v0.6 的模块结构就是围绕看板页面组织的。

### 1.4 假设与依赖

**假设**

- OKX API v5 未来 1 年无破坏性变更
- 模拟盘与实盘行情延迟 < 1 秒
- 用户已持有 OKX 账户、通过 KYC、可创建 API Key
- 用户将在实盘启动前开通 TradingView Pro（alert webhook 为 Pro 功能）
- 本机可长时运行（模拟盘阶段）；实盘阶段迁移到 VPS
- TradingView alert webhook 可用性符合其 SLA（一般 > 99.5%）

**依赖**

- OKX REST 与 WebSocket 服务可用
- Python 3.13 运行环境与 pip 生态
- Redis（队列、去重、引擎水位）
- SQLite（订单/成交/持仓/信号/死信）
- 本地或云服务器稳定网络
- TradingView alert webhook
- supervisor 或 systemd 做进程托管（当前为 supervisor，上云后改 systemd）

**假设变更触发条件**：任一假设被证伪（如 OKX 发布 v6、API 限频大幅收紧）时暂停开发并修订本文档。

---

## 2. 用户与场景

### 2.1 用户画像

仅一人使用：会 Python（中阶）、熟悉加密货币基本概念、以 OKX 为主交易所、对量化交易有学习意愿但尚未稳定盈利。

### 2.2 页面导航视角

看板左侧导航即 10 个模块，一一对应第 4 章的需求分节：

```
首页总览 / 市场监控 / 策略研究 / 回测分析 / 因子库
组合管理 / 风险控制 / 交易执行 / 日志告警 / 系统设置
```

### 2.3 核心使用场景

| 场景 | 走哪些模块 | 频率 |
|---|---|---|
| 每天开盘看一眼 | 首页总览 → 日志告警 | 每日 |
| 看行情、翻 K 线 | 市场监控 | 每日 |
| 改策略参数、试信号 | 策略研究 | 开发期高频 |
| 跑回测、比参数 | 回测分析 | 每策略上线前 |
| 研究因子 | 因子库 | 按需 |
| 配多策略资金 | 组合管理 | 按需 |
| 设风控阈值、紧急停止 | 风险控制 | 上线前/应急 |
| 查订单成交、手动平仓 | 交易执行 | 每日 |
| 查告警、翻日志 | 日志告警 | 异常时 |
| 改配置、改凭证 | 系统设置 | 低频 |

---

## 3. 模块总览

| # | 模块 | 一句话职责 | 状态 |
|---|---|---|---|
| 1 | 首页总览 | 账户/持仓/风险/权益、7 进程健康、今日事件流、数据新鲜度 | 🟡 部分实现 |
| 2 | 市场监控 | 多周期 K 线下载与校验、tickers 实时行情、技术指标图表 | ✅ 基本实现 |
| 3 | 策略研究 | 策略登记、PARAM_SPEC 参数编辑、启停矩阵、双副本一致性 | 🟡 部分实现 |
| 4 | 回测分析 | 回测、寻优、walk-forward、结果持久化与对比 | ✅ 基本实现 |
| 5 | 因子库 | 因子注册、时序/横截面计算与缓存、有效性检验 | ⬜ 未开始 |
| 6 | 组合管理 | 多策略×多品种的权重、资金分配、再平衡、归因 | ⬜ 未开始 |
| 7 | 风险控制 | 两层参数、六条规则、在线阈值覆盖、熔断与 kill switch | 🟡 部分实现 |
| 8 | 交易执行 | webhook → 队列 → 下单 → 对账 → 审计；含死信与手工交易 | 🟡 部分实现（缺仓位计算、状态机、死信重放、对账） |
| 9 | 日志告警 | 单文件日志、11 条告警规则、SQLite 主存、已读与静音 | 🟡 部分实现 |
| 10 | 系统设置 | 配置只读与白名单热改、凭证健康、保留分档、进程控制、备份自检 | 🟡 部分实现 |

**编号规则**：`M<模块号>-<序号>`（如 `M2-7`）。每条需求附「来源」列标注 v0.5 的原编号，便于追溯；`新增` 表示 v0.6 首次提出。

**与里程碑的区分**：`M1`–`M10` 只表示**模块**；**里程碑**统一写作 `MS1`–`MS6`（第 8 章），避免与模块编号混淆。

**状态定义**：✅ 已实现且在看板可用；🟡 后端已具备但看板未完全呈现，或仅覆盖部分场景；⬜ 未开始；🚫 明确不做（本期范围外，避免反复讨论）。

---

## 4. 模块详细需求

### 4.1 首页总览（M1）

**职责**：打开看板第一屏，回答「账户现在怎么样、有没有异常」。只做聚合展示，不产生写操作。

**边界**：不新增计算逻辑。数据来自 monitor 快照（Redis）、SQLite 各业务表、engine 状态键与本地 parquet；数据缺失时必须显式降级标注，不允许静默显示为正常。

#### 4.1.1 数据视图

以下均为**只读聚合视图**，不新增 SQLite 表。

| 视图 | 关键字段 | 来源 |
|---|---|---|
| `OverviewSnapshot` | `account` / `metrics` / `queue` / `kill_switch` / `degraded` | monitor 每轮发布的快照（Redis `quant:dashboard:snapshot`） |
| `ProcessHealth` | `name` / `pid` / `heartbeat_ts` / `restarts` / `status` | Redis `quant:heartbeat:{name}`、`quant:process:{name}` |
| `DailyTimelineItem` | `ts` / `kind` / `title` / `detail` / `level` / `inst_id` / `strategy_id` | `signals` / `orders` / `fills` / `alerts` 四表 UNION ALL |
| `DataFreshness` | `inst_id` / `bar` / `last_bar_ts` / `lag_seconds` / `lag_bars` / `stale` | 本地 parquet 最新已收盘 bar + engine 水位 |

#### 4.1.2 存储

- **不新增 SQLite 表**；事件流用四表 `UNION ALL` 按 `ts` 倒序查询，查询时聚合，不双写
- **新增 Redis 键**：`quant:heartbeat:{name}`（进程心跳，TTL 为 2 倍轮询间隔，过期即视为异常）、`quant:process:{name}`（pid / started_at / restarts，由 supervisor 写入）
- **权益快照**：沿用 `equity_snapshots`，默认保留 365 天，30 天档无需额外调整

#### 4.1.3 接口

| 接口 | 变更 | 说明 |
|---|---|---|
| `GET /api/overview` | **扩展** `processes[]` | 返回 7 个进程的健康状态；原字段不变 |
| `GET /api/timeline` | 新增 | 参数 `date`（默认今天）/ `kinds`（signal,order,fill,alert）/ `limit`；四表合并后按时间倒序 |
| `GET /api/freshness` | 新增 | 只返回引擎实际消费的 `inst × bar` 组合的新鲜度，复用 `/api/engine` 的 `lag_bars` 计算 |
| `GET /api/equity` | 不变 | `hours` 上限 720（30 天），前端提供 24h / 7d / 30d 三档 |

#### 4.1.4 需求条目

| 编号 | 功能项 | 详细要求 | 来源 | 状态 |
|---|---|---|---|---|
| M1-1 | 账户总览卡 | 余额、可用、冻结、未实现盈亏、保证金占用率；数据来自 `/api/overview` 的 `account` | F5.1 | ✅ |
| M1-2 | 持仓摘要 | 各品种方向、数量、开仓均价、未实现盈亏、杠杆；一行一品种，可跳转交易执行 | F5.2 | ✅ |
| M1-3 | 风险摘要 | 保证金率、实时最大回撤、强平价距离、撤单率；超标项用颜色区分 | F5.3 | ✅ |
| M1-4 | 权益曲线 | 近 24 小时 / 7 天 / 30 天三档切换（默认 24h）；数据来自 `equity_snapshots`，保留 365 天 | 新增 | ✅ |
| M1-5 | 进程与引擎健康 | **7 个进程各自心跳**（supervisor/candles/webhook/worker/engine/monitor/dashboard）+ engine 当前周期与最近评估时间；任一进程心跳过期即标红。看板「进程健康」卡按 `/api/processes` 呈现 7 进程在线/心跳/pid/重启次数，并附 engine 当前策略·品种·周期、最近评估与落后根数 | 新增 | ✅ |
| M1-6 | 今日事件流 | 当日信号/下单/成交/告警按时间倒序合并成一条流；支持按类型筛选；点击可跳到对应模块 | 新增 | ⬜ |
| M1-7 | 数据新鲜度 | 引擎消费的 `品种 × 周期` 最新已收盘 bar 时间与滞后根数；超过阈值标红并提示「engine 落后 N 根」。**已实现 `GET /api/freshness`**：`lag_bars` 复用 engine 口径，另加 `missing_bars`（本地最新 bar 相对「此刻应有 bar」的缺口，用于识别 candles 与 engine 一起卡住时 `lag_bars=0` 的假同步）；阈值 1 根，`stale` 为两种落后取或；看板首页「数据新鲜度」卡分别提示「engine 落后 N 根」「本地缺 N 根」。当前只返回 engine 主组合，多绑定逐组合待 engine 发布各自水位 | 新增 | ✅ |
| M1-8 | 今日盈亏汇总 | 当日已实现盈亏 + 未实现盈亏变化 + 手续费，按品种拆分；口径与 M4 回测指标一致 | 新增 | ⬜ |
| M1-9 | 告警摘要卡 | 未读 ERROR / WARNING 计数，按规则分组；点击跳转告警列表（M9-5）；已读状态由 M9-7 提供。看板已有「告警摘要」卡（按级别计数 + Top5 规则，点击跳日志告警），**未读状态仍依赖 M9-7** | 新增 | 🟡 |
| M1-10 | 降级与时效标注 | 任一数据源读取失败或键过期，卡片显示「数据截至 HH:MM」并列出降级项；不允许把缺失静默渲染成 0 或「—」 | 新增 | 🟡 |

**进程心跳规范**：每个进程启动时写 `quant:process:{name}`（pid、started_at），运行期间按各自轮询间隔刷新 `quant:heartbeat:{name}`（TTL = 2 × 间隔）。dashboard 读取 7 个键，缺失或过期即 `status=down`；supervisor 额外记录 `restarts` 计数供排查重启风暴。

**不做**：自定义卡片布局/拖拽、多账户切换、首页写操作（平仓、kill switch 均在对应模块）、图表导出。

**前置条件**：M1-5 需 supervisor 与 candles/webhook/dashboard 补写心跳（当前仅 worker/monitor 有）；M1-9 依赖 M9-7 的已读状态；M1-6 依赖四表均带 `ts` 索引。

**验收**：断网或某进程死亡时，M1-5 必须在 2 分钟内变为异常色；其余卡片保留最后一次成功数据并标注「数据截至」；四表事件流查询在 200ms 内返回当日数据；引擎落后超过 1 根时 M1-7 明确标红。

---

### 4.2 市场监控（M2）

**职责**：把 OKX 行情稳定取回来、正确落盘、并在看板上可读。

**边界**：只管「行情数据的获取、存储、校验与展示」。策略如何消费行情归 M3，下单用价归 M8。落盘字段统一 snake_case，但须保持与 OKX 原始字段名的一一对应（如 `vol_quote` ↔ `volCcyQuote`），保证可回溯。

#### 4.2.1 数据模型

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `Bar` | `ts` / `inst_id` / `bar_size` / `open` / `high` / `low` / `close` / `vol` / `vol_quote` / `confirm` | 已有，保持不变；`vol_quote` 为计价币成交额（OKX `volCcyQuote`），`confirm=0` 表示未收盘 |
| `Tick` | `ts` / `inst_id` / `bid_px` / `bid_sz` / `ask_px` / `ask_sz` / `last_px` / `last_sz` | 已有；**每品种只保留最新一条**，不做历史缓存 |
| `Instrument` | `inst_id` / `inst_type` / `base_ccy` / `quote_ccy` / `settle_ccy` / `tick_sz` / `lot_sz` / `min_sz` / `ct_val` / `state` / `list_time` | 已有，字段名已转 snake_case；`ct_val` 是合约面值，M8 张数换算依赖它 |
| `FundingRate` | `inst_id` / `funding_rate` / `funding_ts` / `next_funding_ts` | 已有，保持不变 |
| `DataQualityReport` | `inst_id` / `bar_size` / `checked_at` / `total_bars` / `missing_ranges[]` / `repaired` / `issues[]` | **新增**；把当前只打日志的校验结果结构化，供看板与告警消费 |

#### 4.2.2 存储

| 数据 | 路径 | 说明 |
|---|---|---|
| K 线 | `data/candles/{inst_type}/{inst_id}/{bar_size}/{year}.parquet` | 按 UTC 年份分文件，同时间戳去重保留最新 |
| 资金费率 | `data/funding_rates/{inst_id}.json` | 按 `ts` 去重追加 |
| 产品配置 | `data/instruments/*.json` | 按 `inst_type` 分文件 |
| **校验报告** | `data/quality/{inst_id}/{bar_size}.json` | **新增**；覆盖写入最近一次结果，非追加 |
| 断点 | `data/.checkpoints/{inst_id}_{bar_size}.json` | 记录 `last_ts` 供断点续传 |

#### 4.2.3 接口

| 接口 | 变更 | 说明 |
|---|---|---|
| `GET /api/candles` | 不变 | 参数 `inst` / `bar` / `limit` |
| `GET /api/quality` | **新增** | 返回最近一次校验报告：缺失段、已修复段、未修复项 |
| `WSClient` | **内部改动** | 重连退避加 ±20% 抖动（5s→60s），连续失败次数与最后错误时间写 Redis，供 M1-5 展示 |
| `DataValidator` | **签名扩展** | 返回值由「缺失段列表」改为 `DataQualityReport`；原有调用方写法保持兼容 |

#### 4.2.4 需求条目

| 编号 | 功能项 | 详细要求 | 来源 | 状态 |
|---|---|---|---|---|
| M2-1 | 历史 K 线下载 | 指定品种（现货/永续）、周期、时间范围；`history-candles` 自动翻页拉旧数据；按 `.checkpoints` 断点续传 | F1.1 | ✅ |
| M2-2 | 实时行情订阅 | **仅订阅 `tickers` 频道**，产出 `Tick`；每品种只保留最新一条（不缓存历史）；自动重连带 ±20% 抖动，退避 5s→60s；连接状态与连续失败次数上报 Redis | F1.2 | 🟡 |
| M2-3 | 产品配置拉取 | 拉取全部交易对 `instruments`（`tickSz`/`lotSz`/`minSz` 等）；模拟盘与实盘 `instIdCode` 独立拉取；USD 现货已迁 USDC | F1.3 | ✅ |
| M2-4 | 资金费率 | 定时拉取当前资金费率并按 `ts` 去重追加到 `data/funding_rates/{inst_id}.json`，供手续费估算与持仓成本参考。**现状只取最新一条（`/public/funding-rate`），未接 `/public/funding-rate-history`，因此无法回补历史区间**，M4-15 的资金费率成本回测需先补该能力，故为 🟡 | F1.4 | 🟡 |
| M2-5 | 数据存储 | K 线按「品种/周期/年份」分 parquet 落盘；产品配置存 JSON。**落盘字段统一转 snake_case**（`volCcyQuote` → `vol_quote`），不保留 OKX 原始驼峰名，避免下游到处做名称映射 | F1.5 | ✅ |
| M2-6 | 限频管理 | 按端点组令牌桶：公开行情 20req/2s、账户 10req/2s、单下 60/批下 300/撤单 60req/2s；命中 `50011` 睡 2 秒重试 | F1.6 | ✅ |
| M2-7 | 数据完整性校验 | 按周期校验时间戳连续性；缺失段自动补拉（最多 3 次）；重复时间戳保留最新；校验 OHLC 合法性；结果输出为 `DataQualityReport` | F1.7 | ✅ |
| M2-8 | 多品种对齐 | 统一时间轴对齐多品种；缺失行按「ffill/NaN/插值」可配置；输出多列 DataFrame 供回测与因子使用 | F1.8 | ✅ |
| M2-9 | K 线图表页 | ECharts 蜡烛图 + 成交量 pane；支持缩放、十字光标、时间轴对齐 | 新增 | ✅ |
| M2-10 | 技术指标叠加 | 可叠加 **MA / BOLL / SuperTrend / 撑压线 / VOL / MACD / KDJ 共 7 类**；指标参数在看板可改并即时重绘，配置存本地 | 新增 | ✅ |
| M2-11 | 支撑压力位 | 按摆动点聚类输出支撑/压力档位并叠加到图上；参数与策略 `sr_*` 同口径 | 新增 | ✅ |
| M2-12 | 周期切换 | 支持 1m/5m/15m/30m/1H/4H/6H/12H/1D 共 9 档 | 新增 | ✅ |
| M2-13 | 自选与对比 | **本期明确不做**；后端 `DataAligner` 已具备多品种对齐能力，前端同图归一化对比留待后续 | 新增 | 🚫 |
| M2-14 | 持仓量 | 拉取永续合约持仓量（open interest）历史，供因子与情绪判断；当前**完全未实现** | F1.4（拆分） | ⬜ |
| M2-15 | 数据质量报告 | 把校验结果结构化落 `data/quality/`，看板 `GET /api/quality` 可查看缺失段、已修复、未修复；未修复项产生告警（接 M9-11） | 新增 | ✅ |

**数据流**：`OKX history-candles → CandleFetcher(翻页/断点) → DataValidator(校验+报告) → 缺失段补拉(≤3次) → BarStorage(parquet)`；实时侧 `OKX WS tickers → parse_ticker → LivePriceBook(仅最新)`。

**不做**：盘口深度与逐笔成交（books/trades）、Tick 历史持久化、多品种同图对比（M2-13）、行情落 SQLite、第三方数据源（仅 OKX）。

**前置条件**：M2-15 依赖 M2-7 的校验结果结构化；M2-2 的连接状态上报与 M1-5 共用 `quant:heartbeat:{name}` 约定。

**验收**：1 年 1H K 线下载 < 60 秒且时间戳连续无缺失；看板切周期不触发全量重算，1 秒内出图；故意删掉中间 10 根后校验能识别、补回并在 `GET /api/quality` 中可见；WS 断连后 60 秒内自动恢复且退避不会永久停在单一间隔。

---

### 4.3 策略研究（M3）

**职责**：管理「策略是什么、参数是什么、现在开没开」，不负责回测结果统计（那是 M4）。

**边界**：只提供生效参数与启用状态供 engine 读取，不执行下单。绩效统计归 M4，风控阈值归 M7，资金分配归 M6。

#### 4.3.1 数据模型

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `ParamSpec` | `name` / `type` / `default` / `min` / `max` / `choices` / `desc` | 声明式参数元信息；`type` ∈ `int` / `float` / `bool` / `str` |
| `StrategyParam` | `strategy_id` / `name` / `value` / `updated_ts` / `updated_by` | DB 覆盖值；未覆盖时回落到 toml 默认 |
| `StrategyParamChange` | `strategy_id` / `name` / `old` / `new` / `ts` / `by` / `reason` / `commit` | 变更留痕，只追加不改 |
| `StrategyBinding` | `strategy_id` / `inst_id` / `bar` / `enabled` / `updated_ts` | 启停矩阵的一格；记录不存在即视为未启用 |
| `StrategyStatus` | `strategy_id` / `inst_id` / `enabled` / `last_eval_ts` / `last_signal` / `blocked_by` | 运行时状态，从 Redis 汇总，不落库 |

#### 4.3.2 参数校验与合并

- **schema 来源**：策略类声明 `PARAM_SPEC: dict[str, ParamSpec]`，键与 `on_init` 读取的键一一对应。未声明 `PARAM_SPEC` 的策略视为**不可在线编辑**，只能改 toml
- **合并顺序**：toml 默认值 ← DB 覆盖值，叠加后交给 `on_init`；`on_init` 内原有的 `ValueError` 保留为最后一道防线
- **校验时机**：保存前校验类型、范围、`choices`；越界直接拒绝，响应中给出字段名与允许区间
- **生效时机**：engine 每轮重载参数（现有行为），无需重启

#### 4.3.3 存储

- **SQLite 新增 3 张表**：`strategy_params`（覆盖值）、`strategy_param_changes`（变更留痕）、`strategy_bindings`（启停矩阵）
- **Redis**：`quant:engine:status:{strategy}:{inst}`（已有，运行时状态）、`quant:strategy:enabled:{strategy}:{inst}`（启停读取缓存）
- **TOML 不回写**：`config/strategies/<id>.toml` 只作为默认值与类路径来源，看板写覆盖层

#### 4.3.4 接口

| 接口 | 签名要点 | 说明 |
|---|---|---|
| 参数 schema | `StrategyRegistry.spec(strategy_id) -> dict[str, ParamSpec]` | 供看板渲染表单 |
| 生效参数 | `StrategyRegistry.effective_params(strategy_id) -> dict` | toml 默认 + DB 覆盖合并；engine 与回测共用同一份 |
| 参数写入 | `StrategyParamService.update(strategy_id, values, reason, by)` | 校验 → 写覆盖表 → 写变更记录 → 更新缓存 |
| 启停 | `StrategyBindingService.set(strategy_id, inst_id, enabled)` / `list()` | 写矩阵并刷新 Redis；engine 每轮读取，未启用则跳过评估 |

#### 4.3.5 需求条目

| 编号 | 功能项 | 详细要求 | 来源 | 状态 |
|---|---|---|---|---|
| M3-1 | 策略接口规范 | 回调 `on_init/on_bar/on_trade/protective_exit/on_finish`；策略状态独立，可并行回测 | F2.1 | ✅ |
| M3-2 | 策略登记 | 每个策略一个 `config/strategies/<id>.toml`，声明类路径与默认参数；看板按需扫描目录列出可用策略（`GET /api/strategies`）。**「未声明 `PARAM_SPEC` 即只读」归 M3-9** | 新增 | ✅ |
| M3-3 | 参数配置 | 看板按 `PARAM_SPEC` 渲染表单；保存前做类型、范围、枚举校验；非法值拒绝并指出字段与允许区间。已提供 `PUT /api/strategies/{id}/params` 与引擎面板表单 | 新增 | ✅ |
| M3-4 | 策略启停 | 支持「策略 × 品种」矩阵逐格启停；engine 已改为按 `strategy_bindings` 逐绑定循环。停止后不再评估该组合，**已有持仓默认保留**（自动平仓风险高，本期不做） | 新增 | ✅ |
| M3-5 | 信号预览 | 展示引擎最近一次评估结果：bar 时间、是否出信号、被哪类条件拦下。**现仅能给出粗粒度状态**（`waiting_data`/`no_signal`/`skipped`/`duplicate`/`error`）与原因串；策略内部的趋势过滤/撑压拦截无法区分 | 新增 | 🟡 |
| M3-6 | 双副本管理 | 策略同时维护 Pine 与 Python 两份源码，git 管理；Pine 为主回测 | F2.1 | ✅ |
| M3-7 | 研究工作流 | `notebooks/` 下可加载本地数据做探索，不依赖看板。**当前 `notebooks/` 只有 `.gitkeep`**，无任何示例 | 新增 | ⬜ |
| M3-8 | 参数版本与变更记录 | 每次改动记 `strategy_param_changes`：字段、改前、改后、时间、操作人、原因、当时 git commit | 新增 | ✅ |
| M3-9 | 声明式参数元信息 | 策略类声明 `PARAM_SPEC`（类型/默认/范围/枚举/说明）；未声明则不可在线编辑；新增策略须同时提供。`sma_cross` 与 `rsi` 均已声明 | 新增 | ✅ |
| M3-10 | 参数两层合并 | toml 默认 ← DB 覆盖；回测与实盘使用同一份合并结果，避免「回测用 A、实盘用 B」 | 新增 | ✅ |
| M3-11 | 启停矩阵 | `strategy_id × inst_id` 为键，`bar` 记在绑定上但不作为键——同一策略同一品种只允许一个周期，避免两个周期对同一持仓重复下单 | 新增 | ✅ |
| M3-12 | 启停即时生效 | 启停状态写 Redis 缓存，engine 每轮读取；切换无需重启进程 | 新增 | ✅ |
| M3-13 | 双副本一致性校验 | 比对 Pine 与 Python 的参数名、默认值、周期；不一致时在策略页提示并产生告警 | 新增 | ⬜ |
| M3-14 | 参数变更回滚 | 可从 `strategy_param_changes` 选任一条历史值一键恢复（产生一条新的变更记录，不删历史） | 新增 | ⬜ |
| M3-15 | 策略详情页 | 一页呈现：参数表单、启停开关、最近 10 次评估、关联回测入口、变更历史 | 新增 | ⬜ |

**参数变更流程**：`看板提交 → 校验(PARAM_SPEC) → 写 strategy_params → 写 strategy_param_changes(含 commit) → 刷新 Redis 缓存 → engine 下一根 bar 生效`。

**不做**：在线编辑策略代码（只编辑参数）、策略版本分支管理（靠 git）、参数自动寻优（归 M4）、策略市场/插件分发。

**前置条件**：M3-13 依赖 Pine 与 Python 的参数名映射；M3-15 依赖 M4 的回测入口。M3-4/M3-11/M3-12 已随 MS4-C 完成（engine 按绑定循环 + 绑定缓存），无剩余前置。

**验收**：改参数保存后，engine 下一根 K 线即使用新参数（无需重启）；参数非法时拒绝保存并给出具体字段与允许区间；Pine 与 Python 参数不一致时策略页可见提示；同一策略同一品种配置两个周期时被拒绝并说明原因。

---

### 4.4 回测分析（M4）

**职责**：在历史数据上验证策略，输出可对比的绩效，并检验参数稳定性。

**边界**：只做「历史数据 → 绩效」的计算、存储与展示，不改策略参数、不下单。参数编辑归 M3，实盘执行归 M8。Pine 回测是主战场，本模块提供 Python 交叉验证与偏离度量化。

#### 4.4.1 数据模型

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `BacktestRun` | `run_id` / `kind` / `strategy_id` / `inst_id` / `bar_size` / `start_ts` / `end_ts` / `params` / `params_hash` / `initial_cash` / `warmup_bars` / `status` / `error` / `created_at` / `finished_at` | `kind` ∈ `single` / `optimize` / `walk_forward` / `batch`；`status` ∈ `pending` / `running` / `done` / `failed` / `canceled` |
| `BacktestMetrics` | `run_id` + 全部指标列 | 与 `BacktestRun` 一对一；指标以列为准，便于 SQL 排序筛选 |
| `BacktestArtifact` | `run_id` / `kind` / `path` / `rows` | 指向 parquet 或 HTML 文件；DB 只存路径与行数，不存数组 |
| `OptimizeJob` | `job_id` / `base` / `grid` / `metric` / `status` / `total` / `done` / `best_params` | 网格任务进度与最优解 |
| `PineBaseline` | `run_id` / 各同名指标值 / `deviation` | 手工导入的 TradingView 回测指标与偏离度 |

#### 4.4.2 存储

**SQLite 新增 4 张表**：`backtest_runs`、`backtest_metrics`、`backtest_artifacts`、`optimize_jobs`。金额沿用 TEXT 存 Decimal 字符串，指标用 REAL。

`PineBaseline` **不单独建表**：导入的 Pine 指标落在 `backtest_metrics` 的 `pine_<metric>` 列与 `deviation` 列（同名指标一一对应），保持「5 个模型 ↔ 4 张表」口径一致。

**文件布局**：

```
data/backtests/{run_id}/
├── equity.parquet      # ts, equity
├── drawdown.parquet    # ts, drawdown
├── trades.parquet      # 成交明细
├── segments.parquet    # 仅 walk_forward：逐段 IS/OOS 结果
└── report.html         # 可选，复用现有 ReportGenerator
```

#### 4.4.3 接口

| 接口 | 签名要点 | 说明 |
|---|---|---|
| 提交回测 | `BacktestService.submit(req) -> run_id` | 先落库为 `pending` 再返回，后台线程执行；HTTP 立即 202 |
| 查询状态 | `BacktestService.status(run_id) -> dict` | 返回 `status` / 进度 / 完成时的指标 |
| 任务执行 | `TaskRunner` | 单 worker 线程 + FIFO 队列；同时只跑 1 个重任务，避免打满 CPU |
| Pine 对比 | `PineBaseline.compare(run_id, csv) -> dict` | 按固定列名 `metric,value` 解析，逐项算偏离度，> 5% 标记超限 |

#### 4.4.4 需求条目

| 编号 | 功能项 | 详细要求 | 来源 | 状态 |
|---|---|---|---|---|
| M4-1 | 回测引擎 | 事件驱动；按时间回放；市价/限价模拟成交；手续费按 OKX 费率 | F2.2 | ✅ |
| M4-2 | 成交模拟 | 本根收盘出信号、下一根开盘成交；`protective_exit` 支持 bar 内成交；滑点可配置 | F2.3 | ✅ |
| M4-3 | 绩效指标 | 总收益、年化、夏普、最大回撤、胜率、盈亏比、交易次数、平均持仓根数 | F2.4 | ✅ |
| M4-4 | 回测报告 | 收益曲线、回撤曲线、交易明细；输出 CSV 与 HTML（内联 SVG） | F2.5 | ✅ |
| M4-5 | 参数寻优 | 网格搜索；结果按指标降序；非法组合跳过而非报错；单次组合数上限。上限 `backtest.max_combos`（默认 500）由看板与 CLI 共用，CLI 可用 `--max-combos` 覆盖 | F2.6 | ✅ |
| M4-6 | Walk-forward 验证 | 滚动 IS/OOS；IS 选参、OOS 只评估；输出逐段表、复合收益、盈利段数、参数众数 | 新增 | ✅ |
| M4-7 | 批量回测 | 一次跑「多品种 × 多周期 × 多参数」，结果汇总成表。当前只有单策略单品种回测与单策略网格寻优，无跨品种/跨周期批量 | 新增 | ⬜ |
| M4-8 | 与实盘偏离 | 日报记录模拟盘成交与同参数回测的偏离度，超 5% 判定 FAIL 并随日报邮件发出。**未进 M9 告警体系**（`alerts` 表无对应规则），故为 🟡 | F3.6 | 🟡 |
| M4-9 | 结果持久化 | 每次回测登记 `BacktestRun`，指标写 `backtest_metrics`，大体积曲线与成交写 parquet 并登记 `artifacts`；重启后可查。**后端已完成**（4 张表 + `/api/backtest/runs` 与详情），前端历史仍走 localStorage | 新增 | 🟡 |
| M4-10 | 历史列表与重跑 | 按策略/品种/周期/时间/状态筛选历史回测；可复制参数一键重跑（新 `run_id`，不覆盖旧结果）。**后端已提供筛选、详情、取消与重跑接口**；前端未接入，仍用 localStorage 摘要（上限 20 条，同一配置覆盖） | 新增 | 🟡 |
| M4-11 | 多组结果对比 | 选 ≥2 个 run 并列展示权益/回撤曲线与指标差异表；须同品种同周期才允许对比。**后端 `GET /api/backtest/compare` 已校验品种/周期**；前端对比表仍取自 localStorage | 新增 | 🟡 |
| M4-12 | 后台任务与进度 | 提交即返回 `run_id`；前端轮询状态；支持取消（`pending` 直接取消，`running` 标记后在下根 bar 退出）。**后端任务队列与取消已就绪**；前端仍调用 `?sync=1` 同步路径 | 新增 | 🟡 |
| M4-13 | 寻优进度与并发 | `GridOptimizer` 的 `n_jobs` 真正生效（进程池）；进度按已完成组合数上报；单次组合数上限可配。**并行与 `backtest.max_combos` 已实现**；进度回调未上报到看板，`/api/optimize` 仍同步返回 | 新增 | 🟡 |
| M4-14 | 扩展指标 | 新增 Calmar、Sortino、月度收益表、最大连续亏损次数与金额、持仓暴露度（有仓 K 线占比）。**后端 7 项均已计算并落库**；前端展示 Calmar/Sortino/连亏/暴露度，月度收益表未呈现 | 新增 | 🟡 |
| M4-15 | 永续资金费率成本 | 回测可选计入资金费率：按 `funding_rate` 历史逐 8 小时结算，持仓过结算点才计费；多空方向符号不同 | 新增 | ⬜ |
| M4-16 | Pine 回测结果导入对比 | 支持粘贴/上传 `metric,value` 两列 CSV，与同参数 Python 回测逐项算偏离度；> 5% 标红并禁止该参数集上实盘 | 新增 | ⬜ |

**指标口径**：`equity = cash + qty × close` 逐根记录；年化按 `(1+r)^(365天/回测时长)-1`；夏普用日收益 `mean/std × sqrt(365)`（加密 7×24）；最大回撤与回撤曲线共用一次遍历。新增指标沿用同一权益序列，不引入新的数据来源。

**不做**：分布式回测、tick 级回测、可视化策略编辑器、自动对接 TradingView（无公开 API，只支持导入）。

**前置条件**：M4-15 依赖 M2-4 补齐资金费率历史回补（当前只取最新一条，不足以覆盖回测区间）；**M4-12/M4-13 的后端长任务与并发已就绪**，剩余为前端轮询与进度展示；M4-16 的 CSV 列名固定为 `metric,value`，指标名与内部字段同名。

**验收**：提交一次长的批量回测，HTTP 在 1 秒内返回 `run_id` 且页面可轮询进度；进程重启后历史回测仍可列出并查看曲线；同一参数集的全量重跑与缓存结果逐项相等；导入 Pine 指标后偏离度 > 5% 的项明确标红；walk-forward 的 OOS 数据不得回流影响选参（有单测固定）。

---

### 4.5 因子库（M5）⬜ 未开始

**职责**：把「可复用的信号原料」沉淀成可注册、可缓存、可检验的单元，供策略与组合复用。当前代码中没有任何因子实现（`src/quant/indicators/` 只有支撑压力，不是因子），本节为完整设计，排 MS7+，不阻塞实盘。

**范围**：同时支持时序因子（单品种自身历史）与横截面因子（多品种排名/分位）。横截面依赖 M2-8 的多品种对齐能力，需该能力稳定后才可计算。

#### 4.5.1 数据模型

| 模型 | 字段 | 说明 |
|---|---|---|
| `FactorDef` | `name` / `category` / `scope` / `lookback` / `params` / `required_fields` / `fn` | 注册元信息。`scope` ∈ `time_series` / `cross_section`；`lookback` 声明最少需要多少根 bar；`required_fields` 限定只能依赖 `open/high/low/close/vol/vol_quote` |
| `FactorValue` | `ts` / `inst_id` / `value` / `param_hash` | 单点计算结果。`param_hash` 由参数规范化后哈希得到，使同一因子多组参数并存而不互相覆盖 |
| `FactorEval` | `factor` / `bar` / `inst_ids` / `horizon` / `ic` / `rank_ic` / `icir` / `layered_returns` / `turnover` | 有效性评估产物；`horizon` 为预测未来多少根 bar 的收益 |

#### 4.5.2 存储

```
data/factors/{factor}/{bar_size}/{year}.parquet
列：ts(int64) / inst_id(str) / value(float64) / param_hash(str)
```

- 与 `data/candles/{inst_type}/{inst_id}/{bar_size}/{year}.parquet` 同构：按 UTC 年份分片、zstd 压缩、`row_group_size=8192`、同键去重保留最新，复用 `BarStorage` 的合并写法。
- 横截面因子**不单独存宽表**，按需从时序结果按 `ts` 透视生成，避免同一数据两种形态不一致。

#### 4.5.3 接口

| 接口 | 签名要点 | 说明 |
|---|---|---|
| 注册 | `@factor(name, scope=..., lookback=..., params=...)` | 装饰器登记到 `FactorLibrary`，模块导入即注册；重名直接报错 |
| 时序计算 | `compute(name, inst_id, bar_size, start_ts, end_ts, params) -> pd.Series` | 命中 parquet 缓存直接读，否则算完落盘；`index=ts` |
| 横截面对齐 | `compute_panel(name, inst_ids, bar_size, start_ts, end_ts, params) -> pd.DataFrame` | `index=ts`、`columns=inst_id`；底层复用 `DataAligner` |
| 有效性评估 | `evaluate(name, inst_ids, bar_size, horizon=1, **params) -> FactorEval` | 产出 IC / RankIC / ICIR / 分层收益 / 换手 |
| 策略接入 | `Context.factors: FactorProvider \| None`（默认 `None`） | 策略写 `ctx.factors.get(name)` 取最近值；**禁止策略直接读 `data/factors`**，保证回测与实盘同窗口口径且不破坏现有回测 |

**窗口约束（必须硬校验）**：`Context.bars_window` 会被裁剪到 `max(warmup_bars, window_size)` 根，若因子的 `lookback + 1` 大于该值，因子只能拿到被截断的历史而**静默算出错误值**。因此 engine 与回测在装配 `FactorProvider` 时必须校验 `窗口 ≥ max(因子 lookback) + 1`，不足直接报错退出，不允许降级。

**构造点**：`Context` 是 `slots=True` 的 dataclass，新增 `factors` 字段后需同步 `src/quant/engine/live.py` 与 `src/quant/backtest/engine.py` 两处构造，及其单测。

#### 4.5.4 需求条目

| 编号 | 功能项 | 详细要求 | 来源 | 状态 |
|---|---|---|---|---|
| M5-1 | 因子定义与注册 | `@factor` 装饰器登记；元信息含名称、类别（动量/波动/趋势/量能/微观结构）、`scope`、`lookback`、默认参数；启动扫描全部模块完成注册 | 新增 | ⬜ |
| M5-2 | 计算函数契约 | 入参为 `(bars, params)`，返回与 bar 等长的序列；只能访问当前及更早的 bar，越界访问未来数据视为违规，由单测固定 | 新增 | ⬜ |
| M5-3 | 参数化与版本 | 参数规范化后哈希为 `param_hash`；同因子多组参数独立缓存；因子改名保留别名，旧结果仍可检索 | 新增 | ⬜ |
| M5-4 | 批量计算与缓存 | 按「因子 × 品种 × 周期 × 时间范围」计算并落 parquet；再次请求命中缓存，不重复计算 | 新增 | ⬜ |
| M5-5 | 增量更新 | 已有缓存时只计算新增 bar，且必须回退 `lookback` 根重叠计算，避免窗口边界跳变 | 新增 | ⬜ |
| M5-6 | 横截面标准化 | 提供 z-score 与百分位两种标准化；输入多品种宽表，输出同形；缺失品种不参与该 ts 的统计 | 新增 | ⬜ |
| M5-7 | 因子有效性分析 | 计算 IC、RankIC、ICIR；默认预测未来 1 根，可配置 horizon | 新增 | ⬜ |
| M5-8 | 分层收益 | 按因子值分五档，统计各档未来收益与多空组合收益 | 新增 | ⬜ |
| M5-9 | 因子衰减 | 计算 lag 1..N 的 IC 序列，判断信号有效期 | 新增 | ⬜ |
| M5-10 | 换手率 | 按分档变动统计因子换手，作为成本估算输入 | 新增 | ⬜ |
| M5-11 | 相关性矩阵 | 因子两两相关性矩阵，标出 > 0.8 的冗余对并提示合并 | 新增 | ⬜ |
| M5-12 | 缺失与极值处理 | 缺失值支持「前值 / NaN / 跳过」三选一；极值支持按分位裁剪；停牌或零成交 bar 不产生因子值 | 新增 | ⬜ |
| M5-13 | 策略与组合复用接口 | 统一走 `FactorProvider`；回测与实盘使用同一提供器，窗口裁剪规则一致 | 新增 | ⬜ |
| M5-14 | 一致性校验 | 同一因子在「全量重算」与「增量更新」两条路径下结果必须逐点相等，由单测覆盖 | 新增 | ⬜ |
| M5-15 | 因子页展示 | 因子列表（类别/scope/参数）、单因子值曲线、IC 时序、分层收益柱状图、相关性热力图 | 新增 | ⬜ |

**不做**：因子自动挖掘（遗传算法/穷举搜索）、在线因子服务、因子组合的自动权重优化。

**前置条件**：M2 多品种对齐稳定（横截面因子依赖）；M4 回测引擎能接收外部因子序列（即 `Context.factors` 落地）；`window_size`/`warmup_bars` 需可按最大因子 `lookback` 自动抬高，否则 M5-13 无法保证与实盘同窗口。pandas / pyarrow 已在 `m1` 依赖中，无需新增包。

**验收**：注册一个新因子后，`compute` 首次计算落盘、二次命中缓存；全量与增量结果逐点相等；`evaluate` 能给出 IC 时序与分层收益；策略在不改代码的情况下切换「用因子 / 不用因子」两种模式。

---

### 4.6 组合管理（M6）⬜ 未开始

**职责**：当同时跑多个「策略 × 品种」单元时，决定「钱怎么分、仓位怎么控、什么时候调」。当前系统为单策略单品种，本节为完整设计，排 MS7+。

**边界**：组合层**只产出目标权重与调仓计划**，不直接调用 OKX。下单走 M8 的 worker 与审计，风控走 M7 的同一套 checker，避免出现第二条下单路径。

#### 4.6.1 数据模型

| 模型 | 字段 | 说明 |
|---|---|---|
| `PortfolioConfig` | `portfolio_id` / `name` / `units[]` / `allocator` / `rebalance` / `risk` | 落 `config/portfolios/<id>.toml` |
| `PortfolioUnit` | `strategy_id` / `inst_id` / `params_ref` / `weight` / `enabled` | 一个「策略 × 品种」单元；`params_ref` 指向 `config/strategies/` 中的参数集 |
| `RebalancePlan` | `ts` / `deltas[]` / `reason` | `deltas` 为目标权重与实际权重的差额，转为调仓单 |
| `PortfolioSnapshot` | `ts` / `equity` / `unit_equity{}` / `weights{}` / `drawdown` | 定时落库，供归因、曲线与再平衡判断 |

#### 4.6.2 存储

- 配置：`config/portfolios/<id>.toml`，与 `config/strategies/` 同级，均在 `.gitignore` 内
- 运行时：SQLite 新增 `portfolio_state`（当前权重与启用单元）与 `portfolio_snapshots`（权益时序），金额沿用 TEXT 存 Decimal 字符串

#### 4.6.3 接口

| 接口 | 签名要点 | 说明 |
|---|---|---|
| 资金分配 | `Allocator.allocate(equity, config) -> dict[unit_key, Decimal]` | 支持 `equal` / `fixed` / `inverse_vol` / `vol_target`；输出归一化权重 |
| 再平衡判断 | `Rebalancer.evaluate(snapshot, config) -> RebalancePlan \| None` | 触发方式 `calendar`（固定周期）或 `threshold`（漂移超 `drift_pct`）；无动作返回 `None` |
| 绩效归因 | `Attribution.report(snapshots, config) -> pd.DataFrame` | 收益拆到 unit，区分「权重配置贡献」与「单元自身收益」 |

#### 4.6.4 需求条目

| 编号 | 功能项 | 详细要求 | 来源 | 状态 |
|---|---|---|---|---|
| M6-1 | 组合配置 | 组合 = 若干「策略 × 品种」单元 + 权重 + 分配方法 + 再平衡策略；独立 toml，看板可读 | 新增 | ⬜ |
| M6-2 | 单元管理 | 单元可单独启用/停用；停用时保留持仓与历史，仅停止新信号 | 新增 | ⬜ |
| M6-3 | 资金分配 | 四种方法：等权、固定权重、波动率倒数、目标波动；单一组合只用一种，不做混合 | F3.4/F4.3 局部 | ⬜ |
| M6-4 | 权重归一与上限 | 权重归一后总和为 1；单单元上限可配；组合总仓位仍受 M7 `max_total_pct` 约束 | 新增 | ⬜ |
| M6-5 | 组合层风控 | 组合总回撤上限、单元间相关性上限、单单元最大贡献度；触发时降权或暂停该单元 | 新增 | ⬜ |
| M6-6 | 再平衡触发 | 支持日历（固定周期）与阈值（权重漂移超 `drift_pct`）两种，可同时开启 | 新增 | ⬜ |
| M6-7 | 再平衡冷却期 | 触发后 N 小时内不再触发，避免反复调仓吃掉收益 | 新增 | ⬜ |
| M6-8 | 调仓单生成 | `RebalancePlan` 拆成各单元的目标增量，交由 M8 统一下单并审计 | 新增 | ⬜ |
| M6-9 | 再平衡失败回滚 | 部分单元下单失败时不重复追单，记录失败原因，下一周期重新评估 | 新增 | ⬜ |
| M6-10 | 快照与存储 | 每次评估写 `PortfolioSnapshot`；频率可配（默认每根 bar 收盘） | 新增 | ⬜ |
| M6-11 | 绩效归因 | 组合收益拆到单元；区分「权重配置贡献」与「单元自身收益」 | 新增 | ⬜ |
| M6-12 | 组合回测 | 用历史数据模拟多单元并行运行与再平衡，输出组合级绩效指标（与 M4 同口径） | 新增 | ⬜ |
| M6-13 | 与单策略模式兼容 | 未配置组合时系统行为与现在完全一致：单策略单品种直接跑，不经过组合层 | 新增 | ⬜ |
| M6-14 | 组合页展示 | 权重饼图、组合权益与回撤曲线、各单元贡献瀑布图、再平衡记录 | 新增 | ⬜ |

**不做**：动态择时切换权重、机器学习权重优化、跨交易所组合、组合内对冲配对。

**前置条件**：M8 支持多策略多品种并行执行；M7 风控支持组合级规则；M4 回测引擎可复用为组合回测。

**验收**：配置双单元组合后，再平衡产生的调仓单只经 M8 下单且全部留痕；关闭组合层后系统回到单策略行为；组合回测与逐单元单独回测的加权结果可对齐。

---

### 4.7 风险控制（M7）

**职责**：定义「什么情况下不许下单、什么时候必须停」，并提供一键停止。

**边界**：只做判定，不做仓位算量（归 M8-14）、不做资金分配（归 M6）、不做策略参数（归 M3）。M8 在下单前调用 M7，M7 不直接触达交易所。

#### 4.7.1 参数作用域

两层：**全局默认**（`config/config.toml` 的 `[risk]`）+ **策略覆盖**（`config/strategies/<id>.toml` 的 `[risk]`）。合并规则为**取严**——同一字段策略值与全局值同时存在时取更保守的一方（比例类取小、笔数类取小），并在 `RiskLimits.applied` 中标注每个字段的来源。

#### 4.7.2 数据模型

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `RiskLimits` | `strategy_id` + `max_order_qty` / `max_position_pct` / `max_total_pct` / `daily_loss_limit` / `consecutive_loss_limit` + `applied` | 合并后的生效参数；`applied` 记录每个字段来自 `global` 还是 `strategy`。**`max_order_qty` 沿用历史字段名，实际含义是单笔最大名义价值（USDT），不是数量** |
| `RuleHit` | `rule_id` / `limit` / `actual` / `qty_before` / `qty_after` | 单条规则的命中详情；未触发则不产生 |
| `RiskDecision` | `approved` / `adjusted_qty` / `reason` / `halt` / `hits: list[RuleHit]` | 在现有结构上增加 `hits`，便于解释「为什么被拒/被减量」 |
| `RiskEvent` | `ts` / `signal_id` / `strategy_id` / `inst_id` / `approved` / `halt` / `reason` / `hits`(JSON) / `equity` | 每次判定落一条，供风控页与告警查询 |
| `CircuitBreakerState` | `inst_id` / `triggered_ts` / `reason` / `resume_at` / `status` / `requires_confirm` | `status` ∈ `tripped`（冷却中）/ `awaiting_resume`（冷却结束、待人工确认）；按品种独立 |

#### 4.7.3 存储

- **SQLite 新增 2 张表**：`risk_events`（每次判定一条，`hits` 存 JSON）、`risk_overrides`（阈值变更留痕：字段、改前、改后、时间、原因、操作来源）
- **Redis**：`quant:risk:limits:{strategy}`（worker 发布的生效值，带 TTL）、`quant:risk:overrides`（hash，字段 `{strategy}:{param}` → 值；**唯一生效源**，worker 每轮心跳重读）、`quant:risk:circuit:{inst_id}`（熔断状态；默认**不设 TTL**，需人工确认恢复，仅在 `circuit_resume_requires_confirm=false` 时带 cooldown TTL）
- 覆盖值只存在于 Redis 与 `risk_overrides`，**不写回 toml**；进程重启后从 Redis 恢复。清空该 hash 即全部回落配置值（应急回滚手段）

#### 4.7.4 接口

| 接口 | 签名要点 | 说明 |
|---|---|---|
| 参数合并 | `resolve_limits(strategy_id) -> RiskLimits` | 全局默认 + 策略覆盖取严；每字段标注来源 |
| 规则判定 | `RiskChecker.check(signal, market_price, today_start_ms, equity) -> RiskDecision` | 签名不变，返回值增加 `hits`；规则顺序固定：halt 类（日亏损 → 连亏 → 熔断）→ 名义价值类 → 比例类 |
| 阈值覆盖 | `RiskOverrideService.set(field, value, reason)` / `list()` / `clear(field)` | 写 Redis 覆盖值 + 落 `risk_overrides`；与 toml 分离，重载后仍生效 |
| 熔断 | `CircuitBreaker.evaluate(bar) -> CircuitState \\| None` / `trip(reason)` / `state()` / `resume()` | 单根 `(high−low)/open` 严格大于阈值即按品种熔断；冷却结束转 `awaiting_resume`，需人工确认才恢复 |
| 事件查询 | `RiskEventService.list(strategy_id, inst_id, rule_id, halt_only, start_ts, end_ts)` | 供风控页与 M9 告警共用 |

#### 4.7.5 需求条目

| 编号 | 功能项 | 详细要求 | 来源 | 状态 |
|---|---|---|---|---|
| M7-1 | 单笔与单品种限制 | 单笔最大名义价值、单品种最大持仓比例；超出时先尝试下调数量，调到 ≤0 则拒绝 | F3.4 | ✅ |
| M7-2 | 总仓位上限 | 全部持仓名义价值 / 权益 ≤ `max_total_pct`；口径明确为**账户级**（汇总全部品种），参数按策略取值 | F3.4/F4.3 | ✅ |
| M7-3 | 日亏损与连亏保护 | 日内已实现亏损触及 `daily_loss_limit` 停止开仓；连续 N 笔亏损同样停止；两者均置 `halt=true`。**「日内」按 UTC+8 切日**（`day_start_ms_utc8`），与 §5.6 的 UTC 日口径不同，见下方口径说明 | F3.4 | ✅ |
| M7-4 | 紧急停止 | kill switch：停止所有策略、撤销全部挂单；看板一键触发并二次确认。激活时默认同时撤销交易所全部挂单并同步本地订单状态，可勾选「仅停止不撤单」；撤单失败不回滚停止标志，页面单独提示需人工核对 | F4.5 | ✅ |
| M7-5 | 实时风险指标 | 保证金率、实时回撤、强平距离、撤单率；与首页 M1-3 同源 | F5.3 | ✅ |
| M7-6 | 极端行情熔断 | 单根振幅 `(high−low)/open` 超阈值（默认 10%）按品种暂停 N 分钟并告警。已落地：`CircuitBreaker.evaluate()` 在引擎每轮评估已收盘 bar 时判定，触发后该品种跳过并推进水位（恢复后不补评估过期 bar），落 `risk_events` 并产生 `circuit_breaker` 告警；worker 同步拒绝该品种信号，其它品种不受影响；冷却期结束后转 `awaiting_resume`，需人工确认恢复 | 4.7（v0.5） | ✅ |
| M7-7 | 下单前预检 | 余额、价格/数量精度、最小下单量、最小名义金额；当前仅手工交易校验 `minSz`，需下沉到 worker 使自动路径同样生效。**已落地**：`PreflightChecker`（`src/quant/risk/preflight.py`）在 worker 调用 `executor.execute` 前依次校验产品配置存在性、`lotSz` 精度、`minSz` 最小量与最小名义金额；现货额外校验可用余额（`availBal → availEq → cashBal`，默认预留 2% 余量、快照缓存 15s），**合约跳过余额校验**（杠杆下的可用保证金无法由本地余额推导，交给交易所 51008 判定）。预检返回的 `qty` 为 `lotSz` 归一化后的基础币数量，worker 必须用它下单，否则 broker 静默取整会导致本地记录与成交不符；合约按 `contracts = floor(qty / ct_val, lotSz)` 折算，`minSz` 比的是张数。拒单直接落 `risk_events` 并从 inflight 移除，**不进 broker、不进死信**。余额读失败放行（`ok=true` 且 `balance_checked=false`，不得显示成「查过且通过」）。仅 `--broker okx` 且直连客户端可用时装配；四层开关见 `[risk] preflight_*` | F4.4 | ✅ |
| M7-8 | 阈值在线调整 | 看板改阈值 → 写 Redis 覆盖层 + 落 `risk_overrides`；不重启即生效，变更可追溯。已落地：`RiskOverrideService.set/clear/list` 写 Redis hash `quant:risk:overrides`（字段 `{strategy}:{param}`）并追加审计行；worker 每轮心跳 `_refresh_limits()` 重读覆盖层并重建 `RiskConfig`，因此最长一个心跳周期（默认 60s）生效；`GET/POST /api/risk/overrides`、`DELETE /api/risk/overrides/{strategy_id}/{param}`；看板「风控限额」内嵌「在线调整阈值」表单与生效覆盖列表 | 新增 | ✅ |
| M7-9 | 风控规则页 | 展示生效规则、阈值及来源（全局/策略/覆盖）、今日触发次数与最近一次详情。**阈值与来源已展示**（规则行显示 `default/global/strategy/override` 标注，override 红色高亮）；`risk_events` 只写不读，触发次数与最近详情缺查询接口 | 新增 | 🟡 |
| M7-10 | 两层参数合并 | 全局默认 + 策略覆盖取严；`RiskLimits.applied` 标注每字段来源，页面可解释「这个值从哪来」。看板「风险规则」面板已显示 global/strategy/override 来源 | 新增 | ✅ |
| M7-11 | 风控事件落库 | 每次判定写 `risk_events`，含命中规则、限值、实际值、调整前后数量、当时权益；拒绝与减量都记 | 新增 | ✅ |
| M7-12 | 熔断恢复策略 | 冷却期结束不自动放行，需人工在风控页确认恢复；恢复动作写 `risk_events`。已落地：冷却到期状态转 `awaiting_resume`（键不设 TTL，引擎与 worker 继续拦截），`POST /api/risk/circuits/{inst_id}/resume` 仅在冷却结束后可用，恢复时写一条 `decision=resumed` 的 `risk_events` 并发 INFO 告警；`[risk] circuit_resume_requires_confirm=false` 可回滚到自动恢复。**已知风险**：熔断键永久保留，若长期无人确认，该品种会一直不开新仓；且 `awaiting_resume` 期间的 bar 不补评估，Redis 未持久化重启会丢失熔断状态（等同自动恢复）。运维口径见 `docs/reference/RUNBOOK_PAPER_TRADING.md` §4.1 | 新增 | ✅ |
| M7-13 | 熔断与 kill switch 职责分离 | 熔断只作用于单个品种、有自动冷却；kill switch 作用于全局、只能人工解除。两者互不复用状态键。状态键分别为 `quant:risk:circuit:{inst_id}` 与 `quant:kill-switch`，单测覆盖「熔断不激活 kill switch」「其它品种照常交易」 | 新增 | ✅ |
| M7-14 | 阈值变更审计 | `risk_overrides` 记录字段、改前、改后、时间、原因、来源；覆盖值可从页面一键清除回落到配置值。已落地：`insert_risk_override` 只追加不覆盖，`revoke_risk_overrides` 置 `revoked_at/revoked_by` 而非删行；`RiskOverrideService` 先写 Redis 后写审计，**审计失败回滚 Redis**，避免「生效了但查不到谁改的」；变更原因必填，比例类字段约束在 (0,1]；看板「变更历史」可查全部记录（含已清除） | 新增 | ✅ |
| M7-15 | 权益口径统一 | 一律使用 monitor 账户快照的 `total_equity`；未拿到快照时不得退回 `initial_equity` 做真实下单判定，应拒绝并记原因。`[risk] allow_initial_equity_fallback` 默认 `false`，worker 已按配置传入 | 新增 | ✅ |
| M7-16 | 规则边界单测 | 每条规则覆盖：恰好等于阈值、无可用价格、空持仓、权益为 0、多规则同时命中时的优先级。`tests/test_risk.py` 已覆盖上述边界 | 新增 | ✅ |
| M7-17 | 风控与告警联动 | `halt=true` 或熔断触发时，同步产生 M9 告警；同一次事件不重复告警（按 signal_id 去重）。熔断分支已接入（引擎直接 `AlertService.publish`）；`halt`（日亏损/连亏）的告警联动仍未接 | 新增 | 🟡 |

**规则执行顺序**：日亏损 → 连亏 → 熔断（三者任一命中即 `halt`，不再继续）→ 单笔名义价值 → 单品种比例 → 总仓位比例。前三条为硬停止，后三条为可下调数量的软限制。

**日切口径说明**：风控的「日亏损」以 **UTC+8 00:00** 为界（`src/quant/foundation/schedule.py` 的 `day_start_ms_utc8`，worker 与看板同源），便于与国内交易习惯对齐；而 §5.6 的「跨日聚合按 UTC 日」只约束**统计与报表**（日报、权益快照、指标聚合）。两套口径并存是有意为之，但**必须在看板与日报上标注时区**，否则「今日亏损」会出现两个数字。

**不做**：组合级风控（归 M6-5）、机器学习异常检测、跨交易所风险汇总、保证金模式自动切换。

**前置条件**：M7-15 依赖 monitor 账户快照；M7-6 依赖引擎能取到当根 bar 的 `high/low/open`；M7-9 页面依赖 M7-11 的事件表。

**验收**：同一字段在全局与策略配置不同时，生效值为取严后的结果且页面标出来源；把阈值在线调低后不重启即生效，清除覆盖后回落配置值；任一规则命中都能在风控页查到完整命中详情与调整前后数量；熔断只影响对应品种，kill switch 仍能全局停止。

---

### 4.8 交易执行（M8）

**职责**：把信号可靠地变成订单与成交，并保证进程崩溃后可恢复。

**边界**：只负责「信号 → 订单 → 成交 → 持仓」的落地。风控规则判定归 M7（M8 只调用其 checker，不重复实现），策略参数归 M3，资金分配归 M6。

#### 4.8.1 数据模型

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `Signal` | `strategy_id` / `side` / `order_type` / `inst_id` / `qty`（**已改为可选**）/ `size_pct` / `price` / `ts` / `signal_id` / `source` | `qty` 缺省时由 M8-14 在服务端按「权益 × 单笔比例 ÷ 价格」计算并按 `lotSz` 向下取整；`qty` 给了仍必须 > 0。`size_pct` 可选，缺省回落 `[execution] default_size_pct`。`source` ∈ `webhook` / `manual` / `portfolio` |
| `Order` | `order_id` / `signal` / `state` / `filled_qty` / `avg_price` / `reduce_only` / `cl_ord_id` / `source` / `created_ts` / `updated_ts` | `state` ∈ `pending` / `live` / `partially_filled` / `filled` / `canceled` / `failed` |
| `Fill` | `order_id` / `inst_id` / `side` / `qty` / `price` / `fee` / `fee_ccy` / `ts` | 现货手续费扣基础币，合约扣 USDT，`fee_ccy` 明确标注 |
| `PositionSnapshot` | `ts` / `inst_id` / `qty` / `avg_price` / `upl` / `source` | `source` ∈ `local` / `remote`，对账时两侧对比 |
| `Discrepancy` | `ts` / `kind` / `key` / `local_value` / `remote_value` / `resolved` | `kind` ∈ `order` / `position` / `balance` |
| `DeadLetter` | `id` / `signal` / `reason` / `created_ts` / `status` / `replayed_ts` | `status` ∈ `new` / `replayed` / `discarded` |

#### 4.8.2 存储

- **Redis**：`quant:signals`（pending）/ `quant:signals:inflight` / `quant:signals:dead`；去重键 `hash(signal_json)` + `SETNX` + TTL 5 分钟
- **SQLite 新增 2 张表**：`dead_letters`（死信持久化，供看板分页与状态标记）、`reconciliations`（每条对账差异）
- **SQLite 加列**：`orders` 表补 `filled_qty` / `avg_price` / `source`
- 死信同时写 Redis 与 SQLite：Redis 列表作为待处理队列，SQLite 作为权威记录与状态来源

#### 4.8.3 接口

| 接口 | 签名要点 | 说明 |
|---|---|---|
| 仓位计算 | `PositionSizer.size(signal, equity, price, limits) -> Decimal \| None` | 信号不带 `qty` 时按「权益 × 单笔比例 ÷ 价格」算量，向下取整到 `lotSz`；无法定价或算量为 0 返回 `None` 并拒单 |
| 状态机 | `OrderStateMachine.transition(local_order, remote_detail) -> Order` | 只允许 `pending → live → partially_filled → filled/canceled`，禁止回退；部分成交累加 `filled_qty` 并重算 `avg_price` |
| 死信运维 | `DeadLetterService.list(limit, offset)` / `replay(id)` / `discard(id)` | 重放前查 `signals` 表做幂等，已 `executed` 的直接拒绝重放 |
| 对账 | `Reconciler.reconcile() -> list[Discrepancy]` | 三类对象：订单状态、持仓数量、账户余额；以远端为准并写 `reconciliations` |
| 手工交易 | `ManualTrader.place` / `cancel` / `amend` / `reduce` / `flatten` | 全部走同一套 M7 风控与 `signals` 审计，`source=manual` |

#### 4.8.4 需求条目

| 编号 | 功能项 | 详细要求 | 来源 | 状态 |
|---|---|---|---|---|
| M8-1 | 模拟盘接入 | `x-simulated-trading:1`；与实盘共用同一套 API 封装 | F3.1 | ✅ |
| M8-2 | Webhook server | FastAPI 端点收 TV alert JSON；5 秒内 ACK；解析失败入死信；按 `hash(json)+TTL 5min` 去重 | F3.2 | ✅ |
| M8-3 | 信号队列与 worker | Redis LIST + BRPOPLPUSH；先入 inflight 再处理；失败入死信并告警；worker 每分钟心跳 | F3.3 | ✅ |
| M8-4 | 订单管理 | 下单/撤单/改单/批量；状态轮询；异常自动撤单 | F3.5 | ✅ |
| M8-5 | 状态持久化 | 信号/下单/成交/持仓变化写 SQLite；重启从最后检查点恢复 | F3.7/F4.7 | ✅ |
| M8-6 | 启动对账 | worker 启动先对账再消费；连续 3 次不一致告警 | F3.7/F4.7 | ✅ |
| M8-7 | 实盘接入 | 实盘 Key 仅开交易+读取、绑定 IP、不开提币；走子账户 | F4.1 | ⬜ |
| M8-8 | 真 webhook 接入 | TV alert 指向 VPS 公网端点；多策略按 `strategy_id` 路由到各自风控参数集 | F4.2 | ⬜ |
| M8-9 | 异常处理与重试 | 网络异常重试 3 次后告警；订单状态不明主动查询；余额不足停止下单 | F4.4 | ✅ |
| M8-10 | 操作审计 | 每笔下单/撤单记录时间、品种、价格、数量、策略 ID、来源、原因 | F4.6 | ✅ |
| M8-11 | 多进程编排 | 托管 server/worker/engine/monitor 等进程；崩溃自动重启；重启先对账再启动 | F4.8 | 🟡 |
| M8-12 | 执行页展示 | 订单列表、成交列表、持仓列表；支持按品种/策略/状态/来源筛选 | 新增 | ✅ |
| M8-13 | 减仓与平仓 | 手动减仓（按比例或指定数量）、一键平仓；`reduce_only=true` 防止反向开仓；二次确认 | 新增 | ✅ |
| M8-14 | 服务端仓位计算 | 信号可省略 `qty`；worker 按「权益 × 单笔比例」算量并按 `lotSz` 向下取整；实际量与上限均记入审计。**已落地**：`PositionSizer`（`src/quant/execution/sizing.py`）在风控前补量，比例三级回落（`qty` → `size_pct` → `[execution] default_size_pct`，默认 0.1）。算量链为 `raw = 权益 × 比例 ÷ 价格` → `max_order_qty` 截断 → 按 `lotSz` 向下取整，`minSz` 不足或算量为 0 直接拒单；合约按 `ctVal` 折张数后再取整（`minSz` 比张数）。补出的量会写一条 `decision="sized"` 的 `risk_events`（`qty_before`=取整前、`qty_after`=实际下单量、`rule`=截断来源），随后仍过风控与预检。webhook 的 `qty` 已改为可选，也可只给 `size_pct`；`[execution] server_side_sizing=false` 是回滚开关，关掉后无 `qty` 的信号直接拒单。产品规格读不到时退回模拟精度，交易所精度由预检兜底 | 新增 | ✅ |
| M8-15 | 订单状态机 | `pending→live→partially_filled→filled/canceled` 单向推进；部分成交累加并重算均价；禁止状态回退。**已落地**：`order_state.can_transition` 定义合法迁移与终态，`update_order_fill` 累加成交量并按加权重算均价、非法迁移只记 WARNING 保持原状态；`save_order` 由「整行覆盖」改为「插入或忽略 + 状态机推进」，重复落库（重放/崩溃恢复）不会把 `filled` 打回 `pending`。`pending → filled` 直连保留，因为市价单可能一次回包即成交 | 新增 | ✅ |
| M8-16 | 死信查看与重放 | 看板列出死信（原始信号 + 原因 + 时间）；支持单条/批量重放回 pending，或标记丢弃；重放前做幂等检查 | 新增 | ✅ |
| M8-17 | 手工下单与撤改 | 看板可下市价/限价单，可撤单、改价改量；与自动单共用风控、审计与状态机 | 新增 | 🟡 |
| M8-18 | 三项对账 | 订单 + 持仓 + 余额三类对象对账；差异写 `reconciliations` 并区分「可自动修复」与「需人工确认」 | 新增 | 🟡 |
| M8-19 | 下单幂等与重试边界 | `clOrdId` 由 `signal_id` 派生保证交易所侧幂等；超时状态不明时先查询再决定，禁止直接重发；重试上限 3 次 | 新增 | 🟡 |

**下单流程**：`webhook → 去重 → pending → worker BRPOPLPUSH → 幂等检查 → 仓位计算(M8-14，仅 qty 缺省时) → 风控(M7) → 预检(M7-7) → 下单 → 状态机推进(M8-15) → 写 signals/orders/fills/positions → 移出 inflight`。

**不做**：算法单（TWAP/冰山）、跨交易所路由、网格/马丁自动挂单、保证金模式动态切换。

**前置条件**：M8-14 依赖 M7-1 的单笔上限；M8-15 依赖 OKX 订单 `state` 枚举；M8-18 依赖 `/api/v5/account/positions` 与 `/api/v5/account/balance`。

**验收**：信号不带 `qty` 时仍能按权益比例正确下单且不超单笔上限；部分成交后 `filled_qty` 单调递增且最终等于成交量；死信重放不会产生重复订单；杀掉 worker 后重启，未消费信号不丢、不重复下单；持仓与余额差异能被对账识别并落 `reconciliations`。

---

### 4.9 日志告警（M9）

**职责**：出问题时「第一时间知道」，事后「查得到证据」。

**边界**：只消费其它模块产生的事件，不自己造业务事件。风控触发（M7-17）、死信新增（M8-16）、回测任务失败（M4-12）、对账差异（M8-18）都通过统一入口写入告警。

#### 4.9.1 数据模型

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `Alert` | `alert_id` / `rule` / `level` / `message` / `ts` / `inst_id`? / `strategy_id`? / `context`(JSON) / `status` / `acked_ts` / `ack_by` | 在现有 4 字段（rule/level/message/ts）基础上补对象定位与状态 |
| `AlertRule` | `rule_id` / `level` / `enabled` / `threshold` / `cooldown_seconds` / `muted_until` | 规则元信息，供页面解释「为什么报」 |
| `AlertSilence` | `rule_id`? / `inst_id`? / `until_ts` / `reason` / `created_by` | 静音规则；按规则或按品种，可叠加生效 |

`Alert.status` ∈ `new` / `acked` / `silenced`。静音期内的新告警仍入库并标 `silenced`，不丢记录，只是不推送邮件。

#### 4.9.2 存储

- **SQLite 新增 2 张表**：`alerts`（告警记录，**权威存储**）、`alert_silences`（静音记录，含到期时间）
- **Redis**：`quant:dashboard:alerts` 保留为实时推送缓存（看板轮询用），内容从 SQLite 冗余派生；缓存丢失不影响历史
- **日志**：保持 `logs/quant-trading.log` 单文件，10MB 轮转、30 天保留；模块名以固定前缀写在行内（如 `[risk.checker]`），检索靠 `grep` 与时间段定位

#### 4.9.3 接口

| 接口 | 签名要点 | 说明 |
|---|---|---|
| 告警写入 | `AlertService.publish(alert) -> str` | 统一入口：查静音 → 查冷却 → 落 SQLite → 推 Redis → 按需发邮件 |
| 告警查询 | `AlertService.list(level, rule, inst_id, status, start_ts, end_ts, limit, offset)` | 供看板分页与 M7-9 风控页引用 |
| 告警状态 | `AlertService.ack(alert_id)` / `silence(rule_id, inst_id, hours, reason)` / `unsilence(id)` | 已读与静音；静音不删历史记录 |

#### 4.9.4 需求条目

| 编号 | 功能项 | 详细要求 | 来源 | 状态 |
|---|---|---|---|---|
| M9-1 | 统一日志规范 | 单文件 `logs/quant-trading.log`，行内固定前缀标模块；10MB 轮转、保留 30 天；时间统一 UTC 并在行内明示时区 | 4.3（v0.5） | ✅ |
| M9-2 | 告警规则 | 已实现 11 条：worker/engine 心跳、队列堆积、死信新增、保证金率、余额突变、持仓漂移、撤单率、连续亏损、API 错误率、kill switch；阈值均在 `[monitor]` 可配。**规则级启停尚未实现**，需靠 M9-7/M9-10 的静音能力，故状态为 🟡 | F5.4 | 🟡 |
| M9-3 | 邮件告警 | SMTP 发往所有者邮箱；未配置（缺 host/username/to_addrs）时降级为 no-op，仅记日志，不阻断监控进程 | F5.5 | 🟡 |
| M9-4 | 监控自身心跳 | monitor 每轮写带 TTL 的心跳键；续期失败即视为自身异常；重启后检查上次心跳间隔，超时则产生「上次心跳中断」告警 | F5.6 | ✅ |
| M9-5 | 告警列表页 | 按时间倒序；展示级别、规则、对象（品种/策略）、状态。后端支持级别/规则/状态/时间区间筛选；**前端已支持级别、规则、状态三种筛选**，卡片标题同时显示命中总数与降级项；时间区间筛选前端未做入口 | 新增 | 🟡 |
| M9-6 | 日志检索 | **本期明确不做**；日志查看走文件（`grep` + 时间段），看板只提供最近告警，不提供日志全文检索 | 新增 | 🚫 |
| M9-7 | 告警确认与静音 | 可标记已读并记录操作人与时间；可对某规则或某品种静音 N 小时，静音期内新告警标 `silenced` 且不发邮件；可手动解除。**看板入口已补齐**：每条未读告警有「标记已读」，面板顶部展示生效静音（规则/品种/到期/原因）并可一键解除，静音表单可选规则、填品种（留空=该规则全部品种）、时长与原因；写操作走 `X-Dashboard-Request` 守卫，成功后立即重取 | 新增 | ✅ |
| M9-8 | 告警持久化 | 告警写 SQLite `alerts` 作为权威记录；Redis list 仅作实时推送缓存，掉电或重启不丢历史 | 新增 | ✅ |
| M9-9 | 去重与冷却 | 同一规则在冷却窗口（默认 600 秒）内只推一次；`kill_switch` 不参与冷却但需防刷屏；恢复类通知（`level=INFO`）不受冷却约束 | 新增 | ✅ |
| M9-10 | 静音粒度 | 支持三种：按规则（所有品种）、按规则+品种（单品种）、全局临时静音；多条静音叠加时任一命中即静音。**前两种已实现且有看板入口**（`rule` 必填、`inst_id` 可选）；全局静音未做——后端要求 `rule` 必填，前端也强制先选规则 | 新增 | 🟡 |
| M9-11 | 与业务事件联动 | 风控 `halt`/熔断（M7-17）、死信新增（M8-16）、回测任务失败（M4-12）、对账差异（M8-18）统一走 `AlertService.publish`；同一次事件按业务 ID 去重，不重复告警 | 新增 | 🟡 |
| M9-12 | 告警保留策略 | 按级别区分保留天数（ERROR 90 天、WARNING 30 天、INFO 7 天）；过期由保留清理任务删除，不影响日志文件保留 | 新增 | ⬜ |
| M9-13 | 存活邮件 | 每 `alive_email_hours`（默认 4 小时）发一封存活邮件，时间戳存 Redis 以便 monitor 重启后不重复发送 | 新增 | ✅ |
| M9-14 | 告警趋势统计 | 近 7/30 天各规则触发次数与 TOP 规则；供首页总览卡片使用 | 新增 | ⬜ |

**告警写入流程**：`触发方 → AlertService.publish → 查静音(AlertSilence) → 查冷却(rule+cooldown) → 落 SQLite alerts → 推 Redis 缓存 → 邮件(enabled 且非 silenced) → 日志`。

**不做**：日志全文检索（M9-6 明确不做）、Prometheus/Grafana 接入、告警升级链（先短信后电话）、外部推送（钉钉/企微）、日志集中采集（ELK/Loki）。

**前置条件**：M9-11 依赖 M7-17、M8-16、M4-12、M8-18 的事件入口；M9-3 依赖 SMTP 配置补齐（当前 P0 阻塞）。

**验收**：`quant test-email` 能真实发出并收到邮件；kill 掉 monitor 后重启会产生「上次心跳中断」告警；同一规则持续命中时冷却期内只出现一条；静音期内告警仍能在列表查到但状态为 `silenced` 且未发邮件；清空 Redis 缓存后历史告警仍可从 SQLite 查询。

---

### 4.10 系统设置（M10）

**职责**：配置看得见、改得对、出事能回到之前；凭证、进程、数据、备份集中管理。

**边界**：M10 **不自己实现参数覆盖**。热改白名单委托已有覆盖层——风控阈值走 M7-8 的 `risk_overrides`，策略参数走 M3-10 的 `strategy_params`；审计分别复用 M7-14 与 M3-8。M10 只提供统一入口、来源标注与只读展示，不引入第三套覆盖机制。

#### 4.10.1 数据模型

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `ConfigView` | `sections[]` → {`key` / `value` / `masked` / `source` / `editable`} | `source` ∈ `toml` / `env` / `default` / `override`；敏感字段恒 `masked=true` |
| `CredentialStatus` | `provider` / `env`（demo/live） / `present` / `age_days` / `last_verified_ts` / `verified_ok` / `message` | **不含任何密钥内容** |
| `ProcessCommand` | `cmd_id` / `name` / `action`（start/stop/restart） / `requested_ts` / `requested_by` / `status` / `result` | 命令与结果分离，异步执行 |
| `BackupManifest` | `backup_id` / `created_ts` / `size_bytes` / `scope[]` / `sha256` / `schema_version` | 与压缩包同目录同名 `.json` |

#### 4.10.2 存储

| 类型 | 位置 | 说明 |
|---|---|---|
| 进程命令 | Redis `quant:supervisor:cmd`（list）+ `quant:supervisor:cmd:result:{id}` | supervisor 轮询取命令、执行后写结果 |
| 凭证状态 | Redis `quant:cred:status` | 仅状态与时间戳，无密钥 |
| 备份 | `data/backups/{id}.tar.gz` + `{id}.json` | 清单文件即索引，不建表（避免「备份库里的备份索引」） |
| 配置热改 | 复用 `risk_overrides` / `strategy_params` | **M10 不新增 SQLite 表** |
| 保留策略 | `config.toml` 的 `[storage]` | 新增按周期分档字段 |

**新增配置项**：`retention_bars_days`（如 `{ "1m" = 365, "1H" = 1095 }`）、`retention_alerts_days`、`retention_dead_letters_days`、`retention_funding_days`。

#### 4.10.3 接口

| 接口 | 签名要点 | 说明 |
|---|---|---|
| 配置视图 | `ConfigService.view() -> ConfigView` | 只读，敏感打码，标注来源 |
| 热改 | `ConfigService.update_hot(key, value, by, reason)` | 仅白名单键；委托 M7-8 或 M3-10；非白名单直接拒绝并提示「需改 toml 重启」 |
| 凭证 | `CredentialService.status()` / `verify()` | `verify` 调只读接口（如 `/account/balance`）在**模拟盘**验证，结果写 Redis |
| 进程控制 | `ProcessControl.command(name, action) -> cmd_id` / `result(cmd_id)` | 写 Redis 命令，supervisor 执行 |
| 备份 | `BackupService.create(scope)` / `list()` / `restore(id, dry_run)` | `restore` 默认 dry-run，校验 sha256 与 `schema_version` 后才落盘 |

**CLI 新增**：`quant backup`、`quant restore`、`quant doctor`（配置/凭证/Redis/磁盘/端口自检）。

#### 4.10.4 需求条目

| 编号 | 功能项 | 详细要求 | 来源 | 状态 |
|---|---|---|---|---|
| M10-1 | 配置查看与编辑 | **只读展示**生效配置（`ConfigView`，敏感恒打码、标注来源）；仅白名单键可在线改，其余提示「需改 toml 并重启」 | 新增 | ⬜ |
| M10-2 | 凭证管理 | 展示 `CredentialStatus`（存在与否、已用天数、对应环境、上次验证结果），**不展示任何密钥内容**；一键在模拟盘验证。**当前无 `CredentialStatus` 实现、无接口**，仅有 `quant test-email` 可验证 SMTP | 4.4（v0.5） | ⬜ |
| M10-3 | 数据保留策略 | 现状已覆盖 bars / reports / signals / orders / fills / equity_snapshots，但只有**单一全局天数**；补按周期分档与 `alerts` / `dead_letters` / `funding_rates` 保留；清理可手动触发 | 新增 | 🟡 |
| M10-4 | 通知设置 | 看板可改 SMTP 主机/端口/发件人/授权码/收件人并发送测试邮件；授权码打码显示 | 新增 | ⬜ |
| M10-5 | 时间口径 | 内部与存储一律 UTC；仅展示层转 UTC+8；K 线时间戳为开盘时间 | 4.6（v0.5） | ✅ |
| M10-6 | 进程与自启 | 看板展示进程列表并支持启停重启；命令走 **Redis 命令通道**，supervisor 轮询执行并回写结果；开机自启归 MS6 | 4.5（v0.5） | ⬜ |
| M10-7 | 部署与备份 | `quant backup` / `quant restore` / `quant doctor`；看板展示上次备份时间与体积；上云与 systemd 归 MS6 | 4.5（v0.5） | ⬜ |
| M10-8 | 访问控制 | 看板仅监听本机；写操作需本机 Host + `X-Dashboard-Request` 头 + 可选 token；跨主机访问须走 HTTPS 反代 | 新增 | ✅ |
| M10-9 | 配置来源标注 | 每个配置项标注 `toml` / `env` / `default` / `override` 四态，便于定位「为什么生效值是这个」 | 新增 | ⬜ |
| M10-10 | 热改白名单 | 明确只允许在线改：风控阈值（M7-8）、策略参数（M3-10）、告警规则开关；其余一律拒绝并说明需改 toml 重启 | 新增 | ⬜ |
| M10-11 | 凭证到期提醒 | `age_days > 90` 时产生告警（接 M9-11），提醒按 M10-2 的轮换流程手工换 Key | 新增 | ⬜ |
| M10-12 | 备份完整性校验 | 备份写 `sha256` 与 `schema_version`；`restore` 默认 dry-run，校验通过才落盘；支持定期恢复演练 | 新增 | ⬜ |
| M10-13 | 保留策略分档 | 按周期设置不同保留年数（短线周期短、长线周期长）；业务表按类型单独配置；清理后不影响策略所需最小窗口 | 新增 | ⬜ |
| M10-14 | 进程命令审计 | 记录谁在何时对哪个进程执行了 start/stop/restart、结果如何；与 M9 告警联动（重启失败即告警） | 新增 | ⬜ |
| M10-15 | 部署自检 | `quant doctor` 一次检查：配置缺项、凭证可用性、Redis 连通、磁盘余量、端口占用，输出明确缺失项而非静默通过 | 新增 | ⬜ |

**热改流程**：`看板提交 → 白名单校验 → 委托 M7-8/M3-10 写覆盖表并留痕 → 刷新 Redis 缓存 → 下一轮生效`。非白名单键直接返回 400 并指明「需改 `config.toml` 后重启对应进程」。

**不做**：密钥托管/KMS、多用户与 RBAC、在线升级代码、容器化编排（归 MS6）、配置版本分支（靠 git）。

**前置条件**：M10-1/M10-10 依赖 M3-10 与 M7-8 的覆盖层；M10-11 依赖 M9-11 告警入口；M10-6 需 supervisor 增加命令轮询；M10-15 依赖 M10-2 的验证能力。

**验收**：`ConfigView` 响应中搜不到明文 `api_key` / `secret` / `passphrase` / SMTP 授权码；非白名单键热改被拒且提示明确；`quant restore --dry-run` 能发现 sha256 不符与 schema 版本不兼容；看板点「重启 worker」后 30 秒内进程恢复且审计有新记录；`quant doctor` 在缺 SMTP 配置时明确报出缺失项。

---

## 5. 非功能需求

### 5.1 性能

| 项 | 要求 | 关联模块 |
|---|---|---|
| 单次历史 K 线下拉 | 1 年 1H（约 8760 根）< 60 秒 | M2 |
| 回测速度 | 1 年 1H + 单策略 < 5 秒 | M4 |
| 信号到下单 | < 2 秒 | M8 |
| WebSocket 重连 | 首次重连 < 5 秒；连续失败时指数退避至上限 60 秒（带 ±20% 抖动） | M2 |
| 看板首屏 | < 3 秒（本机） | M1 |

### 5.2 可靠性

- 所有 HTTP 请求重试 3 次、指数退避
- 进程崩溃后必须可恢复：订单状态自检 + 对账后再启动
- 历史数据落盘前校验字段完整性，缺失则重拉
- 队列消费先入 inflight，避免进程崩溃丢信号

### 5.3 可维护性

- 模块边界清晰：行情/回测/交易/监控/因子/组合各自独立，通过明确接口通信
- 配置与代码分离：凭证、品种列表、风控参数一律走配置文件
- 日志统一格式，单文件 `logs/quant-trading.log` + 行内模块前缀，10MB 轮转、保留 30 天

### 5.4 安全性

- API Key 仅存本地配置文件，永不入库、永不打印到日志
- 实盘 Key 仅开交易权限，绑定本机出口 IP，不开提币
- 模拟盘与实盘 Key 分离，配置切换
- 实盘使用子账户，仅转入本期所需资金（< 1000 USDT），主账户资产隔离
- 凭证每 90 天轮换
- 看板只监听本机回环地址；写接口需本机 Host + `X-Dashboard-Request` 头，token 可选

### 5.5 部署

- 初期：Mac 本地，开发与模拟盘，supervisor 托管
- 后期：VPS，systemd 托管，实盘 7×24

### 5.6 时间口径

| 项 | 规范 |
|---|---|
| 内部存储与计算 | 一律 UTC（ISO 8601 带 `Z`，毫秒精度） |
| K 线时间戳 | 开盘时间（与 OKX 返回一致） |
| 本地展示 | 仅展示层转 UTC+8 |
| 签名时间戳 | OKX 要求 ISO 8601 UTC 毫秒，与服务端时差 < 30 秒 |
| 日志时间 | UTC，日志中明示时区 |
| 回测时间轴 | UTC；收益曲线展示按需转本地 |
| 跨日聚合 | 统计与报表按 UTC 日切分；**风控「日亏损」例外，按 UTC+8 日**（见 M7-3 口径说明），两类口径均须在 UI 标注时区 |

### 5.7 边界场景处理

| 场景 | 检测 | 处理 | 关联模块 |
|---|---|---|---|
| OKX 维护/停机 | 连续 3 次 5xx 或 `50001`/`50013` | 暂停所有策略；每 30 秒探测；恢复后先对账再启动 | M8/M7 |
| 极端行情 | 单根振幅 > 10% | 暂停该品种策略 N 分钟；告警；恢复需人工确认 | M7 |
| 部分成交 | `partially_filled` | 按累计成交量推进状态机；剩余挂单可配置（等待/改价/撤单） | M8 |
| 撤单回报未到 | 5 秒未收到回报 | 主动查询；仍为 `new`/`partially_filled` 视为撤单失败并重试 | M8 |
| 订单状态不明 | 本地与远端不一致 | 以远端为准；记 discrepancy；连续 3 次告警 | M8 |
| 余额不足 | 下单前预检 / `51008` | 停止该策略下单；尝试撤其他挂单释放保证金；告警 | M7/M8 |
| API Key 失效 | `50111`/`50113` | 立即停止所有策略；紧急告警；等待更换 | M8/M9 |
| 时钟漂移 | 与服务器时差 > 15 秒 | 自动校时；失败则停止下单 | M10/M8 |
| USD→USDC 现货迁移 | 2026-09-30 生效 | `instId` 用 `Crypto-USDC`；启动时拉取最新 instruments 验证 | M2 |
| post-only 挂单失败 | 2026-08-20 起仅推 `canceled` | 状态机不能假设先 `live`；收到 `canceled` 即视为失败 | M8 |
| RPI 低于最小名义 | SPOT<500 / FUT<2000 / SWAP<5000 USD | 返回 `54051`；调整金额或换非 RPI 类型 | M8 |
| 行情频道增量推送 | 2026-09-30 起 instruments 改增量 | 本地维护产品缓存按 `instId` 更新；不假设全量 | M2 |

---

## 6. 技术选型

### 6.1 已选型

| 层 | 选型 | 关联模块 |
|---|---|---|
| 语言 | Python 3.13 | 全部 |
| HTTP | `httpx` | M2/M8 |
| WebSocket | `websockets` 自封装 | M2 |
| 数据处理 | `pandas`（+ `polars` 备选） | M2/M4/M5 |
| 存储 | parquet（K 线/因子/回测曲线）+ SQLite（订单/成交/持仓/信号/告警/静音/死信/对账差异/风控事件/阈值变更/策略参数与启停/回测登记 + 组合快照）+ JSON/TOML（配置） | 全部 |
| 回测 | 自研轻量引擎（与 Pine 双回测） | M4 |
| Webhook | `FastAPI` + `uvicorn` | M8 |
| 看板后端 | `FastAPI`（同一进程提供 `/api/*`） | M1–M10 |
| 看板前端 | React 18 + TypeScript + Vite + ECharts | M1–M4/M8/M9 |
| 队列 | Redis LIST + BRPOP + 死信队列 | M8 |
| 去重 | Redis SET + `hash(signal_json)` + TTL 5min | M8 |
| 进程托管 | 本地 supervisor；上云 systemd | M10 |

### 6.2 后续按需引入

| 用途 | 候选 | 何时决定 |
|---|---|---|
| 机器学习策略 | `scikit-learn`、`xgboost`、必要时 `pytorch` | M4 稳定后 |
| 因子分析 | `scipy`（IC/相关性）、`statsmodels` | M5 启动时 |
| 组合优化 | 先用纯 Python 实现四种分配法；`cvxpy` 仅在需要约束优化时引入 | M6 启动时 |
| 监控可视化 | Prometheus + Grafana（替代自研告警页） | M9 扩容时 |
| 任务调度 | `apscheduler` 本地 / `Celery` 上云 | M6 再平衡需要时 |
| 容器化 | Docker Compose | M10 上云时 |

> 原则：能不引入就不引入；每个里程碑启动时回顾此表。

---

## 7. 项目结构

```
quant-trading/
├── docs/
│   ├── requirements/          # 需求文档（本文件 + v0.5 历史）
│   ├── specs/                 # 设计文档
│   ├── reference/             # OKX API 速查等
│   └── PROGRESS.md            # 开发进度
├── config/
│   ├── config.toml            # 全局配置（不入 git）
│   └── strategies/            # 各策略参数（不入 git）
├── src/quant/
│   ├── foundation/            # 配置、日志、错误、类型
│   ├── models/                # Bar/Tick/Instrument/Signal/Order/Position
│   ├── okx/                   # REST/WS 客户端、签名、限频
│   ├── storage/               # parquet、SQLite、校验、对齐
│   ├── fetcher/               # 数据下载编排
│   ├── backtest/              # 回测引擎、寻优、walk-forward、指标
│   ├── research/              # 回测任务编排与结果持久化（M4，未开始）
│   ├── strategies/            # pine/ 与 python/ 双副本
│   ├── factors/               # 因子库（M5，未开始）
│   ├── portfolio/             # 组合管理（M6，未开始）
│   ├── webhook/               # FastAPI 信号接收与去重
│   ├── execution/             # worker、下单、仓位
│   ├── risk/                  # 风控规则
│   ├── engine/                # 实盘策略引擎
│   ├── monitor/               # 监控、告警、心跳
│   ├── dashboard/             # 看板 API
│   ├── report/                # 日报
│   └── cli/                   # 命令行入口
├── web/                       # 看板前端
├── tests/
├── notebooks/
├── data/                      # parquet/JSON（不入 git）：candles/ instruments/ funding_rates/ factors/ backtests/ quality/ backups/ .checkpoints/
├── logs/                      # 日志（不入 git）
└── pyproject.toml
```

---

## 8. 里程碑

### 8.1 里程碑定义

| 阶段 | 范围 | 交付物 |
|---|---|---|
| MS1（已完成） | M2-1 ~ M2-8 | OKX 客户端、本地 parquet、校验与对齐 |
| MS2（已完成） | M4-1 ~ M4-5、M8-2 ~ M8-6、M7-1 ~ M7-3、M3-1 | 回测引擎 + 样例策略双副本；webhook + Redis + worker；风控规则与持久化 |
| MS3（已完成） | M8-1、M8-4 ~ M8-6、M8-12、M8-13 | 模拟盘跑通完整链路；执行页与减仓平仓可用 |
| MS4（进行中） | M9-1 ~ M9-5、M9-7、M9-8、M9-9、M9-11、M9-13、M1-1 ~ M1-7、M1-10、M7-4、M7-5、M7-10、M7-11、M7-15、M7-16、M4-6、M4-9 ~ M4-14、M8-14 ~ M8-16、M8-19、M3-9、M3-10、M3-11、M3-12、M2-15 | 监控告警与告警持久化、告警确认与静音、首页总览（含 7 进程健康与事件流）、kill switch 补齐撤单、风险指标、风控参数合并与事件落库、walk-forward 与回测结果落库对比、执行可靠性、策略参数与启停、数据质量报告 |
| MS5（未开始） | M8-7、M8-8、M8-11、M8-17、M8-18、M7-6 ~ M7-9、M7-12 ~ M7-14、M7-17、M9-10、M9-12、M9-14、M1-8、M1-9、M3-3、M3-4、M3-7、M3-8、M3-13 ~ M3-15、M4-7、M4-15、M4-16、M2-14、M10-1 ~ M10-4、M10-9 ~ M10-15，以及三条 🟡 的收口项 M3-5、M4-8、M9-5 | 实盘接入、真 webhook、进程编排、手工下单与三项对账、熔断与在线阈值调整、告警静音粒度与趋势统计、今日盈亏与告警摘要、研究工作流与策略详情页、批量回测、资金费率成本、Pine 对比、持仓量采集、配置视图与白名单热改、凭证健康、备份与自检 |
| MS6（未开始） | M10-6、M10-7 | 上云、systemd 托管与开机自启（进程自愈由 M8-11 承担） |
| MS7+（候选） | M5 全部（15 条）、M6 全部（14 条） | 因子库、组合管理 |

**矩阵口径说明**：

- 已标记 ✅ 但未在上表列出的条目（M2-9 ~ M2-12、M3-2、M3-6、M8-9、M8-10、M10-5、M10-8）随 MS1–MS3 一并交付，不单列。
- 标记 🚫 的条目（M2-13、M9-6）本就不进入任何里程碑，若后续重启需先改状态。
- MS1 ~ MS4 已有详设：[MS1 数据底座](../specs/2026-10-17-ms1-data-foundation-design.md)、[MS2 回测与 Webhook](../specs/2026-10-17-ms2-backtest-webhook-design.md)、[MS4-A 告警与首页](../specs/2026-10-30-ms4a-alerts-overview-design.md)、[MS4-B 风控与执行](../specs/2026-10-30-ms4b-risk-execution-design.md)、[MS4-C 回测与策略](../specs/2026-10-30-ms4c-backtest-strategy-design.md)。MS5/MS6 尚未出详设。

### 8.2 模块 × 里程碑矩阵

| 模块 | MS1 | MS2 | MS3 | MS4 | MS5 | MS6 | MS7+ |
|---|---|---|---|---|---|---|---|
| M1 首页总览 | | | | ◐ | ◐ | | |
| M2 市场监控 | ● | | | ◐ | ◐ | | |
| M3 策略研究 | | ◐ | | ◐ | ◐ | | |
| M4 回测分析 | | ● | | ◐ | ◐ | | |
| M5 因子库 | | | | | | | ● |
| M6 组合管理 | | | | | | | ● |
| M7 风险控制 | | ◐ | ◐ | ◐ | ◐ | | |
| M8 交易执行 | | ◐ | ● | ◐ | ◐ | | |
| M9 日志告警 | | | | ● | ◐ | | |
| M10 系统设置 | ◐ | | | | ◐ | ◐ | |

图例：● 主要交付　◐ 部分交付　空 不涉及

### 8.3 验收标准（DoD）

MS1–MS3 的逐条验收已达成，标准保留在 v0.5 第 3.7 节。MS4 起在各模块「验收」段落中以一句话形式给出，具体量化指标在对应里程碑的设计文档中细化。

---

## 9. 风险与开放问题

### 9.1 已识别风险

| 风险 | 等级 | 模块 | 应对 |
|---|---|---|---|
| OKX API 变更或限频收紧 | 中 | M2/M8 | 客户端封装层隔离；监控错误码 |
| 实盘亏损超预期 | 高 | M7/M8 | 严格风控 + kill switch + 小资金起步 |
| 时间戳偏差导致签名失败 | 中 | M8 | 定期校时（±30 秒内） |
| 策略过拟合 | 高 | M4 | walk-forward + 样本切分 + 与简单策略对比 |
| 本地断电/断网 | 中 | M8/M10 | MS6 上云；MS5 前小资金试错 |
| Redis 故障导致队列不可用 | 中 | M8 | BRPOP 退避重试 + 告警；LPUSH 失败返回 503 |
| TV alert 重发或丢失 | 中 | M8 | 去重（hash+TTL）+ 死信 + 持久化 |
| Pine 与 Python 策略不一致 | 高 | M3/M4 | 代码评审；偏离 > 5% 禁止上实盘 |
| worker 崩溃丢信号 | 中 | M8 | 先入 inflight 再处理；重启优先消费 inflight |
| 因子库/组合管理范围膨胀 | 中 | M5/M6 | 明确排到 MS7+，不阻塞实盘 |
| 看板堆积过多模块导致维护成本上升 | 中 | 全部 | 每个模块先做只读展示，写操作逐项批准 |

### 9.2 已确认的设计决策

| 问题 | 决策 | 影响 |
|---|---|---|
| 需求组织方式 | 按 10 个产品模块划分（v0.6） | 替代 v0.5 的 F1–F5 横向划分 |
| 模块是否等于页面 | 是，模块即看板导航项 | 非目标中删除「不做 Web UI」 |
| 旧编号处理 | 保留 F 编号作为「来源」列 | 可与旧 spec、旧进度记录对照 |
| 因子库/组合管理 | 写清需求但排 MS7+ | 不阻塞实盘 |
| 双回测 | Pine 为主，Python 交叉验证 | 偏离 > 5% 禁止上实盘 |
| Webhook | 信号型，`side/inst_id/order_type/strategy_id` 必填，`qty` 可选（缺省由服务端算仓位，已落地 M8-14；也可只给 `size_pct`） | Python 端负责风控与仓位（M8-14） |
| 架构 | FastAPI + Redis + worker | 多进程，先队列后执行 |

---

## 10. 变更记录

| 日期 | 版本 | 说明 |
|---|---|---|
| 2026-10-17 | v0.1–v0.5 | 见 [v0.5](2026-10-17-quant-trading-platform-requirements.md) 变更记录 |
| 2026-10-30 | v0.6 | 按 10 个产品模块重构全文：新增第 3 章模块总览、第 4 章十大模块详细需求（每条标注 v0.5 原编号与实现状态）、8.2 模块×里程碑矩阵；删除「不做 Web UI」非目标；新增因子库（M5）与组合管理（M6）两节基础设计；非功能需求、边界场景按模块重新归属 |
| 2026-10-30 | v0.6（补充） | 4.5/4.6 由 6 条基础设计扩写为完整设计：因子库 15 条 + 3 个数据模型 + parquet 存储布局 + 5 个接口（含时序/横截面双范围、增量一致性校验）；组合管理 14 条 + 4 个数据模型 + SQLite 两张新表 + 3 个接口（含四种分配法、再平衡冷却期、与 M7/M8 的边界）；技术选型补组合优化一项 |
| 2026-10-30 | v0.6（补充 2） | 4.4 回测分析由 8 条扩写为 16 条 + 5 个数据模型 + `data/backtests/{run_id}/` 文件布局 + 4 张 SQLite 表 + 4 个接口；新增结果持久化、历史列表与重跑、多组对比、后台任务与进度、寻优并发、扩展指标（Calmar/Sortino/月度/连亏/暴露度）、永续资金费率成本、Pine 结果导入对比；MS4 范围同步纳入 M4-9 ~ M4-14 |
| 2026-10-30 | v0.6（补充 3） | 4.8 交易执行由 13 条扩写为 19 条 + 6 个数据模型 + 2 张新 SQLite 表（`dead_letters`/`reconciliations`）+ `orders` 加 3 列 + 5 个接口；新增服务端仓位计算、订单状态机、死信查看与重放、手工下单与撤改、三项对账、下单幂等与重试边界；修正 `Signal.qty` 由必填改为可选的决策表述（9.2），消除与「Python 端负责仓位」的矛盾 |
| 2026-10-30 | v0.6（补充 4） | 4.7 风险控制由 9 条扩写为 17 条 + 5 个数据模型 + 2 张新 SQLite 表（`risk_events`/`risk_overrides`）+ 5 个接口；新增两层参数取严合并、风控事件落库、熔断恢复策略、熔断与 kill switch 职责分离、阈值变更审计、权益口径统一、规则边界单测、与告警联动；**修正 M7-6「极端行情熔断」的误标（✅ → ⬜，代码中并不存在）**；M7-2 明确为账户级口径 |
| 2026-10-30 | v0.6（补充 5） | 4.9 日志告警由 7 条扩写为 14 条 + 3 个数据模型 + 2 张新 SQLite 表（`alerts`/`alert_silences`）+ 3 个接口；新增告警持久化、静音粒度、与业务事件联动、按级别保留策略、存活邮件、趋势统计；**修正 M9-1 口径（「按模块+日期分文件」→ 单文件 + 行内模块标签，与实现一致）**；M9-6 日志检索改标 🚫 明确不做，并在第 3 章引入 🚫 状态定义 |
| 2026-10-30 | v0.6（补充 6） | 4.3 策略研究由 8 条扩写为 15 条 + 5 个数据模型 + 3 张新 SQLite 表（`strategy_params`/`strategy_param_changes`/`strategy_bindings`）+ 4 个接口；新增声明式 `PARAM_SPEC`、参数两层合并（toml 默认 ← DB 覆盖）、策略×品种启停矩阵、启停即时生效、双副本一致性校验、变更回滚、策略详情页；M3-4 由「单策略单品种」改为矩阵粒度并说明「同策略同品种只允许一个周期」 |
| 2026-10-30 | v0.6（补充 7） | 4.1 首页总览由 7 条扩写为 10 条 + 4 个只读聚合视图（不新增 SQLite 表）+ 3 个接口变更；新增今日盈亏汇总、告警摘要卡、降级与时效标注；**修正 M1-4 口径（「7/30/90 天」→ 实际 24h/7d/30d）**；M1-5 明确为 7 个进程各自心跳并新增 `quant:heartbeat:{name}`/`quant:process:{name}` 两个 Redis 键约定；MS6 移除重复的 M1-5 |
| 2026-10-30 | v0.6（补充 8） | 4.2 市场监控由 13 条扩写为 15 条 + 5 个数据模型 + 1 个新接口（`GET /api/quality`）；**修正三处与实现不符**：M2-2 由「tickers/books/trades + 缓存最近 N 条」改为「仅 tickers、每品种只留最新一条」、M2-4 由「资金费率与持仓量」拆为 M2-4 资金费率 + M2-14 持仓量（后者 ⬜ 完全未实现）、M2-10 由「SMA/EMA 均线」改为实际 7 类指标（MA/BOLL/SuperTrend/撑压/VOL/MACD/KDJ）；新增 `DataQualityReport` 与 `data/quality/` 落盘；M2-13 自选对比改标 🚫 |
| 2026-10-30 | v0.6（补充 9，**十大模块细化完毕**） | 4.10 系统设置由 8 条扩写为 15 条 + 4 个数据模型 + 5 组接口 + 3 个新 CLI（`backup`/`restore`/`doctor`）；新增配置来源标注、热改白名单、凭证到期提醒、备份完整性校验、保留策略分档、进程命令审计、部署自检；**修正四处与实现不符**：M10-1 由 🟡 改 ⬜（看板无任何配置接口）、M10-3 由 ✅ 改 🟡（仅单一全局天数，缺 alerts/dead_letters/funding_rates 与按周期分档）、M10-4 由 🟡 改 ⬜、M10-2 重写为「健康看板 + 模拟盘验证」；明确进程控制走 Redis 命令通道，M10 **不新增 SQLite 表**（热改与审计复用 M3/M7） |
| 2026-10-30 | v0.6（补充 10，全篇一致性复核） | 修掉 3 处 v0.5 遗留口径：§5.3 日志「按模块+日期分文件」→ 单文件 + 行内前缀（与 M9-1 一致）、§9.1 风险表「M6 上云/M5 小资金」→ MS6/MS5、§7 `data/` 目录补 `instruments/ funding_rates/ .checkpoints/`；§5.1 WebSocket 重连改为「首次 < 5 秒、最大退避 60 秒」、§5.4 写接口改为「本机 Host + 自定义头，token 可选」；**修正两处与实现不符**：M9-2 由 ✅ 改 🟡（11 条规则均有阈值但无规则级启停）、M2-4 由 ✅ 改 🟡（只取最新资金费率，未接 `funding-rate-history`，无法回补历史，M4-15 需先补）；补充 `PineBaseline` 复用 `backtest_metrics` 列的口径、`max_order_qty` 实为名义价值的字段说明；M4-6（walk-forward）由「随 MS1–MS3 交付」改列 MS4 |
| 2026-10-30 | v0.6（补充 11，重点模块深度复核） | **M3/M4/M5/M7 逐条对代码复核**，修正 8 处状态与 3 处逻辑缺口：M3-3 改 ⬜（无表单无写入）、M3-4 改 ⬜（engine 为单进程单策略单品种，矩阵需先改多绑定）、M3-5 改 🟡（只有粗粒度 tick 状态，无法区分策略内部拦截）、M3-7 改 ⬜（`notebooks/` 只有 `.gitkeep`）、M3-2 措辞修正（无启动期注册表，「只读」归 M3-9）；M4-7 改 ⬜（无跨品种批量）、M4-8 改 🟡（偏离结论只进日报邮件，未进告警体系）、M4-10/M4-11 由 ⬜ 改 🟡（前端已有 localStorage 摘要历史与指标对比表）、M4-5 标注 500 组上限仅看板路径生效；M8-14 改 ⬜（`Signal.qty` 与 webhook 均为必填，无 `PositionSizer`）、§9.2 webhook `qty` 改为「目标可选、当前必填」；M9-5 改 🟡（仅级别+规则筛选）；M2.1 `Bar` 补 `vol_quote`/`confirm`、`Instrument` 改为实际 snake_case 字段，M2-5 删除「保留 OKX 原始字段名」（实际已转 snake_case）；**新增两处设计闭环**：M5 窗口硬校验（`窗口 ≥ max(因子 lookback)+1`，不足报错不降级）与 `Context` 两处构造点；M7 新增「日切口径说明」（风控 UTC+8、报表 UTC，须在 UI 标注），并同步 §5.6 |
| 2026-10-31 | v0.6（补充 12，状态回填） | 按当前代码逐条复核并回填状态：M3-3/3-4/3-8/3-9/3-10/3-11/3-12 改 ✅（参数表单、变更记录、`PARAM_SPEC`、两层合并、启停矩阵与即时生效均已落地）；M4-9/4-12/4-13/4-14 由 ⬜ 改 🟡、M4-10/4-11 更新说明（后端持久化/任务队列/并发/扩展指标已完成，前端仍用 localStorage 与同步路径）；M7-10/7-11/7-15/7-16 改 ✅（来源标注、事件落库、严格权益口径默认关闭兜底、规则边界单测齐备）；M9-8 改 ✅、M9-7 与 M9-10 由 ⬜ 改 🟡（ack/silence 后端就绪、前端无入口，缺全局静音）、M9-5 补后端筛选能力；M10-2 由 🟡 改 ⬜（无 `CredentialStatus` 实现）；M4-5 修正上限说明（`backtest.max_combos` 由看板与 CLI 共用）；同步更新 §4.3/§4.4 前置条件 |
| 2026-10-31 | v0.6（补充 13，急停撤单落地） | M7-4 由 🟡 改 ✅：看板急停激活时默认调用 `ManualTrader.cancel_open_orders` 撤销交易所全部挂单并同步本地订单状态（交易所为零基准），可勾选「仅停止不撤单」；撤单失败只回报错误、不回滚停止标志，避免「以为停了其实没停」 |
| 2026-10-31 | v0.6（补充 14，数据质量报告落地） | M2-15 由 ⬜ 改 ✅：新增 `storage/quality.py`，candles 每轮校验后把结果并入 `data/quality/{inst}/{bar}/latest.json`，跨次累计缺口 `open`/`repaired`（窗口外旧缺口保持 open，不误判修复）；`quant check-data` 支持手动全量校验；`GET /api/quality` 返回摘要或单份完整报告，看板「数据质量」面板可按缺口展开；monitor 对有未修复缺口的品种产生 `data_quality` WARNING，按 `(inst, bar)` 分别冷却（默认 1 小时），新增 `[quality]` 三项配置 |
| 2026-10-31 | v0.6（补充 15，死信查看与重放落地） | M8-16 由 ⬜ 改 ✅：迁移 v7 建 `dead_letters`，`SignalQueue` 双写（Redis 保留计数告警、SQLite 为权威记录），`stage` 收敛为 `parse`/`submit`/`reconcile`；新增 `DeadLetterService` 与 `GET /api/dead-letters`、`POST /api/dead-letters/{id}/replay`、`POST /api/dead-letters/replay`、`POST /api/dead-letters/{id}/discard`；重放前查 `signals.status`，已 `executed`/`rejected` 返回 `already_processed` 不重复下单，并用 `replayed_at IS NULL` 条件更新防并发双击；看板新增「死信」面板（阶段筛选、payload 展开、单条/批量重放、丢弃）；`[storage] dead_letter_persist` 作为灰度回滚开关 |
| 2026-10-31 | v0.6（补充 16，极端行情熔断落地） | M7-6 由 ⬜ 改 ✅：新增 `risk/circuit_breaker.py`，按 `(high−low)/open` 严格大于阈值判定，状态存 `quant:risk:circuit:{inst_id}` 并带 TTL 自动到期（该 TTL 行为已被补充 17 改为人工确认恢复）；引擎每轮检查待评估 bar，触发后跳过该品种、推进水位（恢复后不补评估过期 bar）、落 `risk_events` 并产生 `circuit_breaker` 告警；worker 对熔断品种的信号直接拒绝（不触发 kill switch）；看板「风险规则」显示熔断中的品种与预计解除时间；`[risk] circuit_enabled`/`circuit_amplitude_pct`/`circuit_cooldown_seconds` 三项配置，前两项默认 10% 与 900 秒。M7-13 由 ⬜ 改 ✅（状态键与 kill switch 完全分离，单测覆盖）；M7-17 由 ⬜ 改 🟡（熔断告警分支已接，`halt` 告警未接）。M7-12 仍 ⬜：当前冷却结束后自动恢复，人工确认恢复待后续实现 |
| 2026-10-31 | v0.6（补充 17，熔断人工确认恢复落地） | M7-12 由 ⬜ 改 ✅：熔断键改为不设 TTL，冷却到期后 `status` 跃迁为 `awaiting_resume`，引擎与 worker 继续拦截该品种；新增 `DashboardService.resume_circuit()` 与 `POST /api/risk/circuits/{inst_id}/resume`（写操作守卫），只允许冷却结束后调用，恢复时写 `decision=resumed` 的 `risk_events` 并发 INFO 告警；看板「风险规则」对 `awaiting_resume` 显示「确认恢复」按钮，恢复后立即重取；`[risk] circuit_resume_requires_confirm=false` 可回滚到 TTL 自动恢复。同步修正 M7-6 描述与 `CircuitBreakerState` 字段表（`status` 实际为 `tripped`/`awaiting_resume`，新增 `requires_confirm`） |
| 2026-10-31 | v0.6（补充 18，熔断键保留风险入档） | 修正 §4.7.3 中 `quant:risk:circuit:{inst_id}`「带 TTL」的过期描述（默认不设 TTL）；M7-12 补充已知风险：熔断键永久保留，长期无人确认会让该品种持续不开新仓，`awaiting_resume` 期间 bar 不补评估，Redis 未持久化重启会丢失熔断状态（等同自动恢复）；`docs/reference/RUNBOOK_PAPER_TRADING.md` 新增 §4.1 熔断处置（查看/恢复/删键应急/回滚开关）与「某品种一直不开仓」排查项 |
| 2026-10-31 | v0.6（补充 19，阈值在线调整与审计落地） | M7-8 由 ⬜ 改 ✅、M7-14 由 ⬜ 改 ✅：新增 `risk/overrides.py`（`RiskOverrideService.set/clear/list`），覆盖值写 Redis hash `quant:risk:overrides`（字段 `{strategy}:{param}`）、审计追加写 `risk_overrides`（只追加、撤销置 `revoked_at`）；写入顺序为「先 Redis 后审计」，审计失败回滚 Redis；变更原因必填，比例类字段约束 (0,1]。worker 新增 `_refresh_limits()`，每轮心跳重读覆盖层并重建 `RiskConfig`，因此最长 60s 生效、无需重启；Redis 读失败保留上一轮生效值（不静默退回配置）。新增 `GET/POST /api/risk/overrides` 与 `DELETE /api/risk/overrides/{strategy_id}/{param}`（写操作守卫）；看板「风控限额」内嵌在线调整表单、生效覆盖 chip（可一键清除）与变更历史，规则行补 `default/global/strategy/override` 来源标注。M7-9 保持 🟡（来源已展示，触发次数仍缺接口） |
| 2026-10-31 | v0.6（补充 21，告警确认与静音看板入口） | M9-7 由 🟡 改 ✅：日志告警页每条未读告警可「标记已读」（后端幂等，保留首次确认人与时间）；面板顶部列出生效静音（规则/品种/到期/原因）并可一键解除；静音表单可选规则、填品种（留空=该规则全部品种）、时长与原因。M9-5 补状态筛选（未读/已读/已解决）并显示命中总数与降级项，时间区间筛选仍未做，保持 🟡。M9-10 仍为 🟡（按规则、按规则+品种已可用，全局临时静音受后端 `rule` 必填约束未做）。同时修复告警时间显示：SQLite 权威行字段是 `created_at`，此前前端只读 Redis 行的 `ts`，导致列表时间为空 |
| 2026-10-31 | v0.6（补充 22，数据新鲜度） | M1-7 由 ⬜ 改 ✅：新增 `GET /api/freshness` 与看板首页「数据新鲜度」卡。`lag_bars` 直接复用 `/api/engine` 口径（本地最新已收盘 bar vs 引擎水位）；额外计算 `missing_bars` = 本地最新 bar 相对「此刻应有已收盘 bar」的缺口，因为断网时 candles 与 engine 会一起卡在同一根 bar 上、`lag_bars` 恒为 0，只看 `lag_bars` 会误判为「完全同步」。阈值为 1 根（PRD 验收要求落后超过 1 根必须标红，取 1 可提前一根预警），`stale = lag_bars ≥ 1 或 missing_bars ≥ 1`。卡片分别提示「engine 落后 N 根」与「本地缺 N 根」，并展示最新已收盘 bar 时间、距今多久、engine 水位与降级来源。本期只返回 engine 主组合（engine 目前只发布该组合的水位与状态键），多绑定逐组合需 engine 另行发布各组合水位。RUNBOOK 新增 §4.3 排障表 |
| 2026-10-31 | v0.6（补充 23，下单前预检下沉 worker） | M7-7 由 🟡 改 ✅：新增 `PreflightChecker` 并在 worker 调用 `executor.execute` 前执行。风控回答「该不该下这么多」（可调、按策略），预检回答「这一单交易所收不收」（硬约束、不随策略变），失败直接拒单并落 `risk_events`，不进 broker、不进死信。校验四项：产品配置存在性、`lotSz` 精度、`minSz` 最小量、最小名义金额；现货额外校验可用余额（预留 2% 余量、快照缓存 15s），合约跳过余额校验（杠杆下可用保证金需由交易所 51008 判定）。**关键取舍**：预检返回的 `qty` 是 `lotSz` 归一化后的基础币数量，worker 必须用它下单，否则 broker 静默取整会导致本地记录与成交不符；合约按 `contracts = floor(qty / ct_val, lotSz)` 折算，`minSz` 比的是张数而非基础币。余额读失败放行（`ok=true` 且 `balance_checked=false`），预检是保护而非闸门，不能因行情接口抖动阻断正常下单。仅 `--broker okx` 且直连客户端可用时装配，仿真 broker 与 `--broker none` 不受影响；配置项见 `config.toml.example` 的 `[risk] preflight_*`。`tests/test_preflight.py` 20 项覆盖四类校验 + worker 集成（归一化数量生效、拒单无成交、无预检时行为不变） |
| 2026-10-31 | v0.6（补充 24，服务端仓位与状态机收口） | M8-14 由 ⬜ 改 ✅：新增 `PositionSizer`（`src/quant/execution/sizing.py`），`Signal.qty` 改为可选并新增 `size_pct`，webhook `qty` 同步放宽为可选。比例三级回落（`qty` → `size_pct` → `[execution] default_size_pct`，默认 0.1，非正值在配置加载期即报错），算量 `权益 × 比例 ÷ 价格` → `max_order_qty` 截断 → 按 `lotSz` 向下取整；合约先除 `ctVal` 折张再取整且 `minSz` 比张数；算量为 0 或低于 `minSz` 直接拒单，不进 broker、不进死信。**执行顺序按依赖调整为「先补量、再风控」**：风控规则本身需要数量才能算名义价值，补出的量仍会被风控继续下调并再过预检，因此不构成绕过。每次补量写一条 `decision="sized"` 的 `risk_events`（`qty_before` 取整前、`qty_after` 实际下单量、`rule` 截断来源），回答「这单数量从哪来」；`[execution] server_side_sizing=false` 为回滚开关，关掉后无 `qty` 的信号直接拒单。产品规格读取失败退回模拟精度，交易所精度由 M7-7 预检兜底。M8-15 由 ⬜ 改 ✅：状态机与成交累加此前已落地，本次补上最后一个缺口——`save_order` 由「整行覆盖」改为「插入或忽略 + 状态机推进」，重复落库（死信重放、崩溃恢复）不会把 `filled` 回退成 `pending`；补 `save_order` 回退与部分成交累加测试 |
