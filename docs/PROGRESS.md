# 项目进度与经验总结

> 更新时间：2026-10-30
> 项目：`quant-trading`（OKX 模拟盘量化交易系统）
> 依据：[PRD v0.6](requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md)

---

## 1. 一句话现状

数据、回测、信号、下单、监控、看板六段链路全部打通，**OKX 模拟盘已在自动跑**；
M1–M3 完成，M4 完成约 95%（含 engine 心跳与恢复通知；只差填 SMTP 账号实测），M5 实盘未开始。

### 量化快照

| 指标 | 数值 |
|---|---|
| 源码 | 107 个 Python 文件 / 约 14,686 行 |
| 测试 | 56 个测试文件 / 约 19,371 行 / **1,024 条全部通过** |
| 前端 | 11 个文件 / 约 6,393 行（React 18 + TS + Vite + ECharts） |
| CLI 子命令 | 20 个 |
| 本地行情 | BTC-USDT-SWAP 与 BTC-USDT 均覆盖 5m/15m/30m/1H/4H/6H/12H/1D 八周期（10 MB parquet） |
| 当前策略 | `sma_cross`（4H，fast=10 / slow=100 + EMA200 趋势过滤） |
| 正在运行的进程 | supervisor + engine + monitor + dashboard |

---

## 2. 里程碑进度

| 里程碑 | 范围 | 状态 | 说明 |
|---|---|---|---|
| M1 数据底座 | F1.1–F1.8 | ✅ 100% | OKX 客户端、限频、parquet 存储、校验、对齐、断点续传 |
| M2 回测 + Webhook 框架 | F2.1–F2.6、F3.2–F3.7 | ✅ 100% | 回测引擎、样例策略、webhook、Redis 队列、worker、风控、SQLite |
| M3 模拟盘 | F3.1、F3.5–F3.7 | ✅ 100% | 真实 OKX 模拟盘撮合验证，最大偏离 3.56%（< 5% 阈值） |
| M4 监控 | F5.1–F5.6 | 🟡 约 95% | 账户/持仓/指标/10 条告警/恢复通知/心跳/存活邮件/看板均已完成；只差 SMTP 账号实测 |
| M5 实盘 | F4.1–F4.8 | ⬜ 0% | 依赖 TradingView Pro，尚未开始 |
| M6 上云 | systemd/systemd 编排 | ⬜ 0% | 未开始 |

按 6 个里程碑计约 **60%**；按「实盘前必须完成」的 M1–M4 计约 **99%**。

### M4 未完成项

| 项 | 现状 |
|---|---|
| F5.5 邮件告警 | 代码与测试完成，`[smtp]` 配置项已就位但值为空；当前降级为只记日志 |
| SMTP 实测 | `quant test-email` 可直接验证，需要填入真实 host/账号/收件人后执行一次 |

> 存活邮件（F5.6）已实现：每 `alive_email_hours` 发一封，上次发送时间存 Redis，
> monitor 重启不会立刻补发；同样只差 SMTP 配置。

---

## 3. 已完成的功能清单

### 3.1 数据层（M1）

- `OKXClient`：HMAC 签名、按端点组令牌桶限频、50011 自动退避、重试、时间同步
- `BarStorage`：按 `candles/{type}/{inst}/{bar}/{year}.parquet` 落盘，写入自动去重
- `CandleFetcher`：断点续传、翻页拉历史、缺口自动补拉、校验报告
- `DataValidator` / `DataAligner`：连续性、重复、OHLC 合法性校验与多品种对齐
- 产品配置、资金费率拉取

### 3.2 回测与策略（M2）

- 事件驱动回测引擎：下一根 bar 开盘成交、滑点与手续费、逐根权益、最大回撤/夏普/胜率/盈亏比
- 网格寻优：非法参数组合跳过而非中断，`max_drawdown` 等按「越小越好」排序
- 样例策略：`sma_cross`、`rsi`，参数与风控写在 `config/strategies/*.toml`
- **区间预热**：`--start` 之前的 bar 只驱动策略状态，不成交、不计入指标
- CLI：`quant backtest`、`quant optimize`，可输出 CSV/HTML 报告

### 3.3 执行链路（M2–M3）

- Webhook server：TV alert 解析、5 秒内 ACK、失败入死信、hash 去重
- Redis 队列 + worker：BRPOP 消费、失败重试与死信、心跳
- 风控：单笔/单品种/总仓位上限、日亏损上限、连续亏损保护
- OKX 模拟盘执行：市价/限价、状态轮询、成交落库 SQLite
- kill switch：手动紧急停止，engine/worker 均尊重该标志
- `quant simulate-alert`：本地模拟 TV 推送，M3 全链路验证不依赖 TV

### 3.4 监控与报告（M4）

- monitor 每轮：账户快照、持仓快照、风险指标（回撤/撤单率/强平距离）、规则告警、写 Redis + SQLite
- 告警规则（10 条）：worker / engine 心跳超时、队列堆积 > 50、死信新增、保证金率、
  余额波动、撤单率、连续亏损、API 错误率、kill switch
- 告警恢复：持续型规则从命中转为正常时补发一条 `INFO 已恢复`，邮件与看板都不会停在旧故障
- 存活邮件：每 `alive_email_hours`（默认 4h）一封，上次发送时间存 Redis，重启不重复
- 邮件通道：`quant test-email` 一键验证 SMTP 是否真能到达邮箱
- 每日偏离日报：回测与模拟盘同窗口比对，超 5% 标 FAIL，CSV 落盘
- 数据保留清理：`retention_days` 生效，K 线/SQLite/日报按天清理，`quant prune` 可手动执行
- 只读看板：FastAPI + React，5 秒轮询

### 3.5 本地实时信号引擎（PRD 之外的补充）

- 每根已收盘 bar 用本地 parquet 回放策略，信号入 Redis 队列交给 worker
- 水位存 Redis，重启不重复下单；每根 bar 只评估一次
- kill switch 生效或有未完成挂单时跳过
- 价值：不依赖 TradingView 也能产出实时信号，TV 只保留为「主回测 + 交叉验证」角色

### 3.6 看板功能（本期重点）

| 面板 | 内容 |
|---|---|
| 顶栏状态 | kill switch、worker/monitor 心跳、队列/死信、告警 ERROR 计数、数据源降级 |
| 账户权益 | 总权益、可用/冻结、未实现盈亏、保证金率、权益曲线（24h/7d/30d 切换，带区间收益与区间回撤） |
| 风险指标 | 最大回撤、撤单率、最小强平距离、挂单/处理中 |
| 策略引擎 | 在线状态、最近评估 bar、水位延迟、本轮/累计补评估、跳过与信号计数、当前策略参数 |
| K 线 | lightweight-charts，周期 1H/4H/1D（按磁盘实际数据渲染按钮）、买卖点标记、持仓均价线 |
| 回测面板 | 单次回测 / 参数寻优，指标卡 + 权益曲线 + 成交明细 + 参数排名；回测结果自动记入本地历史，可勾选多条横向对比 |
| 持仓 / 告警 / 订单 / 成交 | 本地 SQLite + monitor 快照合并展示；告警按规则聚合，可筛级别并展开明细 |
| 成交盈亏 | FIFO 回合统计：已实现盈亏、胜率、盈亏比、平均盈利/亏损、累计手续费，附最好/最差与统计区间 |
| 模拟盘日报 | 读取 `reports/daily/*.csv`，列出每日偏离报告；点击展开「模拟盘 vs 回测」五项比对，超阈值标红 |

后端接口：`/api/overview`、`/api/positions`、`/api/orders`、`/api/fills`、`/api/pnl`、`/api/reports`、`/api/alerts`、`/api/equity`、`/api/engine`、`/api/candles`、`/api/strategies`、`/api/backtest`、`/api/optimize`。

---

## 4. 关键数据与实测结果

### 4.1 行情数据现状

| 品种 | 周期 | 根数 | 覆盖区间（UTC） |
|---|---|---|---|
| BTC-USDT | 1H | 17,534 | 2024-10-26 00:00 → 2026-10-26 13:00 |
| BTC-USDT | 4H | 4,383 | 2024-10-26 00:00 → 2026-10-26 08:00 |
| BTC-USDT | 1D | 729 | 2024-10-26 16:00 → 2026-10-24 16:00 |
| ETH-USDT | 1H | 17,534 | 2024-10-26 00:00 → 2026-10-26 13:00 |
| ETH-USDT | 4H | 4,383 | 2024-10-26 00:00 → 2026-10-26 08:00 |
| ETH-USDT | 1D | 729 | 2024-10-26 16:00 → 2026-10-24 16:00 |

六个组合回填后校验均为 `missing=0 / duplicates=0 / invalid_ohlc=0`。
注意 `[storage] retention_days` 必须大于回填跨度，否则每日清理会把早期历史删掉
（见 §5.1 第 5 条）。

### 4.2 回测

短样本（BTC-USDT 1H，2026-09-15 ~ 2026-10-24，预热 200 根）：

| 指标 | 值 |
|---|---|
| 总收益 | -1.52% |
| 夏普 | -0.33 |
| 最大回撤 | 12.97% |
| 胜率 | 14.29% |
| 盈亏比 | 0.69 |
| 成交 | 14 轮（28 笔 fill） |

长样本（BTC-USDT 1H，2024-11-10 ~ 2026-10-26，17,174 根，预热 200 根）：

| 指标 | 值 |
|---|---|
| 总收益 | -41.94% |
| 年化 | -24.22% |
| 夏普 | -0.77 |
| 最大回撤 | 53.38% |
| 胜率 | 25.94% |
| 盈亏比 | 0.61 |
| 成交 | 239 轮（478 笔 fill） |

结论：链路正确，默认参数（fast=10/slow=50）在两年样本上持续亏损。
但要看清亏损的性质——OOS 区间本身是熊市（见 4.3），**不能据此判定策略失效**。

### 4.3 样本内 / 样本外验证

切分：样本内 2024-11-10 ~ 2025-09-30（牛市），样本外 2025-10-01 ~ 2026-10-26（熊市）。
基准：同期买入持有 BTC **+48.97%** → **-29.82%**；ETH **+34.76%** → **-43.84%**。

| 品种·周期 | 参数（样本内最优） | 样本内夏普 | 样本外总收益 | 样本外夏普 | 样本外汇比 | 轮数 |
|---|---|---|---|---|---|---|
| BTC 1H | 30/80 | 0.30 | -25.01% | -0.84 | 0.57 | 66 |
| BTC 1H | 40/80 | 0.25 | -29.68% | -1.06 | 0.51 | 61 |
| BTC 1H | 30/100 | 0.01 | -37.13% | -1.44 | 0.40 | 58 |
| ETH 1H | 30/50 | 0.39 | -36.82% | -0.61 | 0.61 | 109 |
| ETH 1H | 30/150 | 0.29 | -57.40% | -1.34 | 0.35 | 53 |
| BTC 4H | 10/100 | 0.64 | **+1.33%** | **+0.18** | 0.95 | 18 |
| BTC 4H | 20/100 | 0.60 | -1.02% | +0.10 | 0.87 | 14 |
| ETH 4H | 10/100 | 0.81 | -3.42% | +0.11 | 0.86 | 20 |

三个结论：

1. **样本内正夏普全部消失**：1H 两组都是「样本内 +0.3 左右 → 样本外 -0.6 ~ -1.4」，
   参数确实过拟合，且换品种、换参数都救不回来。
2. **但 1H 并没有输给基准**：BTC 1H 30/80 亏 25%，同期买入持有亏 29.8%；
   ETH 1H 30/50 亏 36.8%，买入持有亏 43.8%。**亏得比躺平少**。
3. **4H 是唯一在样本外转正的周期**：轮数从 60~109 降到 14~20，
   成本从 18~33pp 降到 4~6pp，毛利得以保留。

成本口径：`commission_rate=0.001`（单边）+ `slippage_bps=5` ⇒ **每轮往返约 0.3%**。
1H 的 66 轮 ≈ 20pp 成本，正好抵消毛利；4H 的 18 轮 ≈ 5pp，可以承受。
**结论：亏损主因是换手率，不是信号质量。优先降频，而不是继续调均线长度。**

### 4.4 模拟盘

M3 端到端测试最大偏离 3.56%，低于 5% 阈值，判定 PASS。

当前状态（2026-10-26）：`fills` 表 **0 笔**，engine 日志连日 `no signal`。已核实
不是 Bug：最后一次金叉在 10-20 17:00（UTC），而 engine 进程 10-25 21:18 才启动，
未赶上；10-22 之后的死叉因空仓无需平仓，此后 `fast <= slow` 持续至今。
用 engine 相同回放逻辑验证，10-20 17:00 若在跑会产出 `buy qty≈0.1206`。

---

## 5. 工程经验（踩过的坑）

### 5.1 数据同步

1. **`sync-candles` 默认只回看 3 根**：首次同步新周期只会落 3~4 根，图表几乎空白。首次回填必须显式 `--lookback-bars 500` 或用 `fetch-candles --start/--end`。
2. **OKX 日线以 UTC+8 零点为界**：1D 的 bar 时间戳是 16:00 UTC，当天这根要到 UTC 16:00 才收盘，所以「最后一根缺失」是正常现象，不是同步失败。
3. **断点会挡住历史回填**：`fetch()` 的 cursor 取 `max(start_ts, ckpt.last_ts + bar_ms)`，往旧区间补数据要先清对应断点。
4. **每轮同步要回退重拉**：`sync()` 主动把断点回退 `lookback+1` 根，否则上一轮落盘的未收盘 bar 永远不会被修正成完整 OHLC。
5. **保留策略会和回填打架**：`retention_days` 默认 365，回填 2 年后第二天 03:30 的
   自动清理就会删掉一年历史，表现为「明明拉过，回测起点又变回一年前」。
   回填跨度必须 ≤ `retention_days`（本次已调整为 1095）。
6. **深拉一次后要把断点写回最新**：`fetch()` 每页把断点更新成该页最新一根，
   超大区间拉完后断点会停在最旧一页。若不写回最新，`sync-candles` 会误以为
   落后两年，每轮都重新下载。本次回填脚本末尾显式把断点重置为最新已收盘 bar。

### 5.2 回测口径

5. **冷启动会污染指标**：不加预热时，SMA 窗口和 RSI 的 Wilder 平滑在区间起点还没就绪，前几十根不出信号，回测结果失真。因此引入 `warmup_bars`：照常喂策略，但信号丢弃、不成交、不计入权益；预热不足直接报错。
6. **口径必须单一来源**：CLI、Web、日报都复用 `backtest/runner.py` + `Backtest`，避免三处各写一套导致数字对不上。
7. **非法参数要跳过而不是中断**：`fast_len >= slow_len` 这类组合在网格里很常见，逐组 try/except，全部失败才抛错并带上首条原因。
8. **`inf` / `NaN` 不是合法 JSON**：`profit_factor` 在无亏损时是 `inf`，序列化前统一转 `null`，前端显示「—」。
9. **日报是 warmup 语义的例外**：`warmup_bars` 只保证指标就绪，信号丢弃不成交；
   但日报要和模拟盘比对，必须还原「窗口起点时刻的持仓」，所以改为从本地最早
   一根 bar 连续成交。同一份数据实测：回看 200 根 → 假偏离 52%，全量历史 → 0%。
   两者数字不同属预期，不要当 Bug「统一」。前提是本地 K 线不晚于模拟盘开户时间。

### 5.3 前端图表

10. **成交时间不一定落在 bar 开盘时间上**：标记点会丢，需要把 fill 时间吸附到 `<= ts` 的最近一根 bar。
11. **周期按钮应来自磁盘而不是硬编码**：后端 `list_bar_sizes()` 扫目录，前端只渲染真有数据的周期，避免点了 4H 看到空白。
12. **`setData` 全量替换 + `ResizeObserver` 调宽度**：比每帧重绘稳，也避免图表容器宽度为 0 时渲染异常。
13. **`fitContent()` 只在切换标的后调用一次**：每次轮询都调用会让用户无法缩放/拖动。

### 5.4 测试与工程

14. **fixture 作用域**：类内 fixture 无法跨类复用，多个测试类要用的环境要提到模块级。
15. **时区对齐**：测试里的 `START` 必须用 `datetime(..., tzinfo=UTC).timestamp()` 生成，不要用「取整到天」的魔数，否则与 CLI 的日期字符串差 8 小时。
16. **只读服务不要落盘**：看板回测明确不写 CSV/SQLite，页面刷新即丢弃，避免「看一眼图表就在生产目录留下文件」。
17. **串行化重计算**：回测/寻优用 `asyncio.to_thread` + `Semaphore(1)`，防止多标签页同时点按钮打满 CPU。
18. **结果要裁剪**：权益曲线降采样到 2,000 点、成交最多返回 300 条，否则长区间响应会到几 MB。

### 5.5 风控与资金口径

19. **资金口径必须单一来源**：engine 用快照 `avail_usdt` 算下单量，风控却拿配置
    `initial_cash` 当权益基数。真实账户（权益约 9.8 万）下非 paper 模式的
    单品种上限只有 3,000、日亏损上限只有 500，与真实下单规模完全脱节。
    现统一读同一份 monitor 快照的 `total_equity`，快照缺失才退回配置值并打警告。
20. **别把「被截断」当成「算错了」**：日志里 `qty adjusted by ...` 会指明是哪条规则。
    `max_order_qty`（配置 10000）是独立的单笔名义上限，且恰好与 `initial_cash`
    对齐——真实账户下它就是实际单笔上限，属预期行为。调大它会让模拟盘成交名义
    超过回测规模，反而使日报偏离判 FAIL。
21. **一个信号只取一次权益快照**：在 `_process` 里取一次并传给 `check()`，
    不放在 `RiskChecker` 内部每次读 Redis，既避免 N 次往返，也让同一信号
    在四条规则间用同一个基数，不会出现「日亏损用旧值、仓位用新值」。
22. **读取共用、兜底各自**：`read_account_field()` 把所有异常收敛成 `None`，
    但不替调用方决定默认值——engine 用 `cash_fallback`，worker 用 `initial_cash`。

### 5.6 事件驱动与补评估

23. **「不补发历史」和「不丢信号」是两件事**：早期实现只评估最近一根已收盘 bar，
    进程休眠期间的交叉永久丢失（实测已丢 11 根）。改成逐根补评估后，两者都要兼顾：
    有水位才补，首次启动（无水位）只评估最新一根，否则会把整个回放窗口的信号全发一遍。
24. **补评估必须同步虚拟持仓**：策略靠 `ctx.position.is_long` 决定能否卖出。
    只收集信号不更新持仓的话，金叉买入后会一直被认为空仓，随后的死叉不会平仓
    ——补发序列变成「只买不卖」。
25. **补发要有上限**：停机数天时早期的交叉已无意义，用 `max_catchup_bars`
    截断并告警，比无限制补发安全。
26. **看板只读，状态由 engine 主动发布**：engine 的补评估/跳过计数只存在进程内，
    看板拿不到。做法是 engine 每轮把结果写成状态 JSON（`status_key`），
    看板只读该键 + 水位键 + 本地 parquet 现算延迟，不反向访问 engine 对象，
    也不要求 engine 与 dashboard 同进程。状态超过 `status_timeout_seconds`
    未更新即判离线。

---

## 6. 已知限制与风险

| # | 限制 | 影响 | 建议 |
|---|---|---|---|
| 1 | 本地数据约 2 年（2024-10-26 起），且只有 BTC/ETH 两个品种 | 单品种参数仍可能过拟合，跨品种泛化未验证 | 需要更长样本时继续向前回填，注意同步调大 `retention_days` |
| 2 | 日报回测从本地最早 bar 连续成交（已修） | 无 | 若本地数据晚于模拟盘开户，仍可能残留偏差；可在首次运行时落盘基线快照 |
| 3 | 样本外只有一段（2025-10~2026-10 熊市） | 4H 转正只经过一段熊市检验，样本仍偏少 | 再切一段做滚动验证（walk-forward） |
| 4 | SMTP 未配置 | 真实告警发不出去，只记日志 | 填入 `[smtp]` 后执行 `quant test-email` 实测 |
| 5 | monitor 自身死亡无外部告警 | 心跳键带 TTL 但由谁来告警仍未定义 | 上云后用 systemd/外部探活；本机可先靠存活邮件反推 |
| 6 | 单进程串行寻优 | 大网格慢 | 需要时再加并行 |
| 7 | 无鉴权、只监听本机 | 目前安全，但上云前必须加 | M6 前处理 |
| 8 | 仓库尚未 `git init` | 无版本历史，回滚困难 | 尽快初始化并提交 |
| 9 | 实盘未验证 | 只跑过模拟盘 | M5 小资金试错 |
| 10 | 账户可用 USDT（约 2.3 万）与回测 `initial_cash`（1 万）不同量级 | engine 全仓 sizing 会比回测大约 2.3 倍，目前靠 `max_order_qty=10000` 兜住 | 已决定维持现状：单笔上限即与回测对齐；若日后要放开，需同步调整回测 `initial_cash` |
| 11 | engine 补评估已实现，但超限会丢弃早期 bar | 停机超过 `max_catchup_bars`（默认 24 根 = 1H 一天）时，更早的交叉仍会丢 | 按标的周期调整该值；长期停机应人工复核是否需要手工补单 |

---

## 7. 下一步建议（按优先级）

1. **降频并加趋势过滤（当前最紧要）**：样本外证明 1H 亏损主要是换手成本（66 轮≈20pp），
   4H 已能转正。下一步把周期从 1H 提到 4H（`fast=10/slow=100`），再加一层
   长周期趋势过滤（如价格在 200 日均线上方才允许做多），避免熊市里反复接刀
2. **让模拟盘真正产生成交**：`fills` 表为 0 笔（engine 启动晚于最后一次金叉），
   需等下一次交叉才能验证风控与日报
3. **配置 SMTP 并实测**：填入 `[smtp]` 后跑 `quant test-email`，
   让告警、恢复通知、日报与存活邮件真正到达邮箱
4. **版本管理**：`git init` + 首次提交，后续按里程碑打 tag
5. **补更多品种**：ETH 已有三周期，可按需增加其它主流品种做组合验证
6. **M5 准备**：TradingView Pro 开通、实盘凭证与风控阈值复核、小资金灰度

---

## 8. 变更记录（2026-10-26）

### 8.1 日报回测口径修正

- 问题：回测只回看 `window_size` 根（200 根）就开始成交，模拟盘在窗口起点可能
  持有更早建立的仓位，回测却「空仓起步」，首笔平仓必然对不上。
- 证据：同一份数据、同一窗口做三组对照 —— 回看 50 根偏离 69.25%、200 根 52.11%、
  全量 398 根 0.00%。
- 修复：改为从本地最早一根 bar 连续成交，并把 `window_size` 显式传给 `Backtest`。
- 测试：新增 2 条（窗口中途起点必须复现、自定义 `window_size` 不破坏结果）。
- 文档：RUNBOOK 补口径说明，避免把「日报与 CLI 数字不同」当 Bug。

### 8.2 风控权益基数统一

- 问题：engine 用快照 `avail_usdt` sizing，`RiskChecker` 用配置 `initial_cash`
  当权益基数；真实账户下非 paper 模式的单品种上限只有 3,000。
- 修复：
  - 新增 `execution/account.py::read_account_field()`，engine 与 worker 共用同一份
    快照读取与容错（异常一律返回 `None`，不抛）。
  - `RiskChecker.check()` 新增 `equity` 入参，未传或非法时退回构造值。
  - `Worker` 新增 `equity_provider`，每个信号取一次 `total_equity` 后传给 `check()`。
  - `run_worker` 完成接线并在快照不可用时打警告。
- 测试：新增 10 条（权益覆盖、非法权益兜底、快照解析四种异常、worker 取权益两条）。

### 8.3 一次误判的记录

第一版修复时把 43% 的截断归因于「权益基数太小」，改完实测仍是 43%——真正原因是
`max_order_qty`。教训：**先用日志/对照实验定位是哪条规则生效，再动代码**。
日志里 `qty adjusted by max_order_qty` 与 `qty adjusted by max_position_pct`
是两个不同规则，不能混看。

### 8.4 engine 补评估漏掉的 bar

- 问题：`run_once` 只评估最近一根已收盘 bar，休眠/停机期间的交叉永久丢失
  （实测累计跳过 11 根）。
- 曾走过的弯路：第一版方案是「逐根回放但只取最新一根的信号」，这解决不了问题
  ——信号只在交叉那一根产生，只取最新一根仍然会丢。
- 最终实现：
  - 有水位时逐根补评估，收集该区间内的**全部**信号并按顺序入队
  - 每收集到一个信号就用本根收盘价更新虚拟持仓与现金，否则死叉会因
    「仍被认为空仓」而不平仓
  - 首次启动（无水位）只评估最新一根，避免补发整个回放窗口的历史信号
  - `max_catchup_bars`（默认 24）限制单轮补评估根数，超限丢早期并告警
  - `EngineTick` 新增 `signal_count`，日志对补发打 `catch_up=N`
- 测试：新增 3 条（补发金叉+死叉、首次运行不补发、上限截断）。

### 8.5 看板 engine 健康度卡片

- 问题：补评估、跳过、信号计数只在日志里，运维只能 `grep`；engine 是否在跑、
  水位落后本地行情几根，也没有页面可看。
- 实现：
  - `EngineSettings` 新增 `status_key`（`quant:engine:status:{strategy}:{inst}`）
    与 `status_timeout_seconds`（默认 180）
  - engine 每轮 `publish_status()` 写一份 JSON：本轮状态、评估 bar、补评估根数、
    进程内累计 tick/信号/跳过/补评估；`--once` 也会发布
  - `DashboardService.engine()` 只读状态键、水位键与本地 parquet，
    计算最近评估 bar、水位落后根数、在线状态与累计计数
  - 新增 `GET /api/engine`，前端新增「策略引擎」卡片（5 秒轮询）
- 测试：新增 6 条（engine 状态发布与计数、看板空态/水位延迟/离线判定/
  Redis 降级、HTTP 端点）。
- 注意：计数是进程内累计，engine 重启后归零；跨重启的长期趋势仍需落库。

### 8.6 回填两年历史行情

- 动机：原 1H 只有 54 天、ETH 只有 1H，回测/寻优样本不足以做参数选择。
- 做法：按 `(inst, bar)` 六组回填 2024-10-26 → 现在（BTC/ETH × 1H/4H/1D），
  每组先清断点、拉完再把断点写回最新已收盘 bar。
- 结果：1H 17,534 根、4H 4,383 根、1D 729 根，六组校验均无缺口。
- 配套：`[storage] retention_days` 365 → 1095，否则次日的自动清理会删掉一年历史。
- 实测：两年样本上 `sma_cross(10/50)` 亏损 41.94%、夏普 -0.77、回撤 53.38%，
  说明此前短窗口的正夏普是过拟合；寻优 4 组全负，最好夏普 -0.73。
- 未改代码：回填用一次性脚本完成，仓库内仅调整配置与文档。

### 8.7 样本内 / 样本外参数验证

- 初衷：两年数据在手后，先确认「之前的正夏普是不是过拟合」再决定下一步。
- 做法：按牛熊切分（IS 2024-11-10~2025-09-30 / OOS 2025-10-01~2026-10-26），
  在 IS 上网格寻优取 Top，再到 OOS 上跑同一组参数；同时用同区间买入持有做基准。
- 结果：IS 正夏普全部在 OOS 转负（1H），但 1H 亏损小于买入持有；
  4H 的 `10/100` 在 BTC/ETH 两个品种 OOS 均为正（+1.33% / -3.42%，夏普 0.18/0.11）。
- 关键发现：每轮往返成本约 0.3%（手续费 0.001×2 + 滑点 5bp×2），
  1H 的 60~109 轮意味着 18~33pp 成本，**换手率才是亏损主因**。
- 产出：周期从 1H 提到 4H 作为下一阶段主线；不再靠调均线长度找 alpha。
- 未改运行配置：engine 仍是 1H 10/50，切换需单独确认并重启验证。

### 8.8 告警链路闭环

- 问题：系统已无人值守自动交易，但最关键的信号源 engine 不在告警规则里；
  故障恢复后邮件/看板停在上一条 ERROR，看着像一直没好；SMTP 配了也没法验证。
- 实现：
  - `MonitorSettings` 新增 `engine_status_key`（空=不检查）与 `engine_timeout_seconds`
    （默认 180）；`cli/monitor.py` 按 `status_key:{strategy}:{inst}` 拼好传入，
    engine 未启用时自动关闭该规则
  - monitor 新增第 10 条规则 `engine_heartbeat`：键缺失、无法解析、状态陈旧分别给出明确文案
  - 持续型规则（心跳/队列/保证金/撤单率/错误率/kill switch）记录上一轮命中集合，
    本轮消失时补一条 `INFO 已恢复（此前：…）`；事件型规则（死信新增/余额突变/连亏）不参与
  - 恢复通知不受看板冷却窗口限制；前端 `INFO` 用绿色左边框区分
  - 存活邮件改为 `async` 并把上次发送时间存 Redis `quant:monitor:alive-last`，
    monitor 重启不会立刻补发
  - 新增 `quant test-email [--to a@b,c@d]`：一条命令验证 SMTP 能否真正到达邮箱；
    `config.toml` 补 `[smtp]` 段（host/username 走文件，password 建议走环境变量）
- 测试：新增 9 条（engine 四种状态、恢复只发一次、恢复绕过冷却、存活邮件跨重启、
  test-email 两种退出码）。
- 实测：重启 monitor 后日志显示 `engine_heartbeat=on`，engine 正常时无误报；
  `quant test-email` 在未配置时明确提示缺哪几项。

### 8.9 前端告警中心与策略参数

- 问题：实测 34 条告警全是同一条 `worker_heartbeat`，列表被单规则刷屏，
  其它规则永远没有可见机会；engine 卡也看不到当前跑的均线参数。
- 实现（纯前端，后端零改动）：
  - 告警拉取上限 30 → 200（后端上限即 200）
  - 按规则聚合：一行显示级别徽标、规则名、命中次数、最新消息、最新时间，
    点击展开该规则的全部明细；只筛到单条规则时自动展开
  - 级别筛选（全部/ERROR/WARN/INFO）带计数；规则下拉显示每条规则的命中数
  - 顶栏新增 `ALERTS` 芯片：有 ERROR 显示红色计数，否则「无 ERROR」
  - 策略引擎卡新增「策略参数」，来自 `/api/strategies`，与配置文件一致
- 验证：`npm run build` 通过；用 agent-browser 实测聚合行、展开明细、
  WARN 空态与顶栏 `34 ERROR` 均符合预期。

### 8.10 前端权益曲线时间范围

- 问题：权益曲线写死近 24h，只有形状没有数字；后端 `hours` 早已支持到 720，
  且 `equity_snapshots` 已在持续累积。
- 实现（纯前端）：
  - 曲线从 `fetchAll` 拆出，改为独立 5 秒轮询 `fetchEquity(hours)`，
    切换区间立即重拉并取消旧请求
  - 卡片右上角 `24h / 7d / 30d` 分段按钮（复用 K 线的 `.seg` 样式）
  - 曲线下方四个数字：区间最低、区间收益、**区间最大回撤**、区间最高；
    回撤按区间内滚动峰值计算，超过 10% 标红
  - `degraded` 集合并入权益接口，SQLite 读失败时顶栏 `SOURCES` 显示降级
- 注意：当前 `equity_snapshots` 只有约 32 小时数据，7d/30d 会与 24h 相同，
  待 monitor 继续累积后才有区别。
- 顺带修正：顶栏从 6 个芯片变 7 个，窄屏下最后一个芯片铺满整行避免留空位。
- 验证：`npm run build` 通过；浏览器实测三个区间分别请求
  `hours=24/168/720`，切片标签、收益与回撤均正确刷新。

### 8.11 成交盈亏汇总

- 问题：面板只列最近成交，看不出赚没赚——胜率、盈亏比、已实现盈亏都得自己算。
- 实现：
  - 新增只读接口 `GET /api/pnl`：读取全部成交，用风控同款
    `quant.risk.pnl.round_trip_pnls` 做 FIFO 回合配对（含手续费），
    保证看板口径与风控「连续亏损」、回测胜率完全一致
  - 返回 `fill_count / round_trips / wins / losses / win_rate /
    profit_factor / total_pnl / avg_win / avg_loss / best / worst /
    fee / turnover / first_ts / last_ts`
  - 未平仓部分不计入回合，因此 `total_pnl` 是已实现盈亏而非浮动盈亏；
    无亏损回合时 `profit_factor` 返回 `None`（而非无穷大）
  - 前端新增「成交盈亏」整行卡片：6 个指标 + 页脚（成交笔数、最好/最差、
    统计区间、未平仓说明）；无成交时显示空态提示
- 验证：`pytest` 875 passed（新增 5 条 TestPnl + HTTP 端点断言）；
  `npm run build` 通过；用 4 笔临时成交实测得到
  `-24.24 / 50.00% / 0.82 / 107.88 / -132.12`，核对 FIFO 与手续费无误后已删除。

### 8.12 回测结果历史对比

- 问题：回测面板「只计算不落盘」，刷新即丢，无法横向比较不同参数的表现。
- 实现（纯前端，`localStorage` 键 `quant.backtest.history.v1`）：
  - 每次单次回测成功自动存一条**指标摘要**（策略/品种/周期/区间/参数/
    六个指标/期末权益），不存权益曲线与成交明细，避免撑爆配额
  - 同配置重跑按「策略|品种|周期|区间|参数」签名替换旧记录，不会堆重复项；
    最多保留 20 条，写入失败（隐私模式/配额）时静默降级为只存在内存
  - 历史列表：勾选框、配置、区间与根数、参数、收益/回撤/夏普、保存时间、
    单条删除；右上角「清空」
  - 勾选 2–4 条出现横向对比表：区间/配置/参数 + 六个指标 + 期末权益，
    每项高亮最优值（回撤取最小，其余取最大；交易次数无优劣不高亮）
- 验证：`npm run build` 通过；浏览器实测三次不同参数回测 → 3 列对比表
  高亮正确；刷新页面历史仍在；同参数重跑不新增条目并前移；
  删除后对比表降为 2 列；清空后回到空态。

### 8.13 模拟盘日报查看

- 问题：日报只发邮件 + 落 CSV，面板上看不到，想回看历史偏离得自己翻目录。
- 实现：
  - 新增只读接口 `GET /api/reports?limit=`：扫描 `report.out_dir`
    （默认 `reports/daily`），按 mtime 倒序解析 CSV，返回
    `date/strategy/inst/status/max_deviation/rows`
  - 文件名解析兼容带连字符的 inst（`{date}-{strategy}-{inst}.csv`，
    date 固定 10 字符后切开）
  - PASS/FAIL 由 CSV 内容重算：`side_sequence` 必须 match 且四项
    偏离最大值 ≤ `report.threshold`，与生成时的判定口径一致
  - 前端新增整行「模拟盘日报」卡：列表 + 点击展开五项比对表，
    偏离超阈值标红、方向序列不一致标红；60 秒低频刷新（每天才一份）
  - 目录不存在或无 CSV 时返回空列表并显示空态说明，不报错
- 验证：`pytest` 879 passed（新增 4 条 TestReports + HTTP 端点断言、
  覆盖目录缺失/状态判定/方向不一致/倒序与 limit）；`npm run build` 通过；
  用两份临时 CSV 实测 FAIL/PASS 列表与展开表格，核对后已删除。

### 8.14 紧急停止（kill switch）网页按钮

- 问题：紧急停止只能通过 CLI（`quant kill-switch --on`）操作，盘中想立刻
  停手还得开终端；看板只有只读状态，没有可点的入口。
- 实现：
  - 新增写接口 `POST /api/kill-switch`（`{"action": "on" | "off", "reason"}`），
    只写 Redis 标志 `quant:kill-switch`，不撤挂单、不停进程
  - 三层写操作守卫：`Host` 必须是本机环回（挡 DNS 重绑定）→ 必须带
    `X-Dashboard-Request: 1` 自定义头（浏览器跨站请求会先预检，而本应用
    不返回 CORS 头，挡 CSRF）→ 配置了 `dashboard.token` 时校验
    `X-Dashboard-Token`（`hmac.compare_digest` 常量时间比较）
  - `dashboard.token` 默认空（只允许本机），可用环境变量 `DASHBOARD_TOKEN`
    覆盖；`/api/overview` 返回 `kill_switch.auth_required` 让前端决定是否
    显示 token 输入框
  - 前端「紧急停止」卡：两段式确认（先点「停止开新仓」再填原因确认，
    避免误点）、可填原因写进告警便于复盘、`auth_required` 时显示 token
    输入并存入 `sessionStorage`；服务端状态一变即丢弃本地结果，避免遮住
    风控的自动激活
  - 卡片明确标注「只拦新信号 · 不撤挂单 · 不停进程」；需要同时撤单仍走
    `quant kill-switch --on`
- 验证：`pytest` 883 passed（新增 4 条 `TestKillSwitch`：服务层切换、
  缺自定义头/非本机 Host 拒绝、HTTP 切换、配置 token 后强制校验）；
  `npm run build` 通过；浏览器实测 停止 → 卡片「已激活」+ 原因回显 +
  顶栏 `KILL SWITCH` 芯片变红（`chip-bad`）→ Redis 键存在，monitor 当轮
  产生「紧急停止已激活」ERROR 告警 → 解除 → 芯片恢复 `chip-ok`、Redis 键删除；
  测试告警已清理。
- 补充：`kill_switch` 告警不再参与看板冷却窗口（`_NO_COOLDOWN_RULES`）。
  原先 10 分钟冷却按规则去重，导致 10 分钟内二次激活（先 CLI 停、再网页停）
  在告警列表里看不到；现在每次激活都会留痕。
- 补充：跳过冷却后改为「持续命中只报一次」（`_DEDUPE_RULES`）。一次手动
  停止会持续几十轮，靠 `_with_recovery` 比对上一轮的 `_active_rules`，
  同一规则同一消息不再重复发布；原因变化（`reason=` 变了）视为新的激活，
  解除后再次激活也会重新记一条。这样既不会漏掉「谁在什么时候停的」，
  也不会把 200 条的列表占满、把其它规则的告警挤出去。
- 修复：首版漏写了 `.kill*` 的全部样式（只在响应式里留了一行引用），
  卡片没有 `grid-column`，被压成 1 列 87px 宽、743px 高，把后面所有卡片
  顶到下一屏，整页看起来「只有左半边有东西」。现补全样式并改为整行
  横向布局（状态在左、按钮在右、提示占底行），窄屏降为单列；
  `.engine-reason` 加省略号，避免英文原因把引擎状态撑成两行。

### 8.15 心跳误报治理（宿主机休眠）

- 问题：告警列表里 `worker_heartbeat` / `engine_heartbeat` 累计 122 条
  （ERROR 104、INFO 18），集中在 10-25～10-27，且 `engine 状态陈旧`
  多为 1300s 左右（约 22 分钟），与 `monitor.log` 自身的 20～23 分钟
  空档完全吻合——是整机被挂起（macOS 休眠 / App Nap），不是进程真的挂了。
- 根因：心跳判据只看「别人多久没写」，没考虑「我自己也刚睡醒」。
  唤醒瞬间 monitor 读到的 worker/engine 时间戳必然是挂起前的，于是必报
  一条 ERROR；下一轮恢复后又补一条 INFO「已恢复」，成对刷屏。
- 实现（`monitor/monitor.py`）：
  - `_take_suspend_gap(ts)`：用本轮与上一轮的墙钟偏差判断挂起，
    超过 `interval_seconds × 3`（默认 180s）即认为整机被冻结，
    返回扣除正常周期后的挂起时长
  - `run_once` 命中挂起时，`_evaluate(suspended=True)` 跳过
    `_HEARTBEAT_RULES`（worker/engine 心跳），并把这俩规则从
    `_active_rules` 里静默移除——误报没有「恢复」可发，避免假 INFO
  - 其它规则（队列堆积、死信、保证金、撤单率、kill switch）照常评估：
    它们反映的是真实状态，与挂起无关
- 验证：`pytest` 886 passed（新增 2 条：`_take_suspend_gap` 的边界、
  挂起轮次不报心跳也不补发「已恢复」）；重启 monitor 加载新代码；
  历史 122 条心跳告警已从看板列表清理（`logs/monitor.log` 保留完整原文）
- 未改动：系统电源策略（`pmset` 显示电池模式 `sleep 1`、`displaysleep 2`）。
  这是宿主机层面的设置，属于用户环境；如需彻底避免挂起可执行
  `sudo pmset -b sleep 0` 或运行期间用 `caffeinate -i`。

### 8.16 风控限额仪表（前端）

- 问题：看板只有「风险指标」（回撤 / 撤单率 / 强平距离），看不到风控五条
  规则当前用了多少额度，盘中无法判断离日亏损、仓位上限还有多远。
- 顺带修复一个真 bug：`worker._process` 调 `risk.check()` 时没传
  `today_start_ms`，默认 `0` 在 checker 里表示「不按时间过滤」——PRD 的
  「日亏损上限」实际跑成了「累计亏损上限」，历史累计亏到 -5% 会永久 halt
  并自动开 kill switch，永不重置。
- 时区口径：新增 `foundation.schedule.day_start_ms_utc8()`，按 **UTC+8 零点**
  重置，先把时间戳平移到 UTC+8、按天取整再平移回来，不受宿主机时区影响；
  worker 每次风控检查都传当日零点。
- 数据流：worker 每个心跳周期（60s）把**生效值**写入 Redis
  `quant:risk:limits:{strategy}`（TTL 300s）→ `GET /api/risk-limits` →
  前端 5s 轮询。由 worker 发布而不是看板直读 toml 的原因：`--paper-mode`
  会把 `max_position_pct` 0.3→1.0、`max_total_pct` 0.8→1.0，覆盖只存在于
  CLI 参数里，直读配置文件会显示未生效的旧值。
- 响应：`known / strategy / paper_mode / equity / equity_source /
  day_start_ts / rules[5]`；五条规则 key 为 `daily_loss`、
  `consecutive_loss`、`order_notional`、`inst_position`、`total_position`；
  `level` 按 `ratio < 0.7` 为 ok、`< 1` 为 warn、`>= 1` 为 halt。
- 已用额度口径与风控一致：日亏损取 UTC+8 当日 FIFO 已实现盈亏（同
  `round_trip_pnls`）；连亏取最近回合连续亏损笔数；仓位用本地持仓数量 ×
  monitor 快照 `mark_price`（本地 `BTC-USDT` 前缀匹配账户 `BTC-USDT-SWAP`），
  没有快照时回退本地均价。
- 权益优先取 monitor 快照 `total_equity`，读不到退回
  `[backtest].initial_cash` 并标 `equity_source=initial_cash`，与 worker 的
  风控基数保持同一来源。
- 前端：新增「风控限额」整行卡片，插在「风险指标」与「紧急停止」之间；
  日亏损 / 单品种 / 总仓位画进度条（带 70%、100% 刻度），连亏用点阵，
  单笔上限只列参考值；`known=false` 时整卡替换为一行提示（worker 未启动，
  或停机超过 5 分钟键已过期）。
- 修复：首版把 `/api/risk-limits` 放进了 `fetchAll` 的 `Promise.all`，
  后端进程未重启（该路由还不存在）时一个 404 会让整页显示「连接中断」。
  现改为像权益/日报那样单独轮询，失败只把这张卡降级为
  「接口不可用（… HTTP 404）」提示，其余面板照常刷新。
- 验证：`pytest` 894 passed（新增 7 条：worker 发布生效值、昨日亏损不拦截、
  规则 ratio/level 计算、`known=false`、paper-mode 覆盖值、日亏损触线 halt、
  权益回退初始资金；另修正两条用 1970 时间戳构造「今日亏损」的既有测试）；
  `ruff` 全通过；`npm run build` 通过；实库只读调用确认 `day_start_ts`
  为北京时间零点。

---

### 8.17 持仓操作面板（手动减仓 / 一键全平）

- 背景：看板此前完全只读，风险来了只能靠 CLI 或交易所 App 手动平仓。
  新增唯一的真实下单入口，但**只减不开**：没有任何开仓/反手路径。
- 执行路径：看板进程直连 OKX（`dashboard/manual.py`），复用
  `OkxBroker` 下单与 `Position.apply_fill` 落库；`service.py` 保持只读。
- 数量安全：下单前实时查交易所可用量，实际下单量取
  `min(请求量, 可用量)`；再按 `lotSz` 向下取整，低于 `minSz` 直接拒绝。
  现货口径是基础币，合约口径是「张」，用 `ctVal` 与基础币互转，
  回执同时给 `submitted_qty` 与 `contracts`。
- 合约专用：`OkxBroker.execute` 新增 `reduce_only` 参数，合约单带
  `reduceOnly=true`，避免方向算错时开出反向新仓。合约部分只有单测覆盖，
  当前账户无合约持仓，未做端到端实测。
- 并发保护：同品种 Redis 短锁 `quant:manual:lock:{inst}`（TTL 5s），
  防连点重复下单；Redis 不可用时降级为不加锁，宁可少一层保护也不阻断操作。
- 接口：`POST /api/positions/reduce`（`ratio` 与 `qty` 二选一）、
  `POST /api/positions/flatten`（必须带 `confirm=FLATTEN`）。两者都复用
  三层守卫（本机 Host → `X-Dashboard-Request` → 可选 token）；未配置 OKX
  凭据返回 503，交易所拒单返回 502。
- 全平范围：本地持仓 ∪ monitor 快照多头，逐个品种减到 0，单项失败不影响
  其它并逐条返回结果；本地持仓为 0 但交易所有仓位时以交易所为准。
- 前端：持仓卡片点选一行，表格下方展开操作条（25%/50%/平仓按钮 + 数量输入
  框联动 + 原因必填 + 预估金额与手续费）；卡片头「一键全平」走红色确认条，
  必须手动输入 `FLATTEN` 才可提交。提交期间按钮禁用。
- 新增配置项 `[execution].td_mode`（默认 `cash`），供看板手动下单使用。
- 验证：`pytest` 910 passed（新增 16 条：夹紧、`lotSz` 取整、`minSz`
  拦截、比例需本地持仓、全平清零与已实现盈亏、原因必填、参数互斥、
  合约张数换算与 `reduceOnly`、全平部分失败聚合、快照补全、路由守卫、
  确认词校验、无凭据 503）；`ruff` 全通过；`npm run build` 通过；
  用独立 SQLite 副本起 8091 端口截图核对行高亮、操作条与确认条布局
  （未提交真实订单）。
- 端到端实测（mock 持仓 + 真实 OKX 模拟盘）：临时实例用 `/tmp` 独立
  SQLite 写入 `BTC-USDT 0.01 @ 61234.5`，浏览器点选行 → 25% → 填原因 →
  减仓，链路真实打到 `demo=true` 的 OKX。结果：交易所侧
  `ordId=4047072345865637888 state=filled sz=0.0025 avgPx=80576.2
  fee=-0.2014405 USDT`，BTC 余额 0.77033874 → 0.76783874（差额恰为
  0.0025）；本地同步为 `local_qty=0.0075`、`realized_pnl=48.35425`
  （`(80576.2-61234.5)×0.0025`），`orders`/`fills`/最近成交/累计手续费
  全部刷新正常。**生产库与 8080 实例未被写入**。
- 顺带修一个观感问题：提交成功后表单清空，操作条仍显示「≈ 0.00 USDT」，
  现改为未选数量时不渲染预估行。
- 实测暴露的既有缺口（属 PRD F4.6）：`orders` 表未存
  `inst_id/side/order_type/qty`，看板靠 JOIN `signals` 取上下文。
  手动路径漏写 `signals` 的问题已在 8.18 修复。

---

### 8.18 持仓与订单联动修复

- 现象：手动减仓成功后，「最近订单」的品种/方向/类型/数量四列是 `—`。
- 根因：`manual.py::_reduce_locked` 只写 `orders` 不写 `signals`，
  而 `orders` 表只有 `order_id/signal_id/...`，看板通过
  `LEFT JOIN signals` 取回订单上下文；悬空 `signal_id` 让 JOIN 全空。
  worker 正常路径是 `save_signal → save_order`，手动路径漏了第一步。
- 修复：手动下单在 `save_order` 前补 `save_signal(signal, "executed")`，
  与 worker 路径顺序一致。未改表结构：当前生产库无历史订单，无需回填。
- 顺带修三处同源不一致：
  - 一键全平：本地记录偏小时按本地数量下单会在交易所留下残余仓位，
    现改为全平始终以交易所可用量为准（本地为 0 或落后都不影响）。
  - 本地持仓被翻向：交易所成交量大于本地记录时，`apply_fill` 会把本地
    持仓写成反向空头；现只结平已知持仓，本地无记录时保持空仓。
  - 已平仓位仍占一行：`positions()` 过滤本地 `qty=0` 的记录，平仓后该行
    立即消失；若快照里仍有仓位则按远端行展示，不会漏报。
- 验证：`pytest` 915 passed（新增 5 条：手动订单带信号上下文、超卖不翻向、
  全平用交易所数量、无本地记录保持空仓、零仓行不展示）；`ruff` 全通过。

### 8.19 现货持仓对账

- 现象：看板「概览」持仓数为 0、「持仓」表却有 1 条；现货行的标记价 /
  未实现盈亏全是 `—`，交易所侧现货余额完全不可见。
- 根因：OKX `/account/positions` 不返回 SPOT，而 monitor 的
  `position_inst_types` 默认只有 `SWAP/FUTURES`。现货持仓只存在于
  `/account/balance` 的 `details` 里，monitor 从未采集，快照里自然没有。
- 修复（只动采集层，前端与合并逻辑保持原样）：
  - `Monitor._spot_positions()` 从余额明细逐币种折算 `{ccy}-USDT` 持仓：
    数量取 `cashBal`（缺失回退 `eq`），标记价取公开 ticker 的 `last`，
    没有对应交易对的余额（理财份额等）直接跳过。
  - 现货没有交易所侧成本基数，`avg_price` 改读本地 SQLite 记账；
    读不到时记 0 并由看板层转成「未知」，前端显示 `—` 而不是 `0.00`。
  - `PositionSnapshot.from_spot()` 统一构造现货快照：杠杆 1、无强平价，
    未实现盈亏只在有成本基数时计算。
  - 复用同一轮的 balance 响应，未新增 REST 调用；单个币种行情失败只跳过
    该币种，不影响整轮快照发布。
  - 新增配置 `monitor.spot_quote_ccy`（默认 `USDT`，置空即关闭现货对账）。
- 行为变化：`/api/positions` 现在会列出**交易所侧全部现货持仓**，不再只列
  策略本地记账的品种（模拟盘当前为 BTC / ETH / OKB 三个币种）。
- 配套收紧「一键全平」范围：待平品种 = 本地持仓 ∪ 快照**合约**多头。
  快照里的现货是账户全部余额，可能含 ETH/OKB 这类与策略无关的资产，
  若照单全平会连带卖出；现货只平本地记账过的品种（`_is_contract` 按
  连字符数量区分现货/合约）。
- 遗留口径差异（未改）：风控限额卡里的「持仓名义」由 worker 按本地持仓
  计算，仍只含策略仓位（BTC 0.001 ≈ 80.65 USDT），与持仓表的账户口径
  不同源。这是风控只看策略敞口的既定设计，而非缺陷。
- 验证：`pytest` 922 passed（新增 7 条：ticker + 本地成本、无成本不编造、
  无交易对跳过、置空关闭、行情失败不拖垮整轮、看板零成本转未知、
  快照现货不参与全平）；`ruff` 全通过。

### 8.20 现货未实现盈亏的口径修复

- 现象：BTC 行显示 `0.7663 @ 80,573.80`，未实现盈亏 `+70.20`。但
  80,573.80 只是策略买入的 0.001 BTC 的成本，账户其余 0.7653 BTC 是外部
  转入、没有买入记录。
- 根因：`_local_avg_price()` 无条件返回本地均价，`from_spot()` 又拿它乘
  **交易所全量余额**，两个口径混算，把未记账的币按策略成本价虚增了盈亏。
- 修复：
  - `_local_avg_price(inst_id, qty)` 增加覆盖校验 —— 只有本地数量
    ≥ 交易所余额时才返回均价，否则返回 0（未知）。这样
    `avg_price` 与 `unrealized_pnl` 一起留空，看板显示 `—`。
  - 本地数量 > 交易所余额（外部卖出过一部分）仍用本地均价，按平均成本法
    剩余头寸成本不变，属有效口径。
  - 「成本基数未知」日志按品种去重，只在首次命中时打一条，避免每轮刷屏。
  - 前端「均价」列不再在后端返回空时回退到本地均价：有交易所数据时只认
    合并结果，否则会出现「交易所数量 + 本地均价」的错配展示。
- 验证：`pytest` 924 passed（新增 2 条：余额超出本地记账 → 均价与盈亏留空、
  本地记账覆盖余额 → 正常算出 2,000 USDT）；`ruff` 全通过。
- 顺带修复测试隔离：`tests/test_dashboard.py` 的 `env` fixture 原先使用
  仓库真实 `reports/daily`，每日任务落盘后 `/api/reports` 用例会失败，
  现改为指向 `tmp_path`。

### 8.21 持仓表新增「策略持仓 / 策略盈亏」两列

- 背景：8.20 之后账户口径的未实现盈亏对现货只能留空（成本未知），而策略
  自己那部分持仓的盈亏是可知的，却没有任何字段展示。
- 修复：`_merge_position()` 新增 `strategy_unrealized_pnl`
  = `(标记价 - 本地均价) × 本地数量`。数量与成本都取策略记账，只借用快照
  的标记价，因此不受账户里外部转入的币影响。
- 前端持仓表从 8 列扩到 10 列，表头明确区分两个口径：
  `品种 | 账户数量 | 账户均价 | 策略持仓 | 标记价 | 账户盈亏 | 策略盈亏 | 杠杆 | 强平价 | 距离`。
  「策略持仓」显示 `本地数量 @ 本地均价`，本地无记录时显示 `—`。
- 实测：`BTC-USDT` 账户 0.7663 / 策略 0.001 @ 80,573.80，账户盈亏 `—`，
  策略盈亏 `+0.16`（随标记价浮动）；`OKB-USDT`、`ETH-USDT` 两列均为 `—`。
- 说明：减仓操作条的「可减」与比例按钮仍以**策略持仓**为基数（后端
  `ratio` 也按本地数量计算），所以比例减仓只动策略仓位；要减外部余额
  需用绝对数量输入，由交易所可用量兜底。
- 验证：`pytest` 926 passed（新增 2 条：策略盈亏用本地数量与成本计算、
  纯交易所持仓时留空）；`ruff` 全通过。

### 8.22 现货账户盈亏改用交易所持仓均价 `openAvgPx`

- 背景：8.20 为防「策略成本 × 全量余额」虚增盈亏，把覆盖不了全量的现货
  盈亏一律留空，结果 BTC 账户盈亏恒为 `—`，偏离 OKX App 的展示口径。
- 口径：OKX 余额明细里 `openAvgPx` 是**当前持仓均价**（`accAvgPx` 是含已
  卖出部分的累计均价，不能用于当前持仓），乘全量余额即为账户口径成本。
- 修复：`_spot_avg_price()` 优先取 `openAvgPx`，`> 0` 即采用；交易所没给
  （空投、理财转入）或字段非法时才回退本地记账，并保留 8.20 的数量覆盖
  校验。`openAvgPx` 存在时不再受本地记账数量限制。
- 实测：`BTC-USDT` 均价 `80,279.82`、标记价 `80,475.50`、账户盈亏
  `+149.96`（不再是 `—`，也不再是虚高的 `+70.20`）；`ETH-USDT` 同样走
  交易所均价，`-15.10`。
- 验证：`pytest` 928 passed（新增 2 条：`openAvgPx` 覆盖本地数量不足、
  `openAvgPx` 非法时回退本地）；`ruff` 全通过；前端已重新构建并在浏览器
  确认 10 列表格渲染正常。

### 8.23 现货手续费币种归一（策略持仓与交易所余额对账）

- 现象：OKX 现货买入手续费按**收到的币**收取。实测
  `ordId=4047113072658141184` 买 0.001 BTC，`fee=-0.000001 feeCcy=BTC`。
  但 `Fill.fee` 的契约是「计价币」，`detail_to_fill()` 丢弃了 `feeCcy`。
- 影响：本地持仓只加减 `accFillSz`，不扣基础币手续费，策略持仓永远比
  交易所余额多出累计手续费；账户没有外部余额兜底时，全平卖出会因
  `availBal` 不足被拒。同时 `round_trip_pnls()` 把 0.000001 BTC 当
  0.000001 USDT 相减，日报/风控的日内盈亏偏高。
- 修复：新增 `_normalize_spot_fee()`，现货且 `feeCcy == 基础币` 时
  - 买入：`Fill.qty = accFillSz - fee`，与余额实增一致；
  - 费用按成交价折成计价币记入 `Fill.fee`（卖单只折算费用，不改数量）；
  - 合约（`feeCcy` 为结算币、`sz` 为张数）不参与换算。
- 实测：真实订单回放 `buy → qty=0.000999, fee=0.0805738 USDT`，
  `sell → qty=0.0025, fee=0.20105525 USDT`（与原来一致）。
- 说明：本次改动只影响新成交。历史数据已用一次性脚本回补（逐笔回查交易所
  订单详情，按新口径重写 `fills` / `orders`，再由 `fills` 重建持仓）：
  `ordId=4047113072658141184` 由 `qty=0.001, fee=0.000001` 修正为
  `qty=0.000999, fee=0.0805738 USDT`，BTC-USDT 本地持仓
  `0.001 → 0.000999`，均价 `80,573.80` 不变，已实现盈亏仍为 0。
  回补后重跑脚本输出 `no drift found`，幂等。
- 验证：`pytest` 931 passed（新增 3 条：基础币买入费用扣数量并折 U、
  计价币卖出费用不变、合约不换算）；`ruff` 全通过；worker 与 dashboard
  已重启加载新逻辑。

### 8.24 持仓漂移检测（P1）

- 背景：monitor 只采集、不比对本地记账与交易所余额，看板仅把两个数并排
  展示。本地数量超过交易所余额时（手工提币、外部卖出、手续费漂移），策略
  盈亏仍按本地数量计算，卖出还会因可用量不足被拒。
- 实现：`Monitor._check_position_drift()` 新增 `position_drift` 规则。
  只比对现货（`inst_id` 一个连字符）——合约快照的 `qty` 是张数、本地记
  基础币，直接比较会差一个 `ctVal` 倍数。方向只查「本地 > 交易所」：
  本地少于交易所是外部转入/空投的常态，反向才是必须人工介入的记账漂移。
- 阈值：`MonitorSettings.position_drift_tolerance = 0.005`（超出比例
  0.5%），避免手续费级别的抖动刷屏；交易所已无该币时按 100% 超额告警。
- 该规则加入 `_STATEFUL_RULES`，漂移消除后会补一条 INFO「已恢复」。
- 实测：当前账户本地 `0.000999` < 交易所 `0.76633774`，重启 monitor 后
  无 `position_drift` 告警，符合预期。
- 验证：`pytest` 935 passed（新增 4 条：本地超交易所、交易所超本地、
  容差内、交易所完全没有）；`ruff` 全通过。
- 未做：自动卖出尚未按 `min(本地, 可用)` 裁剪——快照只能近似交易所可用量，
  且 TTL 过期时会静默退化，需与 P3 的合约单位统一一起设计。

### 8.25 看板展示口径统一（P2）

- 问题一：概览「持仓数」只数 monitor 快照，持仓表却是「本地 ∪ 快照」。
  本地有仓、快照还没跟上时两个数字对不上。
  修复：新增 `DashboardService._position_count()`，按同一口径求并集
  （本地 `qty != 0` 的品种 ∪ 快照品种），读不到 SQLite 时返回 None，
  前端退回已拉到的持仓表长度。
- 问题二：快照键缺失或过期（TTL 180s）时账户列全变 `—`，但 `degraded`
  为空，SOURCES 卡片仍显示「正常」。monitor 停机是最常见的故障，界面却
  看起来一切正常。
  修复：`overview()` 与 `positions()` 在快照缺失/不可解析时把 `snapshot`
  加入 `degraded`（Redis 读取失败时不重复追加，仍只报 `redis`）。
- 问题三：「账户数量」列在无快照时回退显示本地数量，「账户均价」列也回退
  本地成本，列名与数据口径不符。
  修复：`positionCells()` 的账户两列只认后端合并结果，本地无快照时显示
  `—`；策略口径继续由「策略持仓 / 策略盈亏」两列承载。减仓操作条的
  「可减」仍以本地数量为基数，不受影响。
- 实测：`/api/overview` 返回 `degraded=[]`、`positions_count=3`，与持仓表
  行数一致；浏览器确认 SOURCES 显示「正常 / redis + sqlite」，BTC 行
  `0.7663 | 80,279.82 | 0.0009990 @ 80,573.80`。
- 验证：`pytest` 937 passed（新增 2 条：持仓数与持仓表同口径、
  快照缺失时降级且账户列留空；另修正 3 条原先假定 `degraded == []` 的
  用例）；`ruff` 全通过；前端已重新构建，dashboard 已重启。

### 8.26 合约单位统一（P3）

- 问题：同一笔合约在不同层用不同单位。OKX 的 `sz`/`pos`/`accFillSz` 是
  **张数**，而引擎的仓位 sizing、风控、本地 `fills/positions`、看板都是
  **基础币**。三处各自换算：`manual.py` 乘除 ctVal，worker 直接把基础币
  当张数下单（差 ctVal 倍），monitor 把张数当基础币写进快照（看板数量
  差 ctVal 倍）。
- 口径约定：**全链路基础币**。合约的张数换算只发生在 broker 与 monitor
  的交易所边界。
- `OKXClient.get_instrument()`：按 instId 查产品配置并进程内缓存。OKX 在
  instType 不匹配时返回 51001（而非空 data），按类型逐个探测并把该错误
  当作「这个类型下没有」。网络/鉴权错误照常抛出，不会把瞬时故障缓存成
  None。顺带修掉 `manual._instrument()` 的同类缺陷——它原先自己探测类型，
  合约查配置会直接抛 51001。
- `OkxBroker.ct_val()` / `execute()` / `amend_order()`：收到的都是基础币，
  提交前按 `qty / ctVal` 折成张数并按 `lotSz` 向下取整；回报用
  `detail_to_fill(..., ct_val=)` 折回基础币。查不到面值时**拒单**（
  `OrderError`），不做「按 1 处理」的降级。合约市价单不再带 `tgtCcy`
  （该字段只对现货有效）。
- `Reconciler`：对账时取同一 ctVal，取不到则跳过该订单并告警，避免把张数
  当基础币写进本地持仓。
- `Monitor._contract_position()`：`pos × ctVal` 后才入快照；查不到面值时
  跳过该行并告警。同时把漂移检测从「只比现货」放开到全品种——单位统一后
  才有可比性。
- `ManualTrader._reduce_locked()`：改为把基础币数量传给 broker，去掉回执
  里二次乘 ctVal 的换算（signals 表里记的也不再是张数）。
- 实测：真实交易所 `BTC-USDT-SWAP` 的 `ctVal=0.01 / lotSz=0.01 /
  minSz=0.01`；`/api/positions` 三个现货品种数量与均价不变，
  `degraded=[]`、`positions_count=3`。
- 验证：`pytest` 948 passed（新增 11 条：快照张数换算/缺面值跳过/现货不
  换算、broker 张数提交与回报折算/按 lotSz 取整/低于 1 张拒单/缺面值拒单/
  现货不查配置、`get_instrument` 命中与 miss 缓存、漂移检测覆盖合约）；
  `ruff` 全通过；monitor 与 worker 已重启加载新逻辑。
- 提醒：切 SWAP 前还要确认三件事——策略 K 线用 `BTC-USDT-SWAP` 的历史
  数据、风控口径（见 8.27 的更正）、`td_mode` 从 `cash` 改为
  `cross/isolated`。

### 8.27 切到 `BTC-USDT-SWAP`

- K 线：`BTC-USDT-SWAP` 1H 回补 2024-01-01 → 2026-10-28，共 24,745 根，
  落在 `data/candles/swap/BTC-USDT-SWAP/1H/{2024,2025,2026}.parquet`；
  `validate` 无 error/duplicate/missing。
- 配置：`[candles].insts = ["BTC-USDT-SWAP", "BTC-USDT"]`（现货保留同步，
  供对比与手动交易）；`[engine]`、`[report]` 的 `inst` 改为
  `BTC-USDT-SWAP`；`[execution].td_mode = "cross"`。
- `OkxBroker.execute()`：`tdMode` 按品种取值——合约用配置值，现货固定
  `cash`（现货传 `cross/isolated` 会被拒单）。新增 1 条用例覆盖。
- 更正 8.26 的「`max_order_qty` 按基础币设」：风控里是
  `qty * price > max_order_qty`，即 **USDT 名义额**，现货/合约同口径，
  切 SWAP 无需调整（当前 10000）。
- 运维事实：supervisor 是手工启动的，`kill` 后不会自动拉起。切换后的启动
  命令为
  `PYTHONPATH=src nohup python3 -m quant supervisor --strategy sma_cross --broker okx --td-mode cross --paper-mode --ws >> logs/supervisor.out.log 2>&1 &`
  ——`--td-mode` 只在 supervisor CLI 上，`config.toml` 的
  `[execution].td_mode` 只管看板手动交易。
- 验证：`pytest` 949 passed、`ruff` 全通过；6 个子进程全部在线，worker
  命令行含 `--td-mode cross`；`/api/overview` 的 `worker/monitor online`、
  `degraded=[]`、`positions_count=3`；引擎新水位键
  `quant:engine:status:sma_cross:BTC-USDT-SWAP` 已建立并处理完 1H bar，
  `last-bar` 同步推进；candles 首轮 `new=14` 补完缺口。
- 已知遗留：worker 的 OKX WS 仍在每轮重连（`Connection reset by peer`），
  与本次切换无关，沿用既有排查项。

### 8.28 P0 收尾：git 初始化 + 合约端到端验证

- 版本管理：仓库此前从未 `git init`。现已初始化并提交两个基线提交
  （源码/测试/文档、前端源码），`.env`、`config/config.toml`、
  `config/strategies/*.toml`、`data/`、`logs/` 仍由 `.gitignore` 排除。
- 合约端到端（OKX 模拟盘，`BTC-USDT-SWAP`）：
  - 买入：`1 张`、`avgPx=80722.9483`、`fee=0.4036147415 USDT`、
    `tdMode=cross`、`state=filled`；本地记为 `0.01 BTC`。
  - 平仓：`1 张`、`avgPx=80699.7129`、`fee=0.4034985645 USDT`；
    交易所 `SWAP positions` 清空，本地持仓归零，回合价差 `-0.232354`。
  - 结论：信号 → webhook → Redis → worker → broker → OKX → SQLite →
    看板全链路在合约口径下打通，`ctVal`/`lotSz` 换算与 `feeCcy=USDT` 一致。
- 验证中发现并修复：`round_trip_pnls()` 未按 `inst_id` 分组，现货
  `BTC-USDT` 的买入会被拿去和 `BTC-USDT-SWAP` 的卖单 FIFO 配对，
  导致看板回合盈亏从 `-1.0395` 错算成 `-0.9443`。现按品种分组配对、
  再按平仓时间合并，兼顾“最近连续亏损”的时间顺序。
- 验证：`pytest` 950 passed（新增 1 条跨品种不互配 + 平仓时间排序用例）；
  重启 worker/monitor/dashboard 后 `/api/pnl` 返回
  `total_pnl=-1.039467306`、`round_trips=1`，与逐笔价差+双边手续费一致。
- 测试数据清理：验证完成后删除两笔 SWAP 测试成交（`orders` / `fills` /
  `signals` / `positions` 四表同步），删前已备份
  `data/state/quant.db.bak-20261028-233452`。清理后
  `/api/pnl` 为 `fill_count=1 / round_trips=0`，持仓表只剩三个现货品种，
  看板告警无残留；今日 08:00 日报生成于切换品种之前（`BTC-USDT`），
  未受本次测试影响。
- 未完成：SMTP 仍缺 `SMTP_USERNAME/SMTP_PASSWORD` 与收件人，`test-email`
  无法实测；worker 的 WS 每轮 `Connection reset by peer` 仍未处理。

### 8.29 K 线面板补齐技术指标

- 图表库由 `lightweight-charts` 换成 `echarts`（按需引入），换取原生多 pane
  联动与指标生态；旧 `CandleChart` 组件与其依赖一并移除。
- 新增 `web/src/chart/`：`indicators.ts` 纯函数（SMA/EMA/RMA/BOLL/MACD/
  KDJ/ATR/SuperTrend/摆动点聚类撑压线）、`options.ts` 组装 ECharts option、
  `CandleChart.tsx`、`IndicatorBar.tsx`、`settings.ts`（localStorage 持久化）。
- 主图：MA（默认 5/10/20/50/200）、BOLL(20,2)、SuperTrend(10,3)、撑压线
  （左右各 3 根摆动点、0.5% 聚类、最多 5 条并标注触碰次数）；副图：VOL
  （含 5/10 量均线）、MACD(12/26/9)、KDJ(9,3,3)。副图按启用数量撑高画布。
- 交互：十字线跨 pane 联动、tooltip 汇总全部启用指标、滚轮缩放 + 底部滑块；
  轮询刷新保留用户缩放位置，切换周期才回到最近 120 根。开关与参数存
  `qt.chart.indicators.v1`，改参数即时重算。
- 验证：`vitest` 13 条指标用例通过（含 KDJ 的 J=3K−2D、SuperTrend 空头
  切换、撑压聚类触碰计数）；`tsc -b` 与 `vite build` 通过（gzip 250 KB）；
  浏览器实测默认三项副图、三个可选指标开关与刷新后设置恢复均正常。
  过程中修复 `grid.left/right` 传数字被当成像素、导致价格轴标签被裁的问题。

### 8.30 撑压线止盈止损接入策略（默认关）

- 新增 `src/quant/indicators/support_resistance.py`：`pivot_levels` /
  `nearest_levels` / `Level`，算法与看板 `indicators.ts` 逐条对齐（strict
  摆动点 → 按价格排序贪心聚类 → 取触碰数前 N → 升序返回），并用同一组输入
  输出做对齐用例，防止前后端漂移。
- `Strategy` 新增可选钩子 `protective_exit(bar, ctx) -> Decimal | None`
  （默认返回 None，不影响现有策略）。回测引擎在每根 bar 先查该钩子：返回
  价位即按该价位成交（`Matcher.fill_at`，不套滑点），成交后跳过本根 `on_bar`。
- 新增 `SrExitMixin`（`strategies/python/sr_exit.py`），`sma_cross` 与 `rsi`
  都继承，参数默认全关：`sr_enabled=false`、`sr_left_right=3`、
  `sr_tolerance_pct=0.5`、`sr_max_levels=5`。
- 规则：开仓信号产生时用已确认摆动点冻结止损（下方最近支撑）与止盈（上方
  最近压力），持仓期间不变；无支撑放弃开仓，无压力只止损；bar 内触及即成交，
  止损优先（同根同时触及按止损）；跳空越过价位按开盘价成交。
- 价位在「产生信号」时冻结而非等 `on_trade`：live 引擎补评估只做虚拟成交、
  不会触发 `on_trade`，依赖成交回调会导致实盘路径永远没有止损。
- live 引擎同步接入：补评估时把触发转成市价卖信号入队（成交价由 worker
  按当时盘口决定），虚拟成交按触发价记账，保证后续 bar 判断与回测一致。
- live 引擎每轮重建策略实例，持仓已存在时不会再走开仓分支，冻结档位无法跨轮
  保留；因此 `protective_exit` 在「持仓但无档位」时用持仓均价重新定档。
  副作用是档位随窗口缓慢漂移（回测路径不触发：开仓分支总会先冻结）。
- 验证：新增 27 条用例（撑压对齐/最近档位/参数校验、冻结、止损优先、跳空、
  无压力只止损、无支撑不开仓、live 补评估触发卖出、已持仓重新定档），
  全量 **977 passed**；`ruff check` 通过。
- 真实数据（BTC-USDT 1H，2026-01-01..2026-10-29，7237 根）：开启后最大回撤
  18.54% → **7.16%**，收益 0.33% → 0.76%，但平均持仓 38.5 → 5.5 根、盈亏比
  0.80 → 0.49 —— 最近支撑往往很近，1H 上容易被打掉，属于结构性止损的典型
  代价，参数还需按周期调。

### 8.31 回填 SWAP 4H/1D 与 4H 参数对照

- 背景：`[candles] insts` 已切到 `BTC-USDT-SWAP`，但本地只有该品种的 1H，
  4H/1D 仅现货有。切周期与趋势过滤都被数据卡住。
- 回填：`quant fetch-candles --inst BTC-USDT-SWAP` 补 4H（2024-01-01 起，
  净增 6,196 根）与 1D（净增 1,032 根）；`validate-data` 两周期均
  `errors=0, duplicates=0, missing=0, invalid_ohlc=0`。1D 首根为 16:00 UTC，
  是 OKX 日线的口径（港时零点），非异常。`data/candles` 由 3.0 MB 增至 3.4 MB。
- 现有本地行情：BTC-USDT-SWAP 1H 24,781 / 4H 6,196 / 1D 1,032；
  BTC-USDT 1H 17,605 / 4H / 1D；ETH-USDT 1H / 4H / 1D。
- 4H 参数对照（BTC-USDT-SWAP，2024-03-01..2026-10-29，预热 200 根）：

  | 参数 | 总收益 | 最大回撤 | 胜率 | 盈亏比 | 交易数 | 平均持仓 |
  |---|---|---|---|---|---|---|
  | 10/100 | 39.65% | 34.43% | 31.25% | 1.21 | 48 | 59.7 |
  | 10/100 + 撑压 | 22.57% | 15.23% | 35.42% | 1.26 | 48 | 9.7 |
  | 10/50 | 8.47% | 35.48% | 27.85% | 0.95 | 79 | 36.6 |
  | 10/50 + 撑压 | 1.16% | 27.71% | 36.49% | 0.78 | 74 | 7.1 |

- 结论一：4H 比 1H 高一个量级（1H 最优 0.33% vs 4H 10/100 的 39.65%），
  印证 §7 的判断——1H 亏损主要来自换手成本（66 轮≈20pp）。
- 结论二：撑压线在 4H 上不再拖累盈亏比（1.21 → 1.26），与 1H 的
  0.80 → 0.49 相反。收益腰斩但回撤由 34% 降到 15%，是收益换回撤的交易，
  是否默认开启取决于目标偏好。
- 结论三：`slow_len=50` 在 4H 上明显弱于 100，慢线还要继续拉长；
  下一步应在 4H 上扫 `slow_len` × 撑压参数，而不是沿用 1H 的 10/50。
- 注意：`[candles] bar = "1H"` 意味着日常同步只维护 1H，4H/1D 会停在
  2026-10-29。切 4H 时须同步改 `[candles] bar`、`[engine] bar`、
  `[engine] max_catchup_bars`（4H 下 24 根=4 天）、日报窗口与 `retention_days`。

### 8.32 补齐 5m/15m/30m/6H/12H 五周期

- 背景：图表与回测此前只支持 1m/5m/15m/30m/1H/4H/1D，缺 6H/12H；
  分钟级虽有 CLI 支持但从未回填，本地实际只有 1H/4H/1D。
- 代码放开周期：
  - `cli/__init__.py` 新增 `BAR_CHOICES` 常量（含 6H、12H），backtest /
    optimize / run-engine / paper-report 四处 `choices` 统一复用它；
    `sync-candles --bar` 改为逗号分隔的自由格式（不再受 choices 限制）。
  - `backtest/runner.py` 与 `report/daily.py` 的 `BAR_MS` 补
    `6H=21_600_000`、`12H=43_200_000`。周期换算的唯一实现
    `DataValidator._bar_size_ms` 本就按 `nH` 通用解析，无需改动。
  - 前端 `BAR_OPTIONS` 扩到 8 个周期；K 线面板的周期按钮仍优先用
    后端返回的 `available_bars`（磁盘实际有哪些），数据到位即自动出现。
- 多周期日常同步：
  - `CandleSyncSettings` 新增 `bars` 列表与 `resolved_bars` 属性
    （`bars` 为空回落为 `[bar]`，旧配置行为不变）。
  - `sync-candles` 一轮改为 `insts × bars` 双循环，单组失败只记日志，
    不中断其余组合；断点本就按 `(inst, bar)` 分文件，多周期互不覆盖。
  - `config/config.toml` 增 `bars = ["5m","15m","30m","1H","4H","6H","12H","1D"]`。
    每轮请求数 2 品种 × 8 周期 = 16 次，间隔 300s，压力可忽略。
- 回填（BTC-USDT-SWAP 与 BTC-USDT 各一份）：
  - 6H / 12H 全量 2024-01-01 起：各 4,130 / 2,065 根
  - 30m / 15m / 5m 近 6 个月（2026-04-29 起）：各 8,813 / 17,626 / 52,878 根
  - `validate-data` 五周期十组全部 `errors=0, duplicates=0, missing=0,
    invalid_ohlc=0`；`data/candles` 由 3.4 MB 增至 10 MB。
- 测试：新增 13 条（BAR_CHOICES 覆盖、6H/12H 在四类 CLI 子命令可解析、
  `sync-candles --bar` 自由格式、`_sync_once` 的 insts×bars 组合与
  单组失败不中断、`resolved_bars` 的回落与优先规则、BAR_MS 新增键），
  全量 **990 passed**，`ruff check` 通过。
- 已知限制：分钟级数据只回填 6 个月，5m 无法做超过半年的回测；
  `retention_days = 1095` 远大于回填跨度，不会被自动清理。
- 生效前提：supervisor 里常驻的 `sync-candles --loop` 只在进程启动时读配置，
  需重启 supervisor 才会开始维护新增周期（也可手动跑一次 `quant sync-candles`）。
- **重启后暴露并修复的缺陷**：`CandleFetcher.fetch()` 是「从新往旧」翻页，
  每页都把断点写成该页最新 ts，循环结束时断点停在**最旧**那一页，
  下一轮 `sync` 就以旧断点为 cursor 把整段历史重拉一遍。
  1H 上表现为每轮 18s 空转（一直从 2026-01-10 重拉 16k 根），
  多周期后 5m 单轮要拉 52k 根（~530 页），完全不可用。
  修复：主循环记录 `max_saved_ts`，循环结束后把断点推回已落盘的最新 ts；
  补单测 `test_multi_page_checkpoint_ends_at_newest`。
  同时把 16 个 `(inst, bar)` 断点按 parquet 实际最后一根重置，
  首轮即从最新出发（本轮 `new=24`，耗时不到 1 秒）。
- 重启后状态：7 个进程全部在线，dashboard `/api/health` 200，
  BTC-USDT 的 4H/1D 也补齐到最新（此前停在 10-24/10-26）。
  全量测试 **991 passed**。

### 8.33 实盘切 4H + EMA200 趋势过滤（2026-10-29）

- 背景：4H 上 `10/100` 回测收益 39.65%，1H 同期只有 0.33%，量级差在周期而非参数。
- 策略改动（`sma_cross.py`）：
  - 新增 `ema_len`（默认 200）与 `ema_filter`（默认关闭，由策略 toml 开启）。
  - `_ema_last()` 以最早 `period` 根的简单平均作种子，之后逐根递推，
    不依赖全量历史，回测与实盘口径一致。
  - 过滤只作用于开仓：金叉且 `close > EMA200` 才买；死叉平仓与撑压
    止盈止损完全不变，不加做空。
  - 开启过滤时窗口要求 `max(slow_len + 1, ema_len)`，不足则不出信号。
- 配套改动：
  - CLI `--warmup-bars` 默认 200 → 300（EMA200 需 201 根收盘）。
  - `build_report()` 的 `config_dir` 默认改为 `None`，回落
    `settings.report.config_dir`，日报测试不再隐式依赖仓库实盘参数。
- 对照回测（BTC-USDT-SWAP 4H，2024-03-01..2026-10-29，5,836 根，预热 300）：

  | 参数 | 收益 | 最大回撤 | 夏普 | 胜率 | 盈亏比 | 交易数 |
  |---|---|---|---|---|---|---|
  | 10/50 | 8.47% | 35.48% | 0.25 | 27.85% | 0.95 | 79 |
  | 10/50 + EMA200 | -3.09% | 22.50% | 0.04 | 31.58% | 0.84 | 38 |
  | 10/100 | 39.65% | 34.43% | 0.57 | 31.25% | 1.21 | 48 |
  | **10/100 + EMA200** | **63.74%** | **17.31%** | **0.90** | 32.00% | 1.94 | 25 |
  | 10/100 + EMA200 + 撑压 | 30.33% | 8.39% | 0.86 | 28.00% | 2.34 | 25 |
  | 10/100 + EMA150 | 66.63% | 17.65% | 0.90 | 33.33% | 1.86 | 30 |

  结论：趋势过滤在慢线 100 时同时抬高收益与回撤表现，在慢线 50 时反而转负
  （过滤掉的主要是震荡段的假突破，而 10/50 本身仍要承受震荡损耗）；
  EMA150 与 EMA200 结果接近，说明不是单点过拟合。
  撑压叠加后收益腰斩但回撤降到 8.39%，夏普基本持平（0.86 vs 0.90），
  本轮先不上，留作低风险偏好时的备选。
- 配置落地（`config.toml` 与策略 toml 均在 `.gitignore` 中，仅本地生效）：

  | 配置 | 现值 |
  |---|---|
  | `[candles] bar` / `[engine] bar` / `[report] bar` | 4H |
  | `[engine] warmup_bars` / `[backtest] window_size` | 300 |
  | `[engine] max_catchup_bars` | 6（4H 一天 6 根） |
  | `[report] window_hours` | 168（4H 一周 42 根，保证有成交可比） |
  | `sma_cross.toml` params | fast=10 / slow=100 / ema_len=200 / ema_filter=true |

- 注意：`ema_len=250` 在 `window_size=200` 时一笔都不成交（窗口被裁剪，
  EMA 永远算不出来），这也是本次必须把窗口抬到 300 的原因之一。
- 测试：新增 6 条 EMA 用例（递推数值、样本不足、低于 EMA 拦截开仓、
  站上 EMA 放行、死叉不受过滤影响、窗口不足不出信号），
  全量 **997 passed**，`ruff check` 通过。

### 8.34 Walk-forward 验证（2026-10-30）

- 背景：4H + EMA200 的 63.74% 来自单段「全历史当 IS」的回测，无法排除
  参数是在整段历史上挑出来的。walk-forward 用滚动 IS/OOS 检验同一套参数
  在未知区间是否还成立。
- 新增能力：
  - `src/quant/backtest/walkforward.py`：`build_segments()` 按 bar 根数切
    滚动段，`WalkForward.run()` 每段在 IS 上按 metric 选参、再用该参数跑
    紧随的 OOS；OOS 结果只做评估，不回流选参。
  - `quant walk-forward` 子命令（第 20 个），参数 `--is-bars/--oos-bars/
    --step-bars/--metric/--grid`，输出逐段表格与汇总。
  - 预热口径：每段只取「该段起点之前」的 bar（IS 与 OOS 各自向前取
    `--warmup-bars` 根），与实盘可见信息一致。
- 首轮结果（BTC-USDT-SWAP 4H，2024-03-01..2026-10-29，预热 300，
  网格 `slow_len=50,100,150 × ema_filter=false,true`，metric=sharpe）：

  | 切分 | 段数 | OOS 复合收益 | 盈利段数 | OOS 平均夏普 | 参数众数 |
  |---|---|---|---|---|---|
  | IS 1500 / OOS 500 | 8 | 46.11% | 4/8 | 0.57 | 10/100 + EMA200 |
  | IS 1000 / OOS 350 | 13 | 91.11% | 7/13 | 0.70 | 10/100 + 无过滤 |

  - `slow_len=100`、`fast_len=10` 在两种切分下都是众数，参数本身稳定；
  - `ema_filter` 的众数在两种切分下相反（True / False），说明过滤收益
    依赖行情阶段：2024 年的震荡段关掉更好，2025 年之后的趋势段打开更好；
  - 单段 OOS 表现分化明显（同一切分里既有 +28%、+19%，也有 -6%），
    4/8 与 7/13 的胜率说明这套策略靠少数大段趋势赚钱，属趋势跟踪的正常形态；
  - OOS 累计交易数只有 21 / 31 笔，样本偏少，结论应视为「方向可信、
    幅度不可照搬」。
- 结论：保留 10/100 + EMA200 的实盘配置（众数之一，且在 1500/500 切分下
  为众数），但把「过滤开关随行情切换」列入后续观察项，必要时改成可配置的
  自适应阈值而不是二值开关。
- 测试：新增 `tests/test_walkforward.py` 18 条（切分相邻/重叠/越界、
  选参不回流、预热不足、复合收益、参数众数、CLI 解析），
  全量 **1,015 passed**，`ruff check` 通过。

### 8.35 EMA 过滤改为 ATR 自适应阈值（2026-10-30）

- 动因：§8.34 显示 `ema_filter` 的最优取值随行情阶段翻转（2024 震荡段关掉
  更好、2025 后趋势段打开更好）。把二值判断换成随波动自适应的门槛。
- 实现（`sma_cross.py`）：
  - 门槛 = `EMA(ema_len) + ema_band_atr × ATR(atr_len)`，`close > 门槛` 才开仓。
  - 新增参数 `ema_band_atr`（默认 0）、`atr_len`（默认 14）；`ema_band_atr = 0`
    时门槛退化为 EMA，与 §8.33 的旧行为逐笔一致（有单测固定）。
  - `ema_filter` 仍是总开关：关闭时整段趋势判定直接放行。
  - 负的 `ema_band_atr`、非正的 `atr_len` 直接报错。
- **口径调整（与设计稿的差异）**：ATR 用「最近 `atr_len` 个真实波幅的等权
  平均」，不做 Wilder 递推。Wilder 平滑会让 ATR 依赖可用窗口长度，
  `window_size` 或预热根数一改结果就漂移，回测/实盘可见窗口不一致时会静默
  偏差；等权窗口完全由最近 `atr_len + 1` 根决定，跨环境可复现。
- 直接回测（BTC-USDT-SWAP 4H，2024-03-01..2026-10-29，10/100 + EMA200）：

  | k | 收益 | 最大回撤 | 夏普 | 盈亏比 | 交易数 |
  |---|---|---|---|---|---|
  | **0** | **63.56%** | 17.64% | 0.89 | 1.82 | 28 |
  | 0.5 | 63.76% | 17.64% | 0.91 | 1.94 | 25 |
  | 1 | 46.29% | 15.05% | 0.80 | 1.97 | 20 |
  | 1.5 | 35.39% | 18.76% | 0.67 | 1.76 | 18 |
  | 2 | 19.94% | 17.93% | 0.48 | 1.53 | 13 |
  | 3 | 15.10% | 11.86% | 0.45 | 1.88 | 6 |

  k ≥ 1 后收益单调下降，回撤只在 k=3 才明显改善；k=0.5 与 k=0 实质等价。
  带宽的主要作用是**减少交易次数**，而不是提升单笔质量。
- walk-forward（同一网格 `ema_band_atr=0,0.5,1,1.5`，slow=100，ema_filter=true）：

  | 切分 | 段数 | OOS 复合收益 | 盈利段数 | 平均夏普 | 参数众数 |
  |---|---|---|---|---|---|
  | IS 1500 / OOS 500 | 8 | 84.07% | 5/8 | 1.29 | k=1 |
  | IS 1000 / OOS 350 | 13 | 75.56% | 4/13 | 0.70 | k=0 |

  两种切分给出的众数相反，且 1500/500 那组全期 OOS 只有 13 笔成交，
  复合收益被少数几笔主导，不足以支撑 k>0。
- **结论**：能力保留、线上配置保持 `ema_band_atr = 0`。带宽没有呈现出
  稳定的超额收益，属于「换了风险形状但没有证据说更好」，按 YAGNI 不启用。
  若后续出现明显的高波动假突破行情，可直接把 k 调到 0.5–1 复测，无需改代码。
- 配置：`config/strategies/sma_cross.toml` 显式写明 `ema_band_atr = 0`、
  `atr_len = 14`（该文件在 `.gitignore` 中）。engine 每轮都会重新加载策略
  参数，改完无需重启。
- 测试：新增 7 条（ATR 等权窗口数值、跳空用前收盘、样本不足、窗口外数据
  不参与、带宽拦截刚站上 EMA 的收盘、k=0 等价旧行为、负 k/非正 atr_len 报错），
  全量 **1,024 passed**，`ruff check` 通过。

### 8.36 PRD 重构为 10 个产品模块（2026-10-30）

- 背景：v0.5 按 F1–F5 横向切分（数据/回测/模拟盘/实盘/监控），与看板页面结构
  对不上，也把「因子库」「组合管理」这类能力漏在视野外。
- 新文档 `docs/requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md`：
  10 个模块 = 看板导航项，每模块内按 `M<模块号>-<序号>` 编号，每条带
  「来源」（原 F 编号或「新增」）与「状态」（✅/🟡/⬜）。
- 模块与现有实现的对应：
  - 市场监控、回测分析、交易执行 → ✅ 基本实现
  - 首页总览、策略研究、风险控制、日志告警、系统设置 → 🟡 部分实现
  - 因子库、组合管理 → ⬜ 未开始（新写基础设计，排 M7+）
- 非目标调整：删除 v0.5 的「不做 Web UI 界面」，改为「本机单用户看板」。
- 里程碑改为「模块 × 里程碑」矩阵，M1–M3 已完成的交付不变，只做归属映射。
- 链接同步：`PROGRESS.md`、`PROJECT_STRUCTURE.md`、两份 spec、`README.md`
  的 PRD 引用改指 v0.6；v0.5 保留为历史版本。

### 8.37 因子库与组合管理需求细化（2026-10-30）

- PRD 4.5/4.6 由各 6 条基础设计扩写为完整设计，两个模块仍标 ⬜ 未开始、排 MS7+。
- 因子库：15 条需求 + 3 个数据模型（`FactorDef`/`FactorValue`/`FactorEval`）
  + `data/factors/{factor}/{bar}/{year}.parquet` 存储布局 + 5 个接口
  （注册、时序 `compute`、横截面 `compute_panel`、`evaluate`、`FactorProvider`）。
  范围含时序与横截面两类；关键约束是「全量重算与增量更新必须逐点相等」。
- 组合管理：14 条需求 + 4 个数据模型（`PortfolioConfig`/`PortfolioUnit`/
  `RebalancePlan`/`PortfolioSnapshot`）+ `config/portfolios/<id>.toml` 与
  SQLite 两张新表 + 3 个接口（`Allocator.allocate`/`Rebalancer.evaluate`/
  `Attribution.report`）。边界明确：组合层只出目标权重与调仓计划，
  下单走 M8、风控走 M7，不新增第二条下单路径。
- 编号去歧义：PRD 里程碑由 `M1–M6` 改为 `MS1–MS6`，与模块编号 `M1–M10` 区分，
  第 3 章补了说明，8.2 矩阵表头同步。

### 8.38 回测分析需求细化（2026-10-30）

- PRD 4.4 由 8 条扩写为 **16 条** + 5 个数据模型 + 4 张 SQLite 表
  （`backtest_runs` / `backtest_metrics` / `backtest_artifacts` / `optimize_jobs`）
  + `data/backtests/{run_id}/` 文件布局 + 4 个接口。
- 新增条目针对现有缺口：
  - 结果不落库 → M4-9 持久化、M4-10 历史列表与重跑、M4-11 多组对比
  - 长任务卡请求 → M4-12 后台任务 + 轮询、M4-13 寻优并发与进度
  - 指标偏少 → M4-14 补 Calmar/Sortino/月度/连亏/暴露度
  - 永续未计资金费率 → M4-15 按 8 小时结算
  - 无 Pine 对比入口 → M4-16 导入 `metric,value` CSV 算偏离度
- 存储约定：DB 只存登记与指标，曲线/成交/段结果落 parquet，DB 存路径；
  金额 TEXT 存 Decimal，指标 REAL。
- 目录结构补 `src/quant/research/`（任务编排与结果持久化）与
  `data/backtests/`；MS4 范围同步纳入 M4-9 ~ M4-14。

### 8.39 交易执行需求细化（2026-10-30）

- PRD 4.8 由 13 条扩写为 **19 条** + 6 个数据模型 + **2 张新 SQLite 表**
  （`dead_letters` / `reconciliations`）+ `orders` 加 3 列
  （`filled_qty` / `avg_price` / `source`）+ 5 个接口
  （`PositionSizer.size` / `OrderStateMachine.transition` /
  `DeadLetterService` / `Reconciler.reconcile` / `ManualTrader`）。
- 新增 6 条针对现有缺口：
  - 信号强制带 qty 与「Python 端算仓位」矛盾 → M8-14 服务端仓位计算
  - `partially_filled` 无状态推进 → M8-15 订单状态机
  - 死信只有计数无入口 → M8-16 查看与重放/丢弃
  - 撤单改单已实现但未暴露 → M8-17 手工下单与撤改
  - 对账只覆盖订单 → M8-18 订单 + 持仓 + 余额三项对账
  - 超时不明单重复提交风险 → M8-19 幂等与重试边界
- 修正 9.2 决策表：`qty` 由必填改为可选，缺省由服务端按权益比例计算。
- 里程碑：MS4 纳入 M8-14 ~ M8-16、M8-19（执行可靠性）；
  MS5 纳入 M8-17、M8-18（手工交易与三项对账）。

### 8.40 风险控制需求细化（2026-10-30）

- PRD 4.7 由 9 条扩写为 **17 条** + 5 个数据模型 + **2 张新 SQLite 表**
  （`risk_events` / `risk_overrides`）+ 5 个接口
  （`resolve_limits` / `RiskChecker.check` 扩展 `hits` /
  `RiskOverrideService` / `CircuitBreaker` / `RiskEventService`）。
- **修正一处误标**：M7-6「极端行情熔断」在 v0.6 初稿标 ✅，但代码中
  完全不存在（全仓无 `circuit`/振幅相关实现），已改为 ⬜ 并补齐需求。
- 新增 8 条针对现有缺口：
  - 风控参数只有策略级、总仓位却汇总全品种 → M7-10 两层取严合并
  - 拒绝与减量只在日志里 → M7-11 事件落库、M7-16 边界单测
  - 阈值改完无法即时生效 → M7-8 在线覆盖、M7-14 变更审计
  - 熔断缺失且与 kill switch 语义混淆 → M7-6、M7-12、M7-13
  - 权益口径混用 `initial_equity` → M7-15 统一取 monitor 快照
  - 风控与告警不同步 → M7-17
- 规则执行顺序明确为：日亏损 → 连亏 → 熔断（硬停止）→ 单笔名义价值
  → 单品种比例 → 总仓位比例（软限，可下调数量）。
- 里程碑：MS2 补入 M7-1 ~ M7-3；MS4 纳入 M7-10/M7-11/M7-15/M7-16；
  MS5 纳入 M7-6 ~ M7-9、M7-12 ~ M7-14、M7-17。

### 8.41 日志告警需求细化（2026-10-30）

- PRD 4.9 由 7 条扩写为 **14 条** + 3 个数据模型 + **2 张新 SQLite 表**
  （`alerts` / `alert_silences`）+ 3 个接口
  （`AlertService.publish` / `list` / `ack`+`silence`）。
- **修正一处口径错误**：M9-1 原写「按模块+日期分文件」，实际是 loguru
  单文件 `logs/quant-trading.log`（10MB 轮转、30 天保留），模块名只在行内。
  已改为「单文件 + 行内模块标签」，与实现一致。
- **引入新状态标记 🚫**（明确不做），M9-6 日志检索改标 🚫：本期不做检索页，
  日志走 `grep` + 时间段。第 3 章状态定义已同步。
- 新增 7 条针对现有缺口：
  - 告警只写 Redis list（keep 200），重启丢 → M9-8 SQLite 主存
  - `Alert` 只有 4 字段、无状态 → M9-7 已读、M9-10 静音粒度
  - 规则清单未成文 → M9-2 明确 11 条已实现规则
  - 去重逻辑隐式 → M9-9 显式化（冷却 600s、kill switch 例外、INFO 例外）
  - 事件来源分散 → M9-11 统一 `AlertService.publish` 入口
  - 无保留策略与统计 → M9-12 按级别保留、M9-14 趋势统计
- 里程碑：MS4 纳入 M9-1 ~ M9-5、M9-8、M9-9、M9-11、M9-13；
  MS5 纳入 M9-7、M9-10、M9-12、M9-14。

### 8.42 策略研究需求细化（2026-10-30）

- PRD 4.3 由 8 条扩写为 **15 条** + 5 个数据模型 + **3 张新 SQLite 表**
  （`strategy_params` / `strategy_param_changes` / `strategy_bindings`）+ 4 个接口
  （`spec` / `effective_params` / `StrategyParamService.update` /
  `StrategyBindingService`）。
- 现状核对：看板只有 `GET /api/strategies` 只读清单，**没有参数编辑与启停接口**，
  参数校验只靠策略 `on_init` 里 `raise ValueError`，无声明式 schema，
  参数变更无任何留痕 —— 这三处是本节新增条目的直接来源。
- 新增 7 条：
  - 参数 schema 无处可查 → M3-9 声明式 `PARAM_SPEC`
  - 回测与实盘可能读到不同参数 → M3-10 两层合并共用一份
  - 启停只有进程级 → M3-11 策略×品种矩阵、M3-12 即时生效
  - Pine 与 Python 可能漂移 → M3-13 一致性校验
  - 改错参数无法回退 → M3-14 变更回滚
  - 信息分散在多个页面 → M3-15 策略详情页
- 关键约束：**同一策略同一品种只允许一个周期**（`bar` 记在绑定上但不是键），
  避免两个周期对同一持仓重复下单。
- 里程碑：MS4 纳入 M3-9 ~ M3-12（参数与启停基础设施）；
  MS5 纳入 M3-3、M3-4、M3-8、M3-13 ~ M3-15（页面与治理）。

### 8.43 首页总览需求细化（2026-10-30）

- PRD 4.1 由 7 条扩写为 **10 条** + 4 个只读聚合视图（**不新增 SQLite 表**）
  + 3 个接口变更（`/api/overview` 扩展 `processes[]`、新增 `/api/timeline`
  与 `/api/freshness`）。
- 现状核对发现两处不一致：
  - **M1-4 权益曲线**：PRD 原写「7/30/90 天切换」，实际前端只有
    24h/7d/30d，后端 `hours` 硬上限 720（30 天）。已改为 24h/7d/30d。
  - **M1-5 进程健康**：PRD 原写「7 个进程各自心跳」，实际只有
    worker/monitor 写心跳键、engine 用自己的 status 键；
    candles/webhook/dashboard/supervisor **完全没有心跳**。
- 新增 3 条：M1-8 今日盈亏汇总、M1-9 告警摘要卡、M1-10 降级与时效标注；
  M1-6/M1-7 补充实现规范（四表 UNION、只盖引擎消费的组合）。
- 新增 Redis 键约定：`quant:heartbeat:{name}`（TTL = 2 × 轮询间隔）、
  `quant:process:{name}`（pid/started_at/restarts，由 supervisor 写入）。
- 里程碑：MS4 扩为 M1-1 ~ M1-7、M1-10；MS5 纳入 M1-8、M1-9；
  **MS6 移除重复的 M1-5**（进程自愈归 M8-11）。

### 8.44 市场监控需求细化（2026-10-30）

- PRD 4.2 由 13 条扩写为 **15 条** + 5 个数据模型 + 1 个新接口
  （`GET /api/quality`）+ 1 个新落盘目录（`data/quality/`）。
- **修正三处与实现不符**：
  - **M2-2**：原写「订阅 tickers/books/trades + 缓存最近 N 条」。
    实际 [ws_client.py](../src/quant/okx/ws_client.py) 只有 `tickers`；
    [LivePriceBook](../src/quant/execution/price.py) 每品种**只存最新一条**。
    改为「仅 tickers、只留最新」，状态 🟡（重连退避饱和待修）。
  - **M2-4**：原写「资金费率与持仓量」。全仓 grep 无任何 open interest 实现，
    只有 `funding_rate_fetcher.py`。拆为 M2-4 资金费率 ✅ + M2-14 持仓量 ⬜。
  - **M2-10**：原写「SMA/EMA 多条均线」，实际看板可叠加 MA/BOLL/SuperTrend/
    撑压线/VOL/MACD/KDJ 共 7 类，描述已改全。
- 新增 2 条：M2-14 持仓量（⬜）、M2-15 数据质量报告（⬜，含
  `DataQualityReport` 模型）；M2-13 自选与对比改标 🚫。
- 顺带明确两处实现口径：`DataValidator` 返回值扩展为结构化报告；
  `WSClient` 退避加 ±20% 抖动并把连接状态写 Redis（供 M1-5 复用）。
- 里程碑：MS4 纳入 M2-15（数据质量报告）。

### 8.45 系统设置需求细化（2026-10-30）

- PRD 4.10 由 8 条扩写为 **15 条** + 4 个数据模型（`ConfigView` /
  `CredentialStatus` / `ProcessCommand` / `BackupManifest`）+ 5 组接口
  + 3 个新 CLI（`quant backup` / `restore` / `doctor`）。
  **不新增 SQLite 表**：热改复用已有覆盖层。
- **修正四处与实现不符**：
  - **M10-1** 配置查看：原标 🟡，实际看板**没有任何 `/api/config`**，
    改为 ⬜。
  - **M10-3** 数据保留：原标 ✅，实际
    [retention.py](../src/quant/storage/retention.py) 只覆盖 bars/reports/
    signals/orders/fills/equity_snapshots，且是**单一全局 `retention_days`**，
    缺 alerts/dead_letters/funding_rates 与按周期分档，改为 🟡。
  - **M10-4** 通知设置：原标 🟡，实际看板无通知接口，改为 ⬜。
  - **M10-2** 凭证管理：重写为「健康看板 + 模拟盘验证按钮」，
    只读不落盘 secret。
- 新增 7 条：M10-9 配置来源标注、M10-10 热改白名单、M10-11 凭证到期提醒、
  M10-12 备份完整性校验、M10-13 保留策略分档、M10-14 进程命令审计、
  M10-15 部署自检。
- 关键边界：M10 **不自己实现参数覆盖**，热改白名单委托
  M7-8 `risk_overrides`（风控阈值）与 M3-10 `strategy_params`（策略参数），
  审计分别复用 M7-14 与 M3-8。
- 进程控制：命令走 **Redis 命令通道**（`quant:supervisor:cmd`），
  supervisor 轮询执行并回写结果，避免看板直连进程。
- 新增配置项：`retention_bars_days`（按周期分档，如
  `{ "1m" = 365, "1H" = 1095 }`）、`retention_alerts_days`、
  `retention_dead_letters_days`、`retention_funding_days`。
- 里程碑：MS5 纳入 M10-1 ~ M10-4、M10-9 ~ M10-15；
  MS6 保留 M10-6、M10-7（上云、systemd 托管与开机自启）。

### 8.46 PRD v0.6 全篇一致性复核（2026-10-30）

十大模块细化完成后做了一次通读复核，修掉 5 处内部矛盾与 2 处状态错标：

- **v0.5 遗留口径（与模块章节自相矛盾）**：
  - §5.3「日志按模块+日期分文件」→ 改为单文件 + 行内前缀，与 M9-1 一致。
  - §9.1 风险表「M6 上云；M5 前小资金试错」→ 旧里程碑编号，改为 MS6/MS5。
  - §7 `data/` 目录漏了 `instruments/`（[instrument_storage.py](../src/quant/storage/instrument_storage.py)）、
    `funding_rates/`（[funding_rate_fetcher.py](../src/quant/fetcher/funding_rate_fetcher.py)）
    与 `.checkpoints/`，已补。
  - §5.1「WebSocket 重连 < 5 秒」与 M2-2 的 5s→60s 退避冲突，改为
    「首次重连 < 5 秒、最大退避 60 秒」。
  - §5.4「写接口需 token」与 M10-8 的「token 可选」冲突，已对齐。
- **状态错标**：
  - **M9-2 ✅→🟡**：11 条规则齐备、阈值在 `[monitor]` 可配，但**没有规则级启停**。
  - **M2-4 ✅→🟡**：[client.py](../src/quant/okx/client.py) 的 `get_funding_rate` 只取
    `/public/funding-rate` 最新一条，未接 `/public/funding-rate-history`，
    **无法回补历史区间**；M4-15 的资金费率成本回测需先补此能力，前置条件已写明。
- **口径补充**：`PineBaseline` 复用 `backtest_metrics` 的 `pine_*` 列（5 模型 ↔ 4 表，
  避免读者以为漏了一张表）；`max_order_qty` 沿用旧名但实际是**名义价值（USDT）**
  （见 [risk/config.py](../src/quant/risk/config.py)）。
- **里程碑**：M4-6（walk-forward）已在本轮实现，从「随 MS1–MS3 交付」的口径说明里
  移出，正式列入 MS4。
- 复核结论：全文 1007 行、150 条需求且无重复编号；MS 覆盖差集只剩 M5/M6（由 MS7+
  「全部」承接）与两条 🚫；状态分布 ✅47 / 🟡23 / ⬜78 / 🚫2。

### 8.47 重点模块（M3/M4/M5/M7）逐条对代码复核（2026-10-30）

对策略、回测、因子、风控四个模块逐条比对实现，发现 9 处状态高估、2 处逻辑缺口、
2 处字段名与实际不符。

**状态高估（已按实际下调）**

| 条目 | 原 | 现 | 依据 |
|---|---|---|---|
| M3-3 参数表单 | 🟡 | ⬜ | 看板只有 `GET /api/strategies` 返回 `{id, params}`，无表单无写入 |
| M3-4 策略启停矩阵 | 🟡 | ⬜ | [config.py](../src/quant/foundation/config.py#L148-L151) 的 `[engine]` 只有单个 `strategy`/`inst`；engine 是单进程单组合 |
| M3-5 信号预览 | ✅ | 🟡 | 只有 `waiting_data`/`no_signal`/`skipped`/`duplicate`/`error` 粗粒度状态，策略内部的趋势过滤/撑压拦截拿不到 |
| M3-7 研究工作流 | 🟡 | ⬜ | `notebooks/` 只有 `.gitkeep` |
| M4-7 批量回测 | 🟡 | ⬜ | 只有单策略单品种回测与单策略网格寻优，无跨品种/跨周期批量 |
| M4-8 与实盘偏离 | ✅ | 🟡 | 判定结论只进日报 CSV 与邮件，未写 `alerts`，未进告警体系 |
| M7-4 kill switch | ✅ | 🟡 | 撤单只在 CLI `quant kill-switch --on` 里做，看板按钮仅写标志 |
| M8-14 服务端仓位 | 🟡 | ⬜ | [signal.py](../src/quant/backtest/signal.py#L62-L63) 与 [parser.py](../src/quant/webhook/parser.py#L19) 均要求 `qty` 必填，无 `PositionSizer` |
| M9-5 告警列表 | ✅ | 🟡 | 只有级别 + 规则筛选与分组折叠，无时间区间、无状态筛选 |

**反向修正（原标 ⬜ 但实际已有部分）**

- **M4-10 历史列表 → 🟡**：前端已有 localStorage 摘要历史（上限 20 条，同配置覆盖），
  但非服务端登记、无筛选、无重跑。
- **M4-11 多组对比 → 🟡**：前端已有指标横向对比表并高亮最优值，但数据取自
  localStorage，且未校验同品种同周期。

**逻辑缺口（已补进 PRD）**

- **M5 窗口约束**：`Context.bars_window` 被裁剪到 `max(warmup_bars, window_size)`，
  因子 `lookback` 超出时只会拿到截断历史并**静默算错**。已要求 engine/回测装配
  `FactorProvider` 时硬校验 `窗口 ≥ max(lookback)+1`，不足直接报错，不降级。
- **M7 日切口径冲突**：风控「日亏损」按 UTC+8 切日（`day_start_ms_utc8`，
  [worker.py](../src/quant/execution/worker.py#L154) 实际调用），而 §5.6 写「跨日聚合按
  UTC 日」。不是 bug 而是两套口径并存，已在 M7 加口径说明并在 §5.6 标注例外，
  同时要求 UI 显示时区，否则「今日亏损」会出现两个数。

**字段名与实际不符（已改）**

- `Bar` 漏了 `vol_quote`/`confirm`；`Instrument` 的 PRD 字段是 OKX 驼峰
  （`tickSz`/`lotSz`/`minSz`），实现里早已是 snake_case 且多了 `ct_val`。
- M2-5「保留 OKX 原始字段名」为假：parquet 列是 `vol_quote`/`confirm`，
  产品 JSON 也是 `tick_sz`。改为「统一 snake_case，保持与 OKX 原名一一对应」。

**里程碑调整**

- M7-4（kill switch 撤单）列入 MS4，属现有按钮的补齐而非新功能。
- M3-5 / M4-8 / M9-5 三条 🟡 的残余能力归 MS5 收口：
  分别依赖 M3-15 详情页、M9-11 告警入口、M9-7 已读状态。

**复核结果**：150 条 / 1014 行；状态分布 ✅43 / 🟡24 / ⬜81 / 🚫2；
MS 覆盖差集只剩 12 条 ✅（随 MS1–MS3 交付，已在口径说明列出）与 2 条 🚫；无悬挂引用。

### 8.48 详设补全：编号消歧 + MS4 三份详设（2026-10-30）

PRD 细化到 150 条后，详设侧暴露出两个问题：一是文件名与标题用旧里程碑编号 `M1/M2`，
与 PRD 模块编号 `M1 首页总览`、`M2 市场监控` 直接撞车；二是 MS4（进行中）覆盖 7 个模块
切片、约 35 条需求，**完全没有设计文档**。

**编号消歧**

- `m1-data-foundation-design.md` → `ms1-data-foundation-design.md`，标题 `M1 数据底座` → `MS1 数据底座`
- `m2-backtest-webhook-design.md` → `ms2-backtest-webhook-design.md`，标题同理
- 两份文件头部各加「编号约定」声明：MS = 里程碑，M1–M10 = PRD 模块
- 同步更新 README / PROJECT_STRUCTURE / PROGRESS 中 7 处引用
- 顺带修 README 里程碑表：原文仍是「M1 设计完成待实施 / M2 待设计」，已改为 MS1–MS3
  已交付、MS4 进行中、MS5/MS6 未开始、MS7+ 候选

**老 spec 对账**（不回改历史章节，新增对账小节）

- MS1 §15.3：4 处偏差（Python 3.13、`data/quality` 新增、保留策略仍单一天数、WS 退避
  饱和到 60s）+ 5 项开放问题裁决（parquet 仍 string、不用 polars、WS 重连已实现、
  不用 apscheduler、断点存 JSON）
- MS2 §17.3：6 处偏差（风控规则顺序中熔断不在 checker 内、`max_order_qty` 实为名义价值、
  kill switch 看板不撤单、`Reconciler` 已实现、服务端仓位未实现、walk-forward 已实现）
  + 4 项开放问题裁决

**MS4 三份详设**

| 文档 | 覆盖条目 | 核心设计 |
|---|---|---|
| [MS4-A 告警与首页](specs/2026-10-30-ms4a-alerts-overview-design.md) | M1-1~7/9/10、M9-1~5/7~13 | SQLite 迁移机制（`user_version`）首次引入；`alerts`/`alert_silences` 两表；`AlertService`（落库→Redis→按静音决定是否发信）；7 进程心跳；四表 UNION 事件流；parquet 统计 O(1) 读新鲜度 |
| [MS4-B 风控与执行](specs/2026-10-30-ms4b-risk-execution-design.md) | M7-4/5/10/11/15/16、M8-14~16/19 | 风控三层参数（默认/全局/策略/在线覆盖）与来源标注；`risk_events` 审计；`risk_overrides` 单表兼当前值与变更史；权益缺失改为拒绝；kill switch 走命令通道撤单；订单状态机与成交累加（**修真实缺陷**）；`PositionSizer`；下单幂等 |
| [MS4-C 回测与策略](specs/2026-10-30-ms4c-backtest-strategy-design.md) | M2-15、M3-9~12、M4-6/9~14 | `PARAM_SPEC` + `resolve_params` 让回测与实盘共用同一真相源；`strategy_bindings` 与引擎多绑定；`TaskRunner` + 4 张表落库；寻优进程池；7 项扩展指标；`data/quality` 质量报告 |

**三份详设共同确立的约定**

- 迁移版本占用：MS4-A = v2/v3，MS4-B = v4/v5，MS4-C = v6，避免并行开发时冲突
- 所有「无法计算」的指标一律返回 `None` 而非 0，与 PRD M1-10「不把缺失静默渲染成 0」一致
- 每个切片都写「回滚开关」：`alerts_persist`、`allow_initial_equity_fallback`、
  `multi_binding`、`resolve_from_db` 等，出问题可只关功能不回滚代码

**对账中发现的实现缺陷（已写入详设，待 MS4 修复）**

- 订单两次部分成交时第二次覆盖第一次 → `filled_qty` 偏小、均价错误
- 权益快照缺失时退回 `initial_cash` 做风控判定 → 敞口可能放大数倍
- kill switch 看板按钮不撤单 → 按下后交易所挂单仍在

---

### 8.49 启动阻塞修复 + 工程化收尾（2026-11-02 ~ 2026-11-03）

**背景**：接手时系统本应在跑，但 5 个常驻子进程（worker / engine / webhook / monitor /
dashboard）全部秒退，只有不碰库的 `sync-candles` 存活；看板 `:8080` 打不开。

**根因**：存量库 `data/state/quant.db` 的 `user_version = 8`，且
`backtest_metrics.funding_cost` 列**已经存在**（更早一版该列写在基线 `_SCHEMA`，后被抽成
v9 增量迁移 `ALTER TABLE ... ADD COLUMN funding_cost`）。当前代码打开库时，v2–v8 已应用，
轮到 v9 想再加一次 → `sqlite3.OperationalError: duplicate column name: funding_cost`
→ `SQLiteStore.__init__` 抛 `StateError` → 5 个子进程全崩。这正好违反 `AGENTS.md` 硬规则
#2「迁移必须幂等」。

**修复（只动迁移循环，不改任何历史 DDL）**：
- `src/quant/state/sqlite_store.py::_apply_migrations`：对每段迁移的 `executescript` 包一层
  `try/except sqlite3.OperationalError`，仅当错误含 `duplicate column` 时视为「已应用」
  跳过，其余错误照常上抛。
- 不新增迁移段、不修改 `_SCHEMA` 与既有 `_MIGRATIONS`，符合「历史迁移不可改」的硬规则。

**验证**：`read_lints` 零错误；`tests/test_sqlite_store.py` **71 passed**（含
`test_v8_db_gains_funding_cost_column` 等迁移用例），无回归；cash 模式 supervisor 在下一个
退避周期内自愈拉满 6 进程。

**运维处置**：机器上原本还跑着一个 `cross` 模式的旧 supervisor（21:14 起），与本次要的
`cash --paper-mode` 冲突且抢占 `:8000`/`:8080` 与信号队列。经用户确认，停掉旧 cross 实例、
保留 cash 实例；端口释放后 6 进程全部在线（candles / webhook / worker / engine / monitor /
dashboard）。

**前端构建**：`cd web && npm install && npm run build`，产出 `web/dist`；dashboard 由
「API-only」切换为完整 React UI（标题 `QUANT / OPS`），浏览器 `http://127.0.0.1:8080`
可看完整看板（账户 / 权益曲线 / 持仓 / 订单 / 成交 / 告警 / 策略 / 回测）。

**工程化**：把 `AGENTS.md` 的协作纪律（含用户后补的「先想透 / 写完必测 / 做完必总结落文档 /
详细注释」）固化为项目级 skill `quant-trading-rules`
（`.codebuddy/skills/quant-trading-rules/`，已通过官方 `package_skill.py` 校验）。该 skill
是 `AGENTS.md` 的可执行伴侣 + 超集，覆盖 9 步工作法 + 14 条硬规则，可在 IDE 技能面板调用。

**未完成（待用户确认）**：
- `sqlite_store.py` 的修复尚未提交；
- `.codebuddy/skills` 与 `web/dist` 是否随仓库提交待定（`.env` 等密钥绝不入库）；
- `optimize_jobs` 表（PRD 有模型、代码未建，见 `AGENTS.md` 已知问题 #1）仍缺。

---

### 8.50 A1：补齐 optimize_jobs 表迁移（v9 → v10，2026-11-03）

**背景**：`AGENTS.md` 已知问题 #1 —— 详设 `MS4-C §10.4` 早已给出 `optimize_jobs` 完整
DDL，但 `_MIGRATIONS` 里只有「留到各自切片再加」的占位注释，表从未被创建；`feature-inventory`
甚至误记成「`optimize_jobs` 已有表」。这导致 M4-13 的进度上报（dashboard 重启后仍可查
「跑到哪了」）缺少落库基础。

**改动（仅 state 层，符合迁移硬规则 #2）**：
- `src/quant/state/sqlite_store.py::_MIGRATIONS` 末尾追加第 9 段（`v9 → v10`），用
  `CREATE TABLE IF NOT EXISTS optimize_jobs (...)` 照 spec §10.4 原样建表；`IF NOT EXISTS`
  天然幂等，不改任何历史 DDL，`SCHEMA_VERSION` 自动变 10。
- `tests/test_sqlite_store.py` 新增 `test_v9_db_gains_optimize_jobs_table`：模拟 v9 存量库
  升级到 v10，断言 `optimize_jobs` 表与 10 个列存在、老数据不丢、`user_version == SCHEMA_VERSION`。

**验证**：`ruff check` 全过；`pytest tests/test_sqlite_store.py` **72 passed**（原 71 + 1），
零回归；`read_lints` 零告警。

**范围边界**：A1 只建表、不参与任何运行逻辑。真正接通 M4-13 进度上报（把 `cli/optimize.py`
改登记式任务、写 `optimize_jobs.completed` + Redis 轮询、前端进度条）属独立功能 **A2**，
不在本次范围，见 `feature-inventory` §3.4 / 后续优先级第 4 条。

### 8.51 npm 依赖审计评估（B-安全，2026-11-03）

**背景**：`npm audit` 报 7 条（4 moderate / 1 high / 2 critical），集中在
vite / esbuild / vitest / echarts / tinypool。原计划走 B-安全（`npm audit fix` 非 force）
清掉 critical。

**实测结论**：`npm audit fix`（非 force）只把 `@vitest/*` 子包从 2.1.8→2.1.9 蹭了一小步，
**0 条告警被清除**，tinypool critical 仍在；npm 现要求 `--force`（升 vitest→4.1.12 等大版本
breaking）才修。即"非破坏修复 critical"在实际解析里不成立。

**决策（选项 B）**：回退 `web/package-lock.json` 的这次无意义改动，**保持现状**。理由：7 条
全为开发/测试/构建期依赖，对生产零暴露——tinypool 仅活在 vitest 测试 runner；esbuild/vite
是 dev-server 类，生产看板静态托管 `web/dist` 不跑 vite dev server；echarts XSS 仅渲染不可信
数据时触发（图表均为自家回测结果）。本地单用户面板，无实际威胁，不影响任何功能。

**后续**：若日后要彻底清零，只能走 B-全量（`npm audit fix --force`，breaking 升
vite5→6 / echarts5→6 / vitest 大版本），需 `build + typecheck + vitest` 全量回归 + 重建
`web/dist`，另行排期，不在本次范围。

---

## 9. 相关文档

- [PRD v0.6](requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md)（[v0.5 历史版](requirements/2026-10-17-quant-trading-platform-requirements.md)）
- [MS1 详设](specs/2026-10-17-ms1-data-foundation-design.md)
- [MS2 详设](specs/2026-10-17-ms2-backtest-webhook-design.md)
- [MS4-A 详设：告警与首页](specs/2026-10-30-ms4a-alerts-overview-design.md)
- [MS4-B 详设：风控与执行](specs/2026-10-30-ms4b-risk-execution-design.md)
- [MS4-C 详设：回测与策略](specs/2026-10-30-ms4c-backtest-strategy-design.md)
- [模拟盘运行手册](reference/RUNBOOK_PAPER_TRADING.md)
- [M3 模拟盘测试报告](tests/2026-10-24-m3-paper-trading-test-report.md)
- [项目结构说明](PROJECT_STRUCTURE.md)
