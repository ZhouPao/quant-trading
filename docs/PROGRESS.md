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

### 8.52 A2 · P1：optimize_jobs 落库方法（2026-11-03）

**范围**：A2（M4-13 寻优进度上报）分 5 期，本期为 P1——只给 state 层加 `optimize_jobs`
的写/查方法；表由 A1（v10 迁移）建立，本期不碰任何运行逻辑与前后端契约。

**改动（仅 state 层，符合迁移/不变量纪律）**：
- `src/quant/state/sqlite_store.py` 新增 4 个方法，全部复用既有 `_execute`/`_query_one`
  与 `self._lock` 线程安全约定，`created_at` 由调用方传 epoch 毫秒（与 `create_backtest_run` 一致）：
  - `save_optimize_job`：`INSERT`，`grid` 以 `json.dumps` 落库；`job_id` 唯一，重复登记按主键
    约束抛 `StateError`；状态白名单 `{running,done,failed,canceled}`，非法抛 `ValueError`。
  - `update_optimize_progress`：单调更新（`completed < ?`），回退进度不覆盖已记录最大值；
    任务不存在返回 `False`。
  - `finish_optimize_job`：置终态 + `finished_at`；不存在返回 `False`；同样校验状态。
  - `get_optimize_job`：按 `job_id` 查单行，供 P3 的 `GET /api/optimize/{job_id}` 复用。
- `tests/test_sqlite_store.py` 新增 `TestOptimizeJobs`（6 例）：落库+读回（含 grid JSON 往返）、
  进度单调、结束、缺失返回 False、重复 job_id 抛 `StateError`、非法 status 抛 `ValueError`。

**验证**：`ruff check` 全过；`pytest tests/test_sqlite_store.py` **78 passed**（原 72 + 6），
零回归；`read_lints` 零告警。

**未完成（留待后续期）**：P2 后端登记式任务 + 进度落库、P3 进度查询 API、P4 前端进度条、
P5（可选）Redis 实时轮询。M4-13 在看板的可见进度仍待 P4 完成才算闭环。

### 8.53 A2 · P2：后端登记式寻优 + 进度落库（2026-11-03）

**范围**：A2 第二期。把 dashboard 的 optimize 从“同步跑完返回排名”改为“登记式任务”——收请求先插
`optimize_jobs(status=running, total=N)`，用 `asyncio.to_thread` 跑 `GridOptimizer.search`，`progress`
回调每 10 组合/2s 节流写 `optimize_jobs.completed`（SQLite，跨重启可查），结束置 `done/failed` +
`finished_at`。第一版只落 SQLite，不碰 Redis（P5 可选）。

**改动**：
- `src/quant/dashboard/backtest.py`：
  - 保留原 `optimize`（同步，供现有 TestOptimize 与内部复用）；**新增** `start_optimize`：入参校验后
    `save_optimize_job` 登记、`asyncio.create_task` 后台跑、`_run_optimize_job` 在 worker 线程跑搜索并把
    progress 落库、结束 `_finish_optimize` 置终态；结果暂存 `self._optimize_results`（内存，待 P3 取回），
    错误入 `self._optimize_errors`。
  - `_optimize_sync` 增加可选 `progress` 参数并透传给 `GridOptimizer.search`（cli 版早已这么传，dashboard 版之前漏了）。
  - 复用既有 `self._lock = asyncio.Semaphore(1)`：后台任务也走同一把锁，与 backtest 任务互斥，
    **保持单消费者、不并发打满 CPU**，未改动 `tasks.py`。
- `src/quant/dashboard/app.py`：`POST /api/optimize` 切到 `start_optimize`，立即返回 `{job_id, status, total, ...}`（登记式）。
- `tests/test_dashboard_backtest.py`：新增 `TestOptimizeJob`（注册+完成+进度写满、非法 metric 不登记、大网格进度写满 3 例）；
  `test_optimize_endpoint` 改为断言新契约（返回 job_id、后台跑完进度落库）。

**关键决策 / 边界**：
- `run_id` 用 `job_id` 占位（optimize 无对应 backtest_run）；确认 SQLite 未启用 `PRAGMA foreign_keys`，占位不触发外键报错。
- 结果暂存内存而非落库：P2 保持最小改动；结果持久化 + 重启恢复（`fail_interrupted_optimize_jobs`）留待后续期。
- 取消按钮（M4-13 字面含“取消”）未做——按你“先只做进度展示”的范围，取消属 P4 可选或单独排期。

**验证**：`ruff check` 全过；`pytest` 127（test_dashboard_backtest）+ 26（test_backtest_batch）passed，零回归；`read_lints` 零告警。

**未完成（后续期）**：P3 进度查询 API（`GET /api/optimize/{job_id}`，返回进度 + 内存结果）；P4 前端进度条 +
轮询（落库轮询即可，无需 Redis）；结果持久化 / optimize_jobs 重启恢复；（可选 P5）Redis 实时轮询。
M4-13 在看板可见进度仍待 P4 闭环。

### 8.54 A2 · P3：寻优进度查询 API（2026-11-03）

**范围**：A2 第三期。新增 `GET /api/optimize/{job_id}`，把 P2 暂存内存的寻优结果/进度
通过 HTTP 取回，闭合“提交→轮询进度→取回结果”的后端链路；同步 `web/src/api.ts` 契约
（纪律 #4：前端契约先定，UI 消费留 P4）。

**改动**：
- `src/quant/dashboard/backtest.py`：新增 `get_optimize_job(job_id)`——读 store 行，把落库的
  `grid` JSON 文本解析回对象；`done` 时附 `_optimize_results` 完整结果、`failed` 时附
  `_optimize_errors` 错误信息；无 store / 不存在返回 None。
- `src/quant/dashboard/app.py`：新增 `GET /api/optimize/{job_id}`，不存在返 404。
- `web/src/api.ts`：新增 `OptimizeJobResponse` 类型（`result: OptimizeResponse | null` 直接复用
  旧返回体）与 `getOptimizeJob(signal, jobId)`（走 `getJson`，带 `signal`，jobId 做
  `encodeURIComponent`）。未改 `App.tsx`，UI 消费留 P4。

**验证**：`ruff check` 全过；`pytest` 新增 `TestOptimizeJob.test_get_optimize_job_shape`（直接走
service 断言 grid 解析 + result/error 形态）+ `test_optimize_job_get`（HTTP POST→轮询→GET 断言
返回体 + 404），连同 P1/P2 套件全绿；`read_lints` 零告警；`api.ts` 经 `npm run typecheck` 通过。

**未完成（后续期）**：P4 前端进度条 + 1s 轮询消费本接口、把 `runOptimize` 从同步改轮询；
P5（可选）Redis 实时轮询。M4-13 在看板可见进度仍待 P4 闭环。

### 8.55 A2 · P4：前端进度条 + 轮询，M4-13 闭环（2026-11-03）

**范围**：A2 第四期、最后一期。把前端寻优从“同步等结果”改为“提交 → 1s 轮询 → 渲染”，
在看板显示实时进度，闭环 M4-13“寻优进度上报”。

**改动（完全复用既有模式）**：
- `web/src/api.ts`：`runOptimize` 改为 `submitOptimize`——返回 `{job_id,status,total}` 信封，
  与 backtest 的 `submitBacktest` 完全同构（P3 的 `getOptimizeJob` 不动）。
- `web/src/App.tsx`：
  - 新增 `followOptimizeJob(jobId)`——照抄既有 `followRun` 的轮询模式（复用 `followAbort`
    做取消、`isTerminalStatus` 判终态、1s `setTimeout` 间隔），终态 done 时把 `job.result`
    写入 `optimize` 状态、finally 清进度。
  - optimize 分支：`submitOptimize` 拿 job_id → `setNotice` → `await followOptimizeJob(job_id)`。
  - 新增 `optimizeProgress` 状态与 `OptimizeProgress` 组件（`completed/total` + 百分比 +
    状态文案），复用 `RUN_STATUS_TEXT` 与 `.dim` 样式。
- `web/src/styles.css`：新增 `.opt-progress*` 进度条样式，复用 `var(--accent)/--line/--panel-2`。

**验证**：`tsc --noEmit -p tsconfig.json` 通过；`npm run build`（`tsc -b && vite build`）成功
（590 模块，1.32s，产出 dist）；`npm test`（vitest）**13 passed**；后端未改动故 pytest 不受影响。
`web/dist/` 被 `.gitignore:44` 忽略，**构建产物不入仓**。

**闭环说明**：M4-13 至此在看板可见实时进度，状态由 🟡 转 🟢。仍可增强（未做）：取消按钮、
结果持久化与重启恢复（`fail_interrupted_optimize_jobs`）、（可选 P5）Redis 实时轮询。

### 8.56 A2 收尾：P1–P4 跨期联调测试（2026-11-03）

**目的**：分期单元测试各自通过后，补齐“各期联手”的端到端联调，防止契约漂移与边界缺陷遗留。

**新增测试**：
- `tests/test_optimize_integration.py`（后端，真实 SQLite + 真实 ASGI HTTP）：
  - `test_submit_poll_fetch_contract`：POST 登记 → 轮询 GET → 取回结果，逐字段校验 P4 的
    `OptimizeJobResponse` 契约（job_id/status/total/completed/metric/n_jobs/grid 解析/result/error）；
  - `test_large_grid_full_chain`：3×3=9 组合，total/completed/grid/top 全链校验；
  - `test_unknown_job_returns_404` / `test_invalid_metric_rejected_without_row`（400 且不登记僵尸任务）；
  - `test_result_lost_after_restart`：锁定“P2 内存结果 + P3 GET 在进程重启后优雅降级为 result=null”的已知边界。
- `web/src/api/optimize.api.test.ts`（前端，mock fetch 契约测试）：校验 `submitOptimize`（POST + job_id 信封）
  与 `getOptimizeJob`（GET + encodeURIComponent + 全覆盖 `OptimizeJobResponse`，含 failed/error 分支）。

**验证**：`pytest` **134 passed**（含 5 新用例）；`npm test`（vitest）**16 passed**（含 3 新用例）；
`ruff` / `tsc --noEmit` 零告警；`npm run build`（tsc -b + vite build）590 模块通过。

**说明**：浏览器内 UI 渲染（进度条实际显示）未做 e2e（无浏览器 harness），但数据层 / P4 契约已从
前后端两侧闭环；如需真浏览器 e2e 需另搭 Playwright 等。

### 8.57 首页总览缺陷审计与第一批修复（2026-11-03）

**背景**：对「首页总览」（MS4-A）做前后端逐功能审计，发现 P0（会崩溃 / 误导）5 条、
P1（口径不一致）若干、P2/P3（性能与体验）若干。本次只修第一批。

**本批修复**：
- **P0-1** `fetchTodayPnl` 无降级兜底 → 补 `instruments` / `degraded` 空值（渲染期不再崩）。
- **P0-2** `fetchTimeline` / `fetchFreshness` 只补 `items` 不补 `degraded` → 一并补上。
- **P0-3** 持仓数读库失败被显示成 0（看起来像空仓）→ 后端失败时写 `degraded:["sqlite:positions"]`，
  前端 `positions_count` 为 null 时显示「—」（仅在持仓表确有数据时才兜底计数）。
- **P0-4** `Promise.all` 一个失败拖垮整页 → 订单 / 成交 / 盈亏（仅「交易执行」页使用）改为
  单点 `.catch` 降级为空数据并标 `degraded`；核心四项仍整体 reject（保留 abort 语义）。
- **P0-5** `Sparkline` 单点时 `step=Infinity` 产生 NaN 路径 → 少于 2 点直接不画（与 `Curve` 同口径）。
- **P1-6** 订单 pill 只列 filled/pending/canceled，漏掉「已报 / 部分成交」→ 改为按后端返回的
  全状态渲染（`ORDER_STATUS_ORDER` 定序 + 中文名），不再漏计数。
- **P1-7** 过期阈值 180s 前端硬编码 → 后端 `overview.thresholds.snapshot_stale_seconds` 下发
  （源自 `risk.snapshot_max_age_ms`），前端只在缺失时兜底 180。
- **P1-9** 保证金率无任何告警着色 → 按后端下发的 `margin_ratio_threshold` 着色（warn / bad）。

**顺带发现并修复的遗留缺陷**：`tests/test_dead_letters.py` 断言 `SCHEMA_VERSION == 9`，
A1 加了 v10（`optimize_jobs`）迁移后应为 10。**此前只按子集跑 pytest 一直没发现**，
本次改为跑全量后暴露——已修正断言并注明迁移来源。

**验证**：`pytest` **全量 1663 passed**；`npx tsc --noEmit` 通过；`npm test`（vitest）
**18 passed**（新增 `dashboard.api.test.ts` 守护 `fetchAll` 降级语义）；`npm run build` 通过；
本次改动文件 `ruff` 全过（`tests/test_open_interest_fetcher.py` 的 6 处 W293 为仓库预存，未触碰）。

**未做（后续批次）**：P1 余项（`auth_required` 缺失、`reason` 空串非 null、时区口径、
订单状态英文混排）；P2（Redis 单 try 包 8 处读、无 `socket_timeout`、串行无 pipeline、
冗余请求、组件 memo 化）；P3（aria-live 噪音、表格行键盘不可达、新鲜度文案误导、
其余阈值继续改为后端下发）。

### 8.58 首页总览第二批修复：P1 余项 + 后端可靠性 / 性能（2026-11-03）

**P1（契约与口径）**：
- `kill_switch` 默认字典补 `auth_required`——Redis 全挂时该字段原本**整个缺失**（非 null），
  前端 `?? false` 会让急停面板隐藏 token 输入。
- `KillSwitch.reason()` 空原因统一返回 `None` 而非 `""`（已同步 `test_kill_switch.py` 断言）。
- 时间显示统一按 **UTC+8**：`clock` / `stamp` 原本用浏览器本地时区，而后端日切
  （timeline `since`、today-pnl `day_start`）是 UTC+8，非 UTC+8 环境下「今日」边界错位。
- 订单状态标签改中文（`StatusTag` 之前直接渲染英文 status，与全中文 UI 不一致）。

**P2（后端可靠性 / 性能）**：
- Redis 读取由「一个大 try 包 8 次读」改为 **`asyncio.gather` 并发 + 按源独立降级**
  （标记 `redis:snapshot / worker / monitor / queue / kill`，同一来源去重）。原先快照读失败会
  **连带放弃**心跳 / 队列 / 急停，且只留一个笼统 `"redis"` 标记、定位不到子源。
- 看板 Redis 连接补 `socket_connect_timeout=2.0` / `socket_timeout=5.0`：原先未设超时，
  Redis 挂起会让 `/api/overview` **永久阻塞**，连降级都走不到。
- `fetchAll` 新增 `includeExecution` 参数：订单 / 成交 / 盈亏只在「交易执行」页使用，
  其它页直接返回空壳，**每轮轮询省掉 3 个请求**（overview 页 11 → 8）。

**契约变更（前后端已同步）**：`degraded` 中 Redis 相关标记由 `"redis"` 细化为
`"redis:{源名}"`；`kill_switch.reason` 空值语义由 `""` 改为 `null`。

**验证**：`pytest` **全量 1663 passed**；`npx tsc --noEmit` 通过；`npm test`（vitest）
**18 passed**；`npm run build` 通过；改动文件 `ruff` 全过。

**未做（第三批）**：P2 余项（跨端点重复读同一快照的短期缓存、组件 `React.memo`、
alerts 每轮拉 200 条、页面不可见时暂停轮询 / 失败退避）；P3（aria-live 噪音、
表格行键盘不可达、新鲜度文案限定单组合、其余阈值继续改为后端下发）。

### 8.59 首页总览「今日盈亏」展示重构（2026-11-03）

**问题（用户反馈「展示有点乱」）**：根因是**算术看起来对不上**——后端口径 `realized` 已扣手续费、
`pnl = realized + unrealized`，但 UI 把「手续费」当独立正数并排展示，用户按
`已实现 + 未实现 − 手续费` 计算得不到「总盈亏」，自然觉得乱。叠加三个次要问题：数字列
**左对齐**且正数无 `+` 号，正负混排难扫读；`degraded:["sqlite"]` 时 `instruments` 为空，
被显示成「今日暂无交易」（把「读不到」说成「没交易」）。

**改动（纯前端）**：
- `api.ts` 新增 `signed()`：正数显式加 `+`（负数 / 零 / 空值保持原样），复用既有 `num()`。
- `App.tsx` 今日盈亏卡：
  - 标签改为「已实现(净)」，并补口径说明「已实现已扣手续费 · 总盈亏 = 已实现 + 未实现」；
  - 手续费列弱化（`.dim`），避免与盈亏列抢视线、也不诱导二次扣减；
  - 所有金额改用 `signed()`；新增降级分支：`degraded` 非空且无明细时显示「数据不可用（来源）」。
- `styles.css`：`.today-pnl` 作用域内数字列右对齐 + `tabular-nums`（不触碰共享 `Table` 组件）。
- 新增 `web/src/api/format.test.ts` 锁住 `num` / `signed` 的正负号与千分位行为。

**验证**：`npx tsc --noEmit` 通过；`npm test` **20 passed**；`npm run build` 通过。

**后续补充**：`today_pnl` 新增 `total_turnover` 字段（成功与降级分支都带），前端汇总行加
「成交额」并用 `compactAmount` 缩写（≥1M 用 M、≥1k 用 k、否则两位小数）；`.today-pnl .kv`
扩为 5 列。`format.test.ts` 补 `compactAmount` 用例。

### 8.60 首页总览第三批：P2 尾巴 5 项（2026-11-03）

**P2-15 TOCTOU**：`KillSwitch` 新增 `status()`——单次 GET 同时返回 `(是否激活, 原因)`，
overview 不再 EXISTS+GET 两次往返（中间标志若被切换会产生 active=true 但 reason=None
的撕裂状态）。`is_active()` / `reason()` 保留给 worker / engine / CLI 原有调用。

**P2-17 快照缺字段静默**：快照在但缺 `ts` / `account`（monitor 恒写这两个字段）→
`degraded` 增加 `snapshot:malformed`，不再静默显示成 —。

**P2-18 时钟漂移被吞**：快照时间在未来（> now+5s 容差）→ `degraded` 增加 `clock-skew`
并 log.warning；年龄仍按 0 显示，但「数据很新」的假象不再掩盖时钟不可信。

**P2-16 串行读残留**：
- overview：订单计数与持仓数两次独立 SQLite 读改 `asyncio.gather` 并发（各自独立降级）。
- today_pnl：三路读取（今日成交聚合 / 持仓 / Redis 快照）并发化；持仓读失败原先静默
  返回 `[]`（浮动盈亏显示成 0、看起来没风险），现在显式降级 `sqlite:positions`。

**P2-20 alerts 每 5s 拉 200 条**：
- 后端 `alert_stats` 新增 `unread` / `by_level` / `total_all`（全量、不带时间窗，
  open/silenced 算未读即 M1-9 口径），规则计数与未读汇总两条查询并发。
- 前端 `fetchAll` 增加 `includeAlerts`：告警明细只在「告警日志」页拉取，
  总览页 fetchAll 从 4 个请求降到 3 个。
- 摘要卡 / ALERTS chip 改用后端聚合；`refresh()`（写操作后）与切页会立即重拉统计。
- 顺带修正 P3-24 的语义错位：chip note 原来写「共 N 条」但显示的是未读数。

**验证**：`pytest` 全量 **1666 passed**（+3：status 单读、malformed、clock-skew）；
`npx tsc --noEmit` 通过；`npm test` **22 passed**（+1 includeAlerts 契约）；
`npm run build` 通过；改动文件 `ruff` 全过。

**未做**：P2-16 的 ETag / HTTP 缓存（本地看板收益小，暂不做）；P3 全部 8 项（体验/可访问性）。

### 8.61 M2「市场监控」存量审计 + 批次一修复（P0×2）（2026-11-03）

**背景**：转入存量审计——对已完成的九大模块（除首页总览）逐一复核前后端，从 M2 开始。
三个只读子代理并行深查（后端数据管道 / 看板 API / 前端图表），覆盖 M2 全部 14 个已实现功能，
分级汇总：P0×2、P1×12、P2×14、P3×12（均核对到 file:line）。本批先修 2 个 P0。

**P0-1 `fetch-open-interest` 一调即崩**：CLI 用了不存在的 `settings.data.data_dir`
（正确为 `settings.storage.data_dir`），且 `--config` 参数被无视（写死 `config/config.toml`）。
修复：改用 `settings.storage.data_dir`、`Settings.load(args.config)`、失败返回 1 并确保
client 关闭；补 CLI 回归测试（此前 0 覆盖）。

**P0-2 指标设置坏结构 → 图表页整页白屏**：`mergeSettings` 只补「缺失字段」不校验「已存在
字段的类型」，localStorage 旧版本/手改坏结构（如 `periods: 5`、`maPeriods: null`）会让
渲染期 `.map` 抛 TypeError，且 `main.tsx` 无 ErrorBoundary → React 整树卸载。
修复：`mergeSettings` 逐字段按默认值类型校验（数组过滤非法元素、标量类型不符回落默认、
整组非对象整组回落）；新增全局 `ErrorBoundary`，渲染异常落可读兜底页而非白屏。

**验证**：`pytest` 全量 **1668 passed**（+2 CLI 回归）；`npx tsc --noEmit` 通过；
`npm test` **23 passed**（+1 坏结构用例）；`npm run build` 通过；改动文件 `ruff` 全过。

**顺带**：更新 `AGENTS.md` 已知问题 #1——`optimize_jobs` 表已由 v10 迁移闭环，原条目过时。

**M2 审计遗留（后续批次，待确认后继续）**：
- 批次二（P1 静默丢数据族）：断点只向前、向早回补静默失效；`_validate_and_fill` 吞
  `errors`（失败伪装成功）；validator 前导缺口盲区（年文件损坏+断点残留永不重补）；
  quality 缺口 key 位移导致重复累积
- 批次三（P1 契约/显示族）：quality API 异常分支形状（前端 TypeError）；dataZoom 被
  spread 覆盖（副图不跟随缩放）；图表时区改 UTC+8；WS「重连倒计时」为静态快照；
  candles 缺 `confirm`；SWAP vol 口径（张数误当基础币，潜伏）
- 批次四/五（P2）：限频器惩罚后整桶回满（含 `test_okx` 固化断言修正）+ 持锁 sleep；
  K 线翻页停滞保护；parquet 原子写 + mtime 缓存（1m 年文件 52 万行每 5s 全量解码）；
  WS 应用层 ping / 订阅被拒重试；`inst` 路径穿越校验；前端轮询 seq 守卫 / NaN 防御 /
  质量明细缓存失效 / 输入 debounce

### 8.62 M2 批次二：K 线管道「静默失败」族修复（P1×4）（2026-11-03）

四项同属「失败了却装作成功 / 丢了数据不报告」，全部修在 M2-1/M2-7/M2-15 管道：

**P1-2 断点只向前，向早回补静默失效**：断点只记录「已落盘的最新 ts」，请求起点早于
断点时 `cursor = ckpt.last_ts + bar_ms` 会把早于断点的 bar 全部过滤、validator 又只查
相邻缺口 → 静默返回「净增 0」，必须手删 checkpoint 才能补早期历史。修复：`fetch` 在
主循环后显式回补 `[start_ts, min(ckpt.last_ts - bar, end_ts)]`（与已有数据重叠部分由
`save_bars` 按 ts 去重，幂等；`sync` 每轮自行回退断点，不受影响）。

**P1-3 整段拉空时错误被吞**：`_validate_and_fill` 只看 `missing` 不看 `errors`，
"empty bars" 只留在 info 级 summary 里，失败伪装成「净增 0」。修复：`report.errors`
逐条提到 **WARNING** 级（进 monitor 日志告警体系）。

**P1-4 validator 前导缺口盲区**：只查相邻 bar 之间的缺口，「年文件被删/损坏 + 断点
残留」导致的前缀丢失完全不可见。修复：`DataValidator.validate` 新增可选
`expected_start_ts`，首根之前的缺口计入 missing（`prev_ts` 为起点前一根虚拟 bar，
补拉区间算术与相邻缺口完全一致）；`CandleFetcher` 传入请求起点。quality 的滚动窗口
起点不是真实数据起点（新上市品种会误报），**有意不传**，已在 docstring 说明。

**P1-5 quality 缺口 key 位移**：`merge_report` 按 `from` 精确匹配新旧缺口，缺口左端
补上一根后 `from` 右移 → 旧 open 永远挂着 + 新增一条，重复累积、`gaps_open` 虚高。
修复：改**区间相交**配对——相交即同一个洞（区间/根数以本次校验为准）；跨出窗口的
缺口无重叠时保持原样（窗口外无法判定，不误标 repaired）；一个旧洞中段被填、两端仍缺
时两段都保留。

**测试**：新增 8 个回归用例（回补两条、前导缺口两条、errors 暴露一条、quality 配对
三条）；更新 2 个受新行为影响的既有用例——`test_resume_skip_already_downloaded` 的
前提正是被修的缺陷（改为断言回补生效 + 主循环确实被跳过）、
`test_rewinds_when_checkpoint_ahead` 因前导缺口补拉多一次 API 调用（改断言首次调用）。

**验证**：`pytest` 全量 **1676 passed**；改动文件 `ruff` 全过。纯后端，前端无改动。

**M2 审计遗留**：批次三（P1 契约/显示族：quality API 异常形状、dataZoom、图表时区、
WS 倒计时、candles confirm、SWAP vol 口径）→ 批次四/五（P2 可靠性与性能）。

### 8.63 M2 批次三：契约/口径/显示族 P1×6（2026-11-03）

**P1-1 quality API 形状**（前后端）：detail 模式异常分支丢 `report` 键、参数不齐返回
200+error 第三种形状（与 list/detail 都不匹配）→ 前端 `QualityDetail` 解引用 TypeError。
修复：service 异常分支按模式带 `report: null`；参数不齐上移路由层 **422**；
`fetchQuality`/`fetchQualityReport` 补空值兜底 + inst/bar URL 编码。

**P1-2 dataZoom 被 spread 覆盖**（前端）：`...zoomOption` 的第二次 spread 把带
`xAxisIndex`/滑块样式的配置整个覆盖 → 缩放只作用于主图、VOL/MACD/KDJ 副图不跟随、
滑块样式丢失，且每次 5s 轮询重绘都复现。修复：start/end 合并进单一 dataZoom 配置，
删除 `zoomOption`；新增测试断言副图网格被绑定。

**P1-3 图表时区改 UTC+8**（前端）：横轴/tooltip 原用浏览器本地时区，与全站
`stamp`/后端日切对不上。修复：`formatTime` 用共享的 `TZ_OFFSET_MS`（api.ts 导出，
单一来源），`getHours→getUTCHours`；新增测试。

**P1-4 WS「重连倒计时」改真倒计时**（前后端）：原先显示退避总时长的静态快照
（30s 退避会连续多轮显示同一个"30s"）。修复：`WSClient` 记录下次重连墙钟时刻
`next_retry_at`（断线时设置、连上清空，`next_delay` 保留兼容），`_ws_status` 白名单
放行，前端按本地时钟倒数；worker 在线但 WS 心跳过期时显示「状态未知」而非整行消失。

**P1-5 candles 下发 `confirm`**（前后端）：前端无法区分未收盘 bar。修复：响应带
`confirm`（0=未收盘/1=已收盘），`CandleRow` 类型同步；图表页脚对未收盘最后一根
显示「最后 1 根未收盘，数值还会变」。

**P1-6 SWAP/FUTURES 成交量口径**（后端）：OKX 合约 idx5 `vol` 单位是**张**，
idx6 `volCcy` 才是基础币量——原先统一取 idx5，一旦同步合约成交量即差 ctVal 倍。
修复：`from_okx_raw` 按 inst 后缀区分（`-SWAP`/`-FUTURES` 取 idx6），现货不变；
新增 SWAP 解析测试。

**测试**：+7 新用例（quality 422/降级形状、WS retry、SWAP 口径、formatTime、
dataZoom 绑定、candles confirm）；更新 1 个旧断言（`test_quality.py` 参数不齐的
200+error 行为已上移路由层）。**验证**：`pytest` 全量 **1682 passed**；
`tsc --noEmit` 通过；`npm test` **25 passed**；`npm run build` 通过；`ruff` 全过。

**过程说明**：本批曾误加一个占位空测试（`pass`），发现后立即删除——如实记录，不掩盖。
**M2 遗留**：批次四/五（P2 可靠性与性能）。

### 8.64 M2 批次四：可靠性加固 P2（2026-11-03）

**先纠错（审计自查）**：批次三报告里的 **P2-6「限频器惩罚后整桶回满 → 循环 50011」经
重新推演不成立**——惩罚 2s 结束后按组速率补充的令牌量 = 2s × refill_per_sec，恰好等于
OKX 的 2s 窗口配额，持续速率仍 ≤ 限额（public：20 张/2s = 10/s 合规）。行为未改，
仅在 `penalize()` docstring 记录该结论，防止后人再误判。

**P2-7 acquire 持锁 sleep（真缺陷，已修）**：等待期间持有桶锁，同组的
`acquire/penalize/tokens` 全部被队头阻塞——50011 的惩罚要等当前等待者睡醒（最长
timeout=10s）才生效。修复：等待计算在锁内、**sleep 移到锁外**循环重试；新增多线程
回归测试证明 `penalize` 不再被慢速等待者阻塞。

**K 线翻页停滞保护**：OKX 异常重复返回同一页时 `oldest` 不前进，主循环与补拉循环
都会无限打转（资金费率侧有 stall 检测，K 线侧没有）。修复：两处循环加「页首不前进
即 break + WARNING」；新增满页重复返回的回归测试（旧实现会挂死）。

**parquet / JSON 原子写**：`BarStorage` 直写目标路径（读方可读到半截文件），
`InstrumentStorage` 直写 JSON（写一半崩溃 → 下次 load 抛 JSONDecodeError 无降级）。
修复：统一 tmp + rename；`BarStorage` docstring 说明并发语义（单写者现状下安全，
多写者需文件锁）。新增测试：save 后无 `.tmp` 残留、merge 路径数据完整。

**路径穿越防护**：`inst_id`/`bar_size`（可来自 HTTP 查询参数）被直接拼进文件路径。
修复：`BarStorage` 新增字符白名单校验（`[A-Za-z0-9-]`/`[A-Za-z0-9]`），`_path_for`/
`list_years`/`list_bar_sizes`/`quality.report_path` 全部前置校验，非法抛 ValueError →
被 service 的异常兜底捕获进 `degraded`，不会 500。新增 `tests/test_storage_hardening.py`。

**WS 应用层心跳**：协议层 `ping_interval=20` 只保 websocket 帧；OKX 网关要求空闲约
30s 内发**文本** "ping"，冷门品种长时间无行情会被周期性踢线、反复重连。修复：连接
期间并行任务每 25s 发 "ping"（间隔可配），`pong` 文本静默处理不误报 invalid message；
新增回归测试。

**验证**：`pytest` 全量 **1691 passed**（+9）；改动文件 `ruff` 全过。纯后端，前端无改动。

**遗留（批次五 + 有意不做）**：
- 批次五（性能/前端）：parquet mtime 缓存（1m 年文件每 5s 全量解码）、前端轮询
  seq 守卫、NaN 防御、质量明细缓存失效、输入 debounce。
- **订阅被拒重试：有意不做**——订阅参数本身非法时盲目重试会造成无限重连循环，
  需要区分「参数错」（告警人工介入）与「瞬态错」（重试）的语义，当前 error 级日志
  已可触发告警；留待有真实案例再设计。

### 8.65 M2 批次五：性能/前端收尾 P2（2026-11-03）

**parquet 谓词下推（替代 mtime 缓存方案）**：`/api/candles` 每 5s 拉尾部 ~300 根，
原先每次 `read_parquet` 全量解码年文件（1m ≈ 52 万行/64 组）。方案取舍：原计划的
mtime 缓存需缓存整年 DataFrame（对象字符串列 ≈ 数百 MB/年），内存代价不可接受；
改为 `read_parquet(filters=[ts 区间])` **row-group 谓词下推**——只解码命中的尾部
分组，零内存代价、无失效一致性问题，结果与全量读一致（filters 精确到行）。
`report_path` 类的全量扫描读不受影响。

**前端四项**：
- **toBars NaN 防御**：一根脏 bar（NaN/Inf）沿 sma 累加污染整段指标链、ECharts
  静默断线 → `toBars` 剔除非有限值的 bar（fills/撑压按 ts 定位，不受影响）；
  新增测试。
- **K 线轮询 seq 守卫**：5s 轮询共享 controller，慢响应后到覆盖新数据 →
  请求序号守卫，非最新丢弃（含 error 分支）。
- **质量明细缓存失效**：detail 缓存原先永不失效，缺口补齐后展开行仍显示旧 OPEN →
  缓存带列表 `ts`，列表每轮刷新后旧明细作废重新拉取。
- **指标输入防抖**：原先每敲一个有效字符都提交 → 整图重算 + localStorage 写。
  `NumberInput`/`ListInput` 改 300ms 防抖 + 失焦立即提交；顺带修掉「非法输入后
  显示值与实际值脱节」——失焦回弹到当前生效值。

**验证**：`pytest` 全量 **1691 passed**（下推路径由既有 roundtrip 用例覆盖）；
`tsc --noEmit` 通过；`npm test` **26 passed**（+1 toBars）；`npm run build` 通过；
`ruff` 全过。UI 交互项（seq 守卫/防抖/缓存失效）无浏览器 harness，靠类型检查 +
代码审查兜底，与 8.56 的 e2e 边界一致。

**M2 审计至此全部收尾**（批次一~五）：P0×2、P1×12、P2×13 已修；P2-6 复核后不成立
（8.64 记录）；P3 体验项与「订阅被拒重试」按范围有意保留，明细见各批次「遗留」。

### 8.66 M3「策略研究」审计 + 批次一修复（2026-11-04）

**审计范围**：M3 全部 15 个 ✅ 功能 + M3-5 🟡 已做部分（M3-7/M3-13 ⬜ 跳过）。
两个只读子代理并行深查前后端，结论：无 P0；**P1×5 / P2×7 / P3×9**，正面确认
两层参数合并处处同一函数、bindings 键口径一致、`strategy_param_changes` 只追加、
Decimal 硬规则合规、一键回测后端确用当前 DB 参数。前端 M3 此前零测试覆盖。

**批次一修复**：
- **P1-1 回测请求参数注入**：`resolve_strategy` 的 overrides 原先对未声明参数
  `raw` 原样直通（任意键/类型混淆/NaN 都能改回测假设）。定性核实：`commission_rate`
  是 toml 里的真实参数，请求覆盖承载合法的费率敏感性研究，**不能一刀切禁用**。
  修复：未声明参数必须①键存在于 toml ②与默认值同类型 ③有限值（`coerce_undeclared`）；
  零费率研究仍可用且结果 `params` 可追溯，但任意键/NaN/类型混淆被拒。
- **P1-2 compare 校验补全**：M3-18 要求「同品种+同周期+同区间」原先只实现前两项 →
  补 `start_ts/end_ts` 一致性；`status != done` 直接拒绝（failed 无可比指标）；
  `ids` 上限 8（`COMPARE_MAX_RUNS`）。
- **P2 coerce 加固**：float/decimal 拒绝 NaN/Inf、int 拒绝非整数浮点（原先 JSON 3.7
  静默截断、字符串报错，口径不一）；`_check_range` 的 `InvalidOperation` 兜底为
  `ParamError`（原先漏到 HTTP 500）。
- **P2 回滚与审计原子性**：`rollback_param` 增加可选 `validate` 回调（store 不反向
  依赖策略模块），服务层按当前 `PARAM_SPEC` 校验恢复值——范围事后收紧时宁可拒绝
  也不写「显示 db 实际用 toml」的脏覆盖；异常分支补 `message` 键（路由 400 文案）。
  `set_strategy_param` 读旧值移入锁内，消除并发 PUT 的重复审计记录。
- **P1-3 前端兜底**：`fetchStrategies/fetchParamChanges/fetchStrategyEvaluations/
  fetchBindings/fetchBacktestRuns` 五个接口补 `?? []`（原先缺字段会让策略页
  `undefined.length` 崩进 ErrorBoundary）；新增 `strategy.api.test.ts` 契约测试。

**过程如实记录**：本批一度把 `set_strategy_param` 的读旧值包进 `self._lock`，
但 `_query_one` 自身也取这把**非重入锁** → 自锁，全量 pytest 挂住、叠加多个后台
测试进程。已改为「持锁状态下直接用连接查询」并清理挂起进程，修复后全量回归通过。
这类问题只有坚持跑全量才会暴露。

**验证**：`pytest` 全量 **1705 passed**（+14，含此前零覆盖的 rollback HTTP 端点）；
`tsc --noEmit` 通过；`npm test` **28 passed**（+2）；`npm run build` 通过；`ruff` 全过。

**批次二（待做）**：加载失败≠参数只读的文案、PUT 失败反馈挪到表单旁、保存后
`info` 合并而非整体覆盖、**DELETE 绑定路由（解绑入口，功能缺口）**、notice 重拉去重、
回测页对比同区间提示。批次三：`_set_params` 类型比较、评估刷屏截断、glob 收窄、
reason 上限、前端纯函数测试。**待用户拍板**：操作人恒为 `"owner"`（本地无用户体系，
建议保持现状并在界面注明「本地单用户」）。

### 8.67 M3 批次二：前端口径族 + 解绑功能缺口（2026-11-04）

**M3-4 解绑入口（功能缺口，本次补齐）**：后端提示「同一组合改周期请先解绑」，
`delete_binding()` 也有实现，但**没有 DELETE 路由也没有任何调用方**——绑定一旦
建立 bar 永远改不了（只能停用）。补齐：
- `DELETE /api/strategies/{strategy}/bindings/{inst}`（带写守卫）→
  `BacktestService.delete_binding` 删库 + 重建 Redis 缓存；
- 前端 `deleteBinding()`（`postWrite` 方法联合类型加 `DELETE`），启停矩阵每行
  加「解绑」按钮（二次确认），页脚文案改为指向该按钮；
- 测试：解绑→缓存清除→可用新周期重新绑定、写守卫、前端 DELETE 契约。

**P1 前端口径三件**（`EnginePanel`）：
- 加载失败不再被误报成「该策略未声明 PARAM_SPEC，参数只读」：新增 `infoError`
  状态，失败文案改为「策略参数加载失败」。
- PUT 失败反馈从「变更历史」面板（离表单两个 section）挪到**参数表单旁**：
  新增独立 `saveError`，回滚错误仍用原 `error`，互不串扰。
- 保存成功后 `setInfo` 从整体覆盖改为**合并**（`param_spec`/`sources`/`editable`
  是可选字段，缺失时保留旧值），不再把编辑面板打回只读。

**P2 重复请求**：四个轮询 effect 原先依赖 `notice`（展示文案），「没有变化，未写入」
也会触发重拉，回滚成功还手动再拉一遍 → 改为独立 `refreshKey`，只有真正写入成功
（保存 / 回滚）才递增一次，去掉手动 `fetchParamChanges` 双请求。

**P1 对比提示**：`CompareTable` 计算所选 run 的区间集合，混合区间时显示
「后端会拒绝对比」提示；说明文案补上「区间」（与 8.66 的后端校验呼应）。

**验证**：`pytest` 全量 **1707 passed**（+2）；`tsc --noEmit` 通过；`npm test`
**29 passed**（+1）；`npm run build` 通过；`ruff` / IDE 诊断全过。

**遗留（批次三）**：`_set_params` 跨类型相等判断、评估历史被 `waiting_data` 刷屏、
`_scan_strategies` glob 收窄、reason 长度上限、前端 M3 纯函数测试
（`sortCompareVersions`/`bestValue`/`formatParams`）、操作人 `"owner"` 口径待拍板。

### 8.68 M3 批次三：审计收尾（2026-11-04）

**先复核再动手，两处结论与旧审计不同（如实记录）**：
- **`glob("*.toml")` 误收任意 toml —— 不成立**：`config/strategies/` 里的模板是
  `.toml.example` 后缀，根本不匹配 `*.toml`；且 `load_strategy_class` 对缺
  `strategy.class` 的文件抛 `StrategyConfigError`，`_scan_strategies` 已
  warning + skip。最多是日志噪音，**不改**。
- **`_scan_strategies` 每请求全量重扫（P2 性能）**：目录只有 2 个有效 toml、
  `importlib` 有模块缓存，收益可忽略；没有性能证据就不做缓存（避免为改而改），**缓做**。
- **操作人恒为 `"owner"`**：本地无用户体系下的现实值，属产品口径决策，
  用户未拍板，**本批不动**（前端 `updateStrategyParams` 与后端默认值一致）。

**本批修复 4 项**：
- **P2 `_set_params` 跨类型相等误判**：toml 里的 `float 0.1` 与 `coerce_value`
  返回的 `Decimal("0.1")` 直接比较为 **False**（已实证），提交与生效值相同的
  decimal 参数会被误判成变更、白写 DB 覆盖 + 审计记录、并把 `sources` 从
  toml 改成 db。修复：比较前把当前生效值按同一 `PARAM_SPEC` 归一。
- **P2 评估历史被低信息状态刷屏**：engine 每轮写定长列表（10 轮），数据落后时
  连续 `waiting_data`、正常运行时高频 `duplicate` 会把有信号的轮次全挤出去。
  修复：engine 原始历史保留 10 → **50 轮**（数据完整），看板扫描全部轮次后
  **按 `(inst, status, reason)` 折叠连续重复**、只留状态变化点取前 10 条；
  UI 文案同步改为「最近 N 个评估状态（连续重复轮次已折叠）」。
- **P2 审计字段无长度上限**：`ParamUpdateRequest.reason/by`、
  `RollbackRequest.reason/changed_by` 加 `max_length`（200/64），超长直接 422。
- **P1 参数版本签名键序敏感 + 前端 M3 零测试**：`formatParams` 按
  `Object.entries` 顺序拼串，同一组参数来自不同合并路径（toml / DB / 请求覆盖）
  时键序可能不同 → 对比表把同一版本算成两个、名次被稀释。修复：**键排序后拼接**。
  顺带把 `bestValue` / `formatParams` / `sortCompareVersions` 及 `CompareSort`
  抽到 `web/src/compare.ts`（纯函数、可单测），App.tsx 改为引用。

**验证**：`pytest` 全量 **1711 passed**（+4：跨类型相等、超长字段 422、折叠×2）；
`npx tsc --noEmit` 通过；`npm test` **33 passed**（+4 `compare.test.ts`）；
`npm run build` 通过；`ruff` 全过。

**M3 审计收尾总账**：P0 无；P1×5 全修（批次一 3 + 批次二 2）；P2×7 修 6、
1 项（全量重扫）复核后判定收益可忽略而缓做；P3 修 3（评估刷屏、reason 上限、
语义文案），余项（对比上限静默忽略、compare 单 id 报错文案、equity 降采样尾点
等）按范围有意保留。M3「策略研究」模块存量审计到此结束。

### 8.69 M3 第二轮复核 · 批次甲：P0 止损（2026-11-04）

**背景**：对 M3 全部 18 项做第二轮全量复核（含 ⬜ 项与三批修复验收），发现上一轮
「M3 审计收尾」过早：留下一处 **P0 半成品**，且「两层合并处处同一函数」「五回调
实盘/回测共用」两条正面结论被推翻。本批先做不依赖任何口径决策的止损项。

**P0 全量解绑被 seed 复活（上批新增 DELETE 解绑后暴露）**：
- 机理：`write_cache` 空列表只 `delete(key)` → `read_cache` 空 hash 返回 `None`（判成
  「缓存丢了」）→ 默认 `bindings_fail_mode="db"` 回落 SQLite → `ensure_seed` 见表空
  即补种 `enabled=True` 初始绑定；`halt` 模式则直接触发 kill switch。且 `_bindings_sync`
  **每次看板读矩阵也调 `ensure_seed`**，复活不需要等引擎。
- 修复（两处合修，缺一不可）：
  ① `bindings.write_cache` 空绑定写哨兵 field `__empty__`、`read_cache` 识别后返回 `[]`
     而非 `None` —— 区分「用户主动清空」与「缓存丢失」；
  ② SQLite 新增 `app_meta` 表（v11 幂等迁移）与 `get_meta/set_meta`，`ensure_seed`
     改为**仅在从未初始化时种一次**；存量库已有绑定时只补标记、不动数据。
- 测试（6 条）：清空后不复活、空缓存≠缓存丢失、缓存丢失回落仍不复活、halt 模式
  允许有意空绑定、HTTP 层 DELETE 后 GET 不再种回、equity 尾点。

**P1 equity 降采样丢尾点**：`tasks.equity` 的 `iloc[::step]` 止于倒数第二点
（N=100/limit=60 → 丢 index 99），曲线终点与 `final_equity` 不符 → 显式补尾点。

**复核纠错（本批有意不做）**：子代理报「实盘漏调 `on_trade`」，亲自核实后判定
**不改** —— `live.py:275` 每轮 `strategy_cls()` 重建实例，`SrExitMixin.protective_exit`
已用「持仓均价重新定档」自适应（`sr_exit.py:108-115`），`on_trade` 清冻结价位在实盘
无跨轮意义；强行补反而会改变同轮多 bar 回放行为，属于为改而改。记为生命周期
一致性讨论，非 bug。

**验证**：`pytest` 全量 **1717 passed**（+6）；改动文件 `ruff` 全过。纯后端。

**后续**：批次丁落地两个已拍板的口径决策（CLI 共用函数不共用数据源 / Python 为
唯一基准）；其余复核发现（Pine 门禁三态、CLI 校验、错误详情解析、AbortError 竞态等）
按批次乙/丙顺序处理。

### 8.70 M3 批次丁：两个口径决策落地（2026-11-04）

**这是设计决策落地，不是 bug 修复**，两条都已由用户拍板。

**决策① CLI 回测参数：共用函数，不共用数据源**
- 设计文档 MS4-C §3.2 要求「回测与实盘共用 `resolve_params`」，但 CLI 三条路径
  （`backtest`/`walkforward`/`optimize`）一直是 `load_strategy_class` +
  `parse_params` 原样合并，无注释说明是有意设计 —— 属改造遗漏。
- 落地：新增 `cli/_params.py::resolve_cli_params`，三条 CLI 统一走
  `resolve_strategy`，自动获得类型/范围/未知键/NaN 校验；**默认 `store=None`
  （纯 toml 基线）**，保证同一条命令结果可复现、寻优网格不被在线值污染；
  新增 `--use-db-params` 显式开关用于「验证当前实盘参数」，并尊重全局回滚
  开关 `params.resolve_from_db=false`。看板 M3-16 一键回测本就读 DB 覆盖。
- **行为变化（需知悉）**：`--params` 里「toml 中不存在的键」现在会被拒绝
  （与看板路径一致）；需要覆盖这类参数时，正确做法是在 toml 里声明该键。
- 测试 `tests/test_cli_params.py`（8 例）：默认忽略 DB 覆盖、开关读取、全局
  开关优先、显式参数优先且被 coerce、费率敏感性仍可用、越界/NaN/注入键被拒。

**决策② 以 Python 为唯一基准（修订一条原始设计设定）**
- 事实：`pine_compare.py` 原写「Pine 为主回测，Python 副本用于交叉验证」，
  但实盘执行主体是 Python，且 Python 已扩展 EMA/ATR 过滤、`SrExitMixin`、
  手续费预留（Pine 未复刻）。改 Python 去追 Pine 会**直接改变实盘信号**并污染
  既有审计/回测历史；改 Pine 零运行风险。
- 落地：
  - 文档修订：`sma_cross.py` 文件头、`pine/README.md`、`pine_compare.py`
    docstring 统一改为「**Python + 真实 toml 是唯一生产基准，Pine 是只验证
    最基础逻辑的基线策略副本，不镜像生产参数/逻辑**」；两个 `.pine` 文件头
    声明基线定位与已知差异。
  - **门禁三态**：`BacktestService.pine_gate()` 返回
    `passed / blocked / not_covered`，`GET /bindings` 带出该字段，前端启停矩阵
    面板在 `not_covered` 时显式提示「当前参数集未做 Pine 交叉验证，偏离门禁
    未覆盖，启用不会被 5% 检查拦截」——消除 `pine_block` fail-open 的虚假安全感
    （`pine_block` 本身仍按原语义不拦，没做过对比不等于对比不通过）。
    查询异常降级为 `not_covered` 并告警，不拖垮矩阵接口。
  - **静态一致性登记**：新增
    `tests/strategies/test_pine_python_consistency.py`，把 Pine ↔ toml 的
    参数名/默认值差异、以及 `PARAM_SPEC` 默认值 ↔ toml 默认值的差异
    （`slow_len 50 vs 100`、`ema_filter false vs true`）显式登记，
    任何新增漂移都会让测试失败；同时要求每个 `.pine` 文件头声明基线定位。

**验证**：`pytest` 全量 **1733 passed**（+16）；`npx tsc --noEmit` 通过；
`npm test` **33 passed**；`npm run build` 通过；`ruff` 全过。

**M3 第二轮复核剩余（批次乙/丙，按优先级）**：P1 `paramGroups` 按 `kind`
过滤、两套最优口径统一（`extreme` vs `bestValue`）、`formatParams` 嵌套键排序、
`getJson`/`postWrite` 错误详情解析（compare 400 中文原因被吞）；P2 三处
`.catch` 未过滤 `AbortError`、共享 controller 无序号、compare 重复 id、
回滚 `git_commit` 恒 NULL、`history_scan` 硬编码与历史键无 TTL、
`bindings` 路由参数校验、缓存重建串行化；P3 注释/文档漂移与 a11y。

### 8.71 M3 批次乙：三批半成品收口（2026-11-04）

对第二轮复核发现的前端/契约尾巴逐项收口，共 8 项。

**P1 错误详情被前端吞掉（根因级）**：`getJson` 完全不解析 `detail`、`postWrite`
只认字符串 detail。Pydantic 422 的 `detail` 是数组，一律显示成「HTTP 422」——
用户看不到「哪个字段超长」，**直接抵消批次一所有校验的可读性**。修复：抽出
`errorMessage()` 统一解析（字符串原样透出；数组按 `loc` 拼成 `字段: 原因`；
无 detail 回退状态码），`postWrite`/`getJson`/`postJson`/`deleteGuarded`/急停
五处重复块全部接入；新增两种形态的契约测试。

**P2 AbortError 竞态**：变更历史/最近回测/绑定列表三处 `.catch` 未过滤
`AbortError`——cleanup abort 触发的 reject 会执行 `setX([])`，若新 effect 的
请求先返回，旧 catch 后到会把列表覆盖成空（表格闪空）。修复：三处统一先判
`AbortError` 再清状态。

**P1 两套最优口径**：`EnginePanel` 内联 `extreme()`（单值也标最优、`higherBetter`
硬编码）与回测页 `bestValue`（<2 个有效值不标、按 `LOWER_IS_BETTER` 判断）并存，
同一指标两处高亮规则不一致。修复：详情页改用共享 `bestValue`，删除 `extreme`。

**P1 参数版本对比混入非单次回测**：`paramGroups` 只判 `status === "done"`，
walk-forward/batch 的 `params` 是 `base+grid` 结构、指标口径不同，混进同组排名
会得出错误结论。修复：`run.kind !== "backtest"` 直接跳过。

**P2 嵌套参数签名仍不稳定**：`formatParams` 上一批只排顶层键，`base`/`grid`
嵌套对象仍按 `JSON.stringify` 原序——键序不同即生成不同签名、制造假版本。
修复：递归稳定序列化（对象键排序、数组保序）+ 测试。

**P2 评估历史键永不过期**：解绑/改绑后该键不再被读取，Redis 里永久残留。
修复：写入后 `expire` 7 天（`EVAL_HISTORY_TTL_SECONDS`）；`service.py` 的
`history_scan = 50` 硬编码改为引用 `EVAL_HISTORY_LIMIT`（单一来源）。

**P2 死代码（复核后删除而非保留）**：`CompareTable` 的「所选回测区间不一致」
提示在批次一后端补上区间校验后**永不可达**（不一致直接 400，组件不渲染）。
删除该分支，说明文案改为指向真正可见的 `compareError`。

**验证**：`pytest` 全量 **1733 passed**（TTL 断言并入既有引擎用例）；
`npx tsc --noEmit` 通过；`npm test` **36 passed**（+3：字符串 detail、
Pydantic 数组 detail、嵌套键序）；`npm run build` 通过；`ruff` 全过。

**批次丙遗留**：诊断/体验小项——评估列表 `degraded` 未展示、合并区间注释漂移、
`total ?? 0` 在旧后端显示「共 0 条，显示最近 20 条」、回滚 `git_commit` 恒 NULL、
回滚 TOCTOU、bindings 路由参数校验、缓存重建串行化、compare 重复 id 去重；
以及待拍板的操作人 `"owner"` 口径。

### 8.72 M3 批次丙：P2/P3 尾巴收尾（2026-11-04）

**P2 回滚审计缺代码版本**：`service.rollback_param` 未传 `git_commit`，回滚行恒为
NULL——审计链里「这次回滚发生在哪版代码上」不可追溯。修复：抽出公共
`foundation/gitinfo.py::git_commit()`（原先只有 `backtest.py` 的私有 `_git_commit`，
无法复用），参数变更与回滚共用；补测试（monkeypatch 固定 commit）。

**P2 bindings 参数无格式校验**：`inst_id` 直接拼进缓存 field `{strategy}:{inst}`，
含冒号会让 `partition(":")` 解析整体错乱。修复：`_validate_inst_id` 白名单
（`[A-Za-z0-9-]{1,64}`），新增/解绑两条路径前置校验 → 400；补测试。

**P2 绑定缓存重建无串行化**：两个写操作各自「读全量 → delete+hset」会互相覆盖，
后完成的可能把对方改动回退。修复：`_binding_lock`（独立于回测信号量），
`set_binding`/`delete_binding` 的「写库+重建」整体串行。

**P2 compare 重复 id**：`ids` 含重复 run_id 不去重，返回重复条目、污染最优标注。
修复：`dict.fromkeys` 先去重再校验数量；补测试（`["a","a"]` → 400）。

**P2 回测历史 total 误导**：`total ?? 0` 在旧后端显示「共 0 条，显示最近 20 条」。
修复：回退到本页条数。

**P3 评估降级未展示**：`fetchStrategyEvaluations` 的 `degraded` 被丢弃，读库/Redis
失败时页面显示「引擎尚未发布评估历史」——把「读不到」说成「没有」。修复：保留
`degraded` 并在列表上方提示「可能不完整」。

**P3 注释漂移**：两处仍写「notice 变化会触发重新拉取」，实际是 `refreshKey`
（批次二改的），已同步。

**复核后判定不改（记录理由）**：`store.rollback_param` 的读记录/校验/写入分两把锁
（TOCTOU）——并发回滚同一 id 时第二次会因「值已相同」返回 False，不会产生坏数据；
强行把校验回调纳入持锁范围会重蹈 8.66 的死锁风险，且校验依赖策略模块（可能较慢），
收益与风险不匹配。保留现状并在 docstring 说明。

**验证**：`pytest` 全量 **1736 passed**（+3）；`npx tsc --noEmit` 通过；
`npm test` **36 passed**；`npm run build` 通过；`ruff` 全过。

**M3「策略研究」至此全部收尾**：审计（P1×5 / P2×7 / P3×9）+ 第一轮三批
（8.66/8.67/8.68）+ 第二轮复核（8.69 P0 止损、8.70 口径决策、8.71 半成品收口、
8.72 尾巴）全部完成。唯一未决：操作人恒为 `"owner"`（本地无用户体系，属产品
口径，待拍板）。

### 8.73 M4「回测」审计 + 批次甲：P0 止损（2026-11-04）

**审计范围**：M4 全部 16 项（14 ✅ 含 3 条前端缺口 + M4-8 🟡 + M4-13 🟢）。前端/后端
两路只读子代理并行深查，结论：后端 **P0×4 / P1×约20**，前端 **2 处契约漂移即白屏 +
P1×8**。本批先修 4 个「结论不可信」级 P0。

**P0-1 `quant optimize --metric calmar|sortino` 直接 TypeError**：`cli/optimize.py`
在 `GridOptimizer.search` 已按 `_rank` 排好序之后**又按原始值排一次**；这两个指标
在「无回撤 / 无负收益日」时为 `None`，`None < float` 直接崩。修复：删除二次排序
（`_ASCENDING` 一并移除），统一由 `_rank` 处理 None。

**P0-2 walk-forward 重叠时复合收益虚高**：`build_segments` 明确允许
`step_bars < oos_bars`（OOS 重叠），而 `compounded_return` 直接连乘各段
`total_return` → 同一段行情被算多次。修复：新增 `overlapping_oos`，重叠时
`compounded_return` 返回 **None**（调用方显示「—」），不产出错数；CLI 输出同步
「—（OOS 段重叠，不可复合）」。**没有改成禁止重叠**——重叠滚动是合法用法，
只是复合收益不适用。

**P0-3 资金费率部分覆盖时静默按 0 计**：原先只校验「文件不存在」，数据只覆盖
一半时缺失段成本恒 0、系统性低估收益。修复：`load_funding_schedule` 新增
`start_ts/end_ts`，校验首条 ≤ 起点、末条 + 8h ≥ 终点（缺则报错并给出回补命令）；
**四条路径全部透传**（CLI 单次/寻优/walk-forward、看板单次/寻优/walk-forward/批量）。

**P0-4 Pine 裸百分数导致门禁永久 blocked**：TradingView 导出的 `max_drawdown,8.32`
被当小数 `8.32`（内部应为 `0.0832`）→ 偏离 100 倍。修复：比例类指标
（`total_return`/`max_drawdown`）不带 `%` 且 `|值|>1` 时**拒绝导入并提示两种正确写法**
（`8.32%` 或 `0.0832`），不静默猜。

**同批修的关联项**：
- **门禁同源校验**：带请求级参数覆盖的回测与「当前生效参数集」不同源，导入后
  门禁永远匹配不到（静默 fail-open）→ `import_pine` 现在直接拒绝并提示重跑。
- **门禁跨品种/周期串味**：`worst_pine_deviation` 增加可选 `inst_id/bar` 过滤，
  `pine_block` 按当前启用的组合查询——BTC 1H 的坏导入不再阻止 ETH 4H。
- **分母为 0 的说明**：仍按 100% 处理（安全优先），但导入响应新增 `notes`
  说明原因，避免用户误以为数值算错。
- **复核新发现（子代理未报）**：`cli/walkforward.py` **根本没传 `funding`**，
  与看板路径在「是否计入资金费率」上口径不同（违反 AGENTS #4）→ 已补齐。

**验证**：`pytest` 全量 **1747 passed**（+11：Pine 4、walk-forward 2、费率 2、
CLI 1、门禁 2）；`ruff` 全过。纯后端，前端无改动。

**过程如实记录**：`overlapping_oos` 初次写成 `item.oos_start`，但区间在
`SegmentResult.segment` 子对象上——4 个既有测试立刻抓到，已修正为
`item.segment.oos_start`。另有一处新测试的 fake 数据缺 `slow_len` 导致 KeyError，
属测试数据问题，已补全。

**M4 遗留**：批次乙（引擎/成交正确性：`fill_at` 区间校验、取消首根 IndexError、
限价单挂单语义、cash 穿负、金额 Decimal、turnover 落库、`_metrics_payload` inf、
批量 `should_stop`、compare 校验 kind、rerun 参数、持久化事务）；批次丙（日报
`resolve_strategy` + funding + 判定维度、前端 kind 分流、`LOWER_IS_BETTER` 补集合、
寻优高亮、成交方向、筛选契约、两处白屏兜底）；批次丁（P2/P3 + 两条未做功能
M4-4 导出、M4-6 walk-forward 入口）。

### 8.74 M4 批次乙：引擎/成交正确性（2026-11-04）

**P1 取消在首根 bar → IndexError → 状态变 failed + 误告警**：`equity_curve` 为空时
`_run_payload` 取 `[-1]` 抛错，异常落到 `except Exception` 绕过 canceled 判定。
修复：空曲线回退 `initial_cash`，取消由 `should_stop` 正常判定为 canceled。

**P1 保护性出场价不校验 bar 区间**：策略返回本根从未触及的价格也会「成交」（任意价
后门，策略 bug 会静默放大收益）。修复：`low <= exit_price <= high` 前置校验，越界
抛 ValueError（fail loud，不夹价掩盖 bug）。

**P1 限价单未穿透即丢弃（不是挂单）**：只给一根 bar 的机会，限价策略被系统性低估。
修复：改为 **GTC 挂单**——未穿透保留到下一根继续尝试；新信号替换旧挂单时记入
`unfilled`（不静默丢）。**这是行为变更**：限价策略的回测成交数会变多（修正方向）。

**P1 金额被转 float**（AGENTS 硬规则 #1）：`max_consecutive_loss_amount`、
`funding_cost` 原先 `float()` 入库会变成 `0.10000000000000001`。修复：保持
`str(Decimal)` 文本；连带修 `runner.format_metric` 兼容文本金额（我改完 Decimal 后
CLI 立刻因 `f"{'0':.2f}"` 崩，全量测试抓到，已一并修复）。

**P1 `turnover` 静默丢弃**（硬规则 #3 的坑）：`metrics.py` 早就算出该指标、前端
`METRIC_ORDER`/`METRIC_HINTS` 也早已注册，但表里没有列、`_METRIC_COLUMNS` 也没
——`save_backtest_metrics` 静默 return。修复：v12 迁移补 `turnover TEXT` 列 +
白名单；新增 roundtrip 测试。**前端无需改动**。

**P1 `_metrics_payload` 遇 inf 会 500**：`float("inf")` 直接进 `JSONResponse`
（`allow_nan=False`）→ `/runs`、`/compare`、`/runs/{id}` 全挂。修复：读取时
`math.isfinite` 判定，非有限值置 `None`（前端统一渲染「—」）。

**P1 批量取消粒度不符**：`BatchSweep` 只在格与格之间检查 `should_stop`，一格 10 万根
时取消要等很久。修复：透传到 `Backtest.run`（每根 bar 检查）。**连带语义修正**：
格内取消时 `run` 只回放了部分 bar，该格结果必须丢弃；取消不再抛「全部失败」。
既有测试 `test_should_stop_ends_after_current_cell` 同步改为
`test_should_stop_aborts_within_cell_and_discards_partial`（断言 `ok == []`）。

**P1 compare 不校验 run 类型**：`backtest` 与 `walkforward`/`batch` 的 metrics 键
完全不同，同表排名会拿不可比指标当「最优」。修复：要求所有 run 的 `kind` 一致。

**复核纠错（不改）**：审计报「cash 穿负 → `max_drawdown` 算 0 → Calmar 失效」
**不成立**——`compute_drawdown_curve` 的 peak 从首点正权益起算，穿负后
`(peak - 负值)/peak > 1`，回撤只会更大；且回测允许杠杆是常见口径，加资金校验会
改变策略行为。仅记录，不改。

**验证**：`pytest` 全量 **1753 passed**（+6：GTC、出场越界、取消首根、compare
kind、inf 防护、turnover roundtrip）；`ruff` 全过。纯后端，前端无改动。

**批次乙遗留（放到下一批）**：`rerun` 参数类型漂移（需改请求契约）、4 张表写入
非事务、回测 `git_commit` 从不写入、`trades` 取最早 100 笔但文案写「最近」、
产物读取无异常兜底。

### 8.75 M4 批次丙：口径/前端族（2026-11-04）

**P1 日报回测与实盘参数不同源（M4-8）**：`report/daily.py` 用 `load_strategy` 只读
toml，看板改过参数后模拟盘用 DB 参数、日报回测用 toml → 偏离报告第一步就不同源、
**会误判 FAIL**。修复：改用 `resolve_strategy`（toml ← DB 覆盖），尊重全局
`params.resolve_from_db`；回测补传 `funding`（模拟盘真实成交含资金费），只校验
对比窗口的费率覆盖（早期历史成本不影响窗口内 fills 对比）。

**P1 详情视图不按 `run.kind` 分流**：渲染只看当前 `mode`——在批量模式点开单次回测会
渲染 `BatchResultView` 并报「所有格子都失败了」，反之指标全「—」。修复：按
`run.kind` 选视图（`batch` → 批量表，其余 → 单次详情），仅 `batch/backtest` 两个
模式展示详情，寻优模式不受影响。

**P1 对比最优集合不全**：`LOWER_IS_BETTER` 只有 `max_drawdown`，于是 `funding_cost`
（净支出）、`max_consecutive_losses`、`max_consecutive_loss_amount`、`turnover`
会**把最差值标绿**。修复：补齐集合；`avg_holding_bars` 加入 `NO_BEST`（无优劣）。

**P1 寻优结果每行都标「最优」**：无条件 `bt-best` 使高亮完全失效。修复：只第 1 名
加高亮，其余用 `mono`。

**P1 成交「最近 N 笔」取的是最早 N 笔**：`frame.iloc[offset:offset+limit]` 是正序
前段，与 UI 文案相反。修复：改尾部窗口（`offset` 从最新往旧偏移），返回仍按时间
正序。

**P2 筛选契约不符（M4-10）**：前端是自由文本框、后端等值匹配 → 输入部分策略名
必然 0 条且无提示。修复：`strategy_id`/`inst_id`/`bar` 改 **LIKE 模糊匹配**
（参数化，不拼字符串），状态/类型保持等值；前端补「类型筛选」下拉
（`backtest`/`walkforward`/`batch`），`BacktestRunsQuery` 加 `kind`。

**P1 两处白屏隐患**：`fetchBacktestRuns`/`fetchBacktestCompare` 逐条兜底
`metrics ?? {}`（对比表/最优标注直接解引用）；`PineComparePanel` 的
`rows/unmatched/persisted` 加 `?? []`，并展示后端 `notes`（分母为 0 的说明）。

**验证**：`pytest` 全量 **1758 passed**（+5：日报同源 2、成交窗口 1、筛选 2）；
`npx tsc --noEmit` 通过；`npm test` **37 passed**（+1 最优集合）；`npm run build`
通过；`ruff` 全过。

**批次丙遗留（放到批次丁）**：M4-10 时间区间筛选（后端已支持 since/until，前端
未接）、日报 PASS/FAIL 判定维度仍不含 PnL/持仓/成交价、`rerun` 参数类型漂移、
4 张表写入非事务、回测 `git_commit` 从不写入、产物读取异常兜底，以及两条未做
功能（M4-4 报告导出、M4-6 walk-forward 入口）。

### 8.76 M4 批次丁：可靠性/审计收尾（2026-11-04）

**P1 `rerun` 参数类型漂移**：原先把 `backtest_runs.params` 拼成 `k=v` 文本再
`parse_params` 猜类型——Decimal 变 float、字符串 `"10"` 变 int、值含 `;`/`=` 直接
崩，**重跑结果可能与原 run 不同源**。修复：`BacktestRequest.params`/
`BatchRequest.params` 支持 `str | dict`，rerun 直接传原 JSON 对象（`_overrides_of`
统一归一）；删除 `_params_text`。

**P1 回测完成态非事务**：指标 + 产物清单 + 状态三步各自 commit，中途失败会留下
「有产物无指标」或「有指标无 artifact」的中间态。修复：新增
`SQLiteStore.complete_backtest_run`，一个事务内写完三者，异常整体回滚；补成败
两条测试。

**P1 回测结果不可溯源**：`git_commit` 列有参数但从没写入。修复：单次/walk-forward/
批量三处登记都写 `git_commit()`（公共 `foundation/gitinfo.py`）。

**P2 产物损坏让接口 500**：`_read_parquet` 直接抛。修复：捕获后返回空表 + WARNING，
`equity/trades/segments/batches` 按「无数据」降级；补损坏文件测试。

**M4-10 时间区间筛选（前端补齐）**：后端早已支持 `since/until`，前端只有文本+
下拉。修复：`BacktestRunsQuery` 加 `since/until`（UTC ms），历史筛选区加两个
`type=date`，`dayStartMs/dayEndMs` 转 UTC 整天；非法/空值不传。

**验证**：`pytest` 全量 **1763 passed**（+5：rerun 类型保真、git_commit、事务成败、
产物降级）；`npx tsc --noEmit` 通过；`npm test` **37 passed**；`npm run build`
通过；`ruff` 全过。

**M4 审计收尾总账（批次甲~丁）**：P0×4（CLI 崩溃 / 复合收益虚高 / Pine 百分数 /
费率覆盖）全修；P1×约 20 全修；P2 修 10+。**复核后不改**：cash 穿负不影响回撤
（8.74 记录）、`_scan_strategies` 全量重扫收益可忽略（8.68 记录）。

**M4 仍待做（需另排期）**：
1. **M4-4 报告导出**：后端产物有 HTML，但没有下载路由，前端 `artifacts` 零消费
   ——属未做功能。
2. **M4-6 walk-forward 提交入口**：后端接口/任务/逐段表全就绪，前端零请求函数
   ——属未做功能，工作量较大（表单 + 轮询 + 逐段表）。
3. **M4-8 判定维度**：仍只比笔数/数量/名义/手续费/方向序列，未含 PnL/持仓/成交价；
   扩展需同时改 `FillSummary`/`DeviationReport`/CSV/前端日报表。

### 8.77 M4 三项功能补齐（M4-4 / M4-8 / M4-6）（2026-11-04）

**M4-4 回测报告导出**：后端产物早就有 HTML/CSV（`report`/`equity`/`trades`/
`segments`/`batch`），但既无下载路由、前端 `artifacts` 也零消费。补齐：
- `GET /api/backtest/{run_id}/artifacts/{kind}`：kind 走**白名单**
  （`ARTIFACT_KINDS`），路径只取该 run 在 `backtest_artifacts` 登记过且文件仍
  存在的记录（`tasks.artifact_path`）——不接受调用方拼路径，杜绝任意文件读取；
  `FileResponse` 以 `attachment` 下载。
- 前端详情页底部「产物下载」链接（按 kind 列出）。
- 测试：下载 HTML 200 + Content-Disposition、未知 kind 404。

**M4-8 日报判定维度**：原先只比笔数/数量/名义/手续费/方向序列——**两侧方向与
成交量都对、成交价差 10% 依然 PASS**。补齐两个 fills 可算的维度：
- `FillSummary` 加 `realized_pnl`（复用 `risk.pnl.round_trip_pnls` 的 FIFO 回合
  盈亏，与风控/监控同口径）；
- `DeviationReport` 加 `pnl_dev` / `avg_price_dev`，纳入 `max_deviation` 与
  PASS/FAIL；CLI 文本表、CSV（`pnl`/`avg_price` 两行）、前端日报表标签同步。
- **持仓维度未加**：日报侧没有 position 快照数据源，硬加只能得到假精度；
  需要时先让 monitor 落快照再扩展（已在案）。
- 测试：同笔数同方向但价差 10% → 必须 FAIL；完全一致 → 两个新维度为 0。

**M4-6 Walk-forward 前端入口**：后端接口/任务登记/逐段产物全就绪，前端零请求
函数、只有历史列表一个 `kind` 标签。补齐：
- `api.ts`：`submitWalkforward` / `fetchBacktestSegments` + 类型。
- 回测页加「Walk-forward」模式：网格 + 排序指标 + IS/OOS 根数 + 步长（留空 =
  OOS 根数）；提交后复用 `followRun` 轮询，`loadArtifacts` 按 kind 取
  `/segments`；详情页渲染逐段表（段号 / IS 区间 / OOS 区间 / 参数 / IS 收益 /
  OOS 收益 / OOS 夏普）。
- 模式切换、空态文案、历史列表均把 `walkforward` 纳入。
- **测试边界如实说明**：前端无组件 harness，本项靠 `tsc --noEmit` + `build` +
  代码审查兜底（与既有 UI 交互项同一口径）；后端接口此前已有测试。

**过程如实记录**：下载路由初版漏了 `import asyncio`，全量测试立刻报
`NameError`，已补。同一次全量里有 4 个 `test_manual_trade` 失败（账户快照过期
503）——**单独重跑 54 passed**，确认是「全量与前端 build 并行」造成的时序
flaky，与本批改动无关。

**验证**：`pytest` 全量 **1767 passed**（+4）；`npx tsc --noEmit` 通过；
`npm test` **37 passed**；`npm run build` 通过；`ruff` 全过。

### 8.78 MS5 收尾 0-1 / 1 组：账本纠偏 + 交易页筛选（2026-11-04）

**0-1 账本纠偏**：需求表滞后于代码，先同步再排期——
- M4 从 `14 ✅ + 2 🟡` 改为 **15 ✅ + 1 🟡**：M4-4 报告导出、M4-6 walk-forward
  前端入口已在前一批补齐；仅 M4-8「偏离未进告警体系」仍 🟡。
- M3 从 `15 ✅ + 1 🟡 + 2 ⬜` 改为 **15 ✅ + 2 🟡 + 1 ⬜**：M3-13 已有
  `tests/strategies/test_pine_python_consistency.py` 显式登记双副本漂移（测试级，
  无 CLI 一键校验），如实标 🟡。
- M8-12 更新为「筛选主体已交付，来源筛选待加列」。

**1 组 交易页筛选（M8-12，后端 + 前端）**：
- 存储层：`get_recent_orders` 支持 `inst_id`/`strategy_id`/`status`；
  `get_recent_trades` 支持 `inst_id`。列名硬编码、值全部参数化。
- API：`GET /api/orders?inst=&strategy=&status=`、`GET /api/fills?inst=`。
- 前端：交易页订单表加「品种/策略/状态」筛选栏、成交表加「品种」；300ms
  防抖 + `AbortError` 过滤；空筛选直接复用 overview 的 30 条，有筛选才独立
  请求（limit=100），避免交易页重复拉全量。
- 测试：存储层 4 条（品种/策略/状态、成交品种）。

**本批未做（如实记账）**：
- **来源筛选**需要 `orders.source` 列 —— `Order` 模型与 `orders` 表都没有该字段
  （M8-10 的「来源」目前只体现在日志/信号上下文，未持久化），加列需 v13 迁移 +
  所有下单路径写入，超出 M8-12 的「展示」范围，单列一项。
- **持仓表筛选**：`positions` 只有 `inst_id`，本地过滤即可，暂不加后端参数。

**验证**：`pytest` 全量 **1770 passed**（+4）；`npx tsc --noEmit` 通过；
`npm test` **37 passed**；`npm run build` 通过；`ruff` 全过。

**过程说明**：一次全量里 `TestProcesses::test_all_online` 断言 `6 == 7` 失败，
单独重跑 `TestProcesses` **8 passed** —— 该用例读真实进程状态，属环境性 flaky，
与本批改动无关。前端初版误用了不存在的 `ORDER_STATUS_TEXT`，`tsc` 立刻报错，
已改为实际的 `ORDER_STATUS_LABELS`。

**下一项**：按既定顺序进入 3-1 `quant backup`（SQLite 在线备份 + config 快照 +
sha256 + manifest）。

### 8.79 MS5 收尾 3-1：`quant backup`（2026-11-04）

**新增 `src/quant/foundation/backup.py` + `quant backup`**（M10-7 第一项）：
- **在线备份**：用 `sqlite3.Connection.backup` 而不是复制文件——WAL 模式下
  worker 正在写入也能拿到一致快照，直接 copy 可能复制到半个事务、恢复后库损坏。
- **产物结构**：`<data_dir>/backups/YYYYMMDD-HHMMSS/` 下 `quant.db` +
  `<config>.toml`（存在才复制）+ `manifest.json`（created_at、schema_version、
  每个文件的 size/sha256）。
- **schema 版本**：直接读 `PRAGMA user_version`，不 import `SCHEMA_VERSION`，
  避免 `foundation → state` 的循环依赖。
- **只快照显式指定的配置文件**，不递归目录——避免把密钥/凭据一并打包。
- CLI：`quant backup [--out DIR] [--config config/config.toml]`，打印每个文件的
  大小与 sha256 前缀。

**测试**：`tests/test_backup.py` 4 条——在线快照可独立打开且数据完整、显式配置
被快照、源库缺失报 `BackupError`、manifest 的 schema_version 与备份库一致。

**验证**：`pytest` 全量 **1775 passed**（+5）；`ruff` 全过。

**下一项**：3-2 `quant restore`（dry-run 校验、拒绝覆盖运行中的库、恢复前自动备份）。

### 8.80 MS5 收尾 3-2：`quant restore`（2026-11-04）

**新增 `restore_backup` + `quant restore`**（M10-7 第二项）：
- **恢复前校验链**：`manifest.json` 必须存在；每个文件的 size 与 sha256 必须与
  清单一致（篡改/半截复制直接拒绝，不会把坏库写回）。
- **版本检查**：备份的 `PRAGMA user_version` **高于当前代码**时拒绝——降级恢复
  会丢掉新 schema 的列，宁可让人先升级代码。
- **运行中保护**：目标库 `-wal` 非空时拒绝（说明仍有进程在写），提示先停
  dashboard/worker；确认可覆盖时用 `--force`。
- **恢复前自动备份**：目标库存在时先 `create_backup` 到
  `<db>/backups/pre-restore/<stamp>/`，恢复出错还能退回。
- **在线回写**：`sqlite3.Connection.backup` 把备份库写回目标库，不直接覆盖文件。
- **`--dry-run`**：只跑校验链与版本检查，不写任何文件。
- CLI：`quant restore --backup DIR [--dry-run] [--force] [--config ...]`，
  恢复后提示重启 worker/dashboard 让连接指向新库。

**测试**：`tests/test_backup.py` 新增 5 条——往返恢复（且先自动备份）、dry-run
不改文件、篡改备份被拒、WAL 非空需 `--force`、清单缺失被拒。

**验证**：`pytest` 全量 **1780 passed**（+5）；`ruff` 全过。

**下一项**：3-3 `quant doctor`（端口/Redis/DB/schema/凭证/数据新鲜度/磁盘自检，
`--json` 输出）。

### 8.81 MS5 收尾 3-3：`quant doctor`（2026-11-04）

**新增 `quant doctor`**（M10-7 第三项），逐项自检并给可读结论：
- **SQLite**：文件存在、`PRAGMA quick_check`、schema 版本与代码对比
  （低于 → warn「下次启动自动迁移」；高于 → fail「先升级代码」）。
- **Redis**：`PING`（2s 超时），URL 里的密码**脱敏**为 `://***@host`。
- **OKX 凭证**：三个字段是否齐全，只报「缺哪个」不显示值；`demo=false` 标 warn。
- **SMTP**：`host`/`to_addrs` 缺失 → warn（告警会降级为仅日志）。
- **数据新鲜度**：读主组合（`engine.inst/bar`）最近 7 天 K 线，最后一根
  > 48h → warn、> 7d → fail。
- **磁盘**：可用比例 < 10% → warn、< 5% → fail。
- **产物目录**：`mkdir` + 写探针文件验证可写。
- 退出码：有 `fail` 返回 1；`--strict` 时 `warn` 也返回 1；`--json` 输出机器
  可读结构（不含密钥）；`--skip-redis` 供离线自检。
- 配置本身加载失败也输出 `[FAIL] config ...` 而不是抛 traceback。

**测试**：`tests/test_doctor.py` 7 条——健康报告、缺库 fail、缺凭证 fail、
数据 3 天未更新 warn、无本地 K 线 warn、实盘模式 warn、SMTP 未配置 warn。

**验证**：`pytest` 全量 **1787 passed**（+7）；`ruff` 全过。

**下一项**：3-4 `M10-6 进程启停`（Redis 命令通道 + 审计；按拍板默认只允许
`demo=true` 的进程启停，实盘需额外开关）。

### 8.82 MS5 收尾 3-4a：supervisor 启停命令通道（2026-11-04）

**M10-6 的后端可测部分**（API 与前端按钮留给 3-4b）：
- **命令队列**：看板 `LPUSH quant:control:supervisor`，supervisor 在每秒轮询
  开头 `LPOP` 消费（`_drain_commands`）。命令格式
  `{command_id, action: stop|start, name, operator, issued_at}`。
- **期望状态**：`_Child.desired`（默认 True）。`stop` → `terminate()` +
  `desired=False`；进程退出后**保持停止**，不再进入自动重启；`start` →
  `desired=True` 并立即拉起。
- **不破坏自愈**：未被 stop 的进程崩溃仍走原有指数退避重启（有专门用例）。
- **审计**：每条命令写 `quant:control:audit`（LPUSH + LTRIM 1000），含
  `command_id/action/name/operator/issued_at/status/error/ts`；未知进程/动作
  记 `rejected`，坏 JSON 只告警、不中断轮询。
- **进程表**：`quant:supervisor` 的每个子进程新增 `desired` 字段，看板可区分
  「用户主动停止」与「异常离线」。
- **安全边界**：本批只做了通道与执行；**入口（API）还没开**，因此当前想停进程
  只能手工 LPUSH。3-4b 会加 `POST /api/processes/{name}/stop|start`，并按拍板
  默认只允许 `okx.demo=true` 时使用。

**测试**：在原 `tests/test_supervisor.py` 的 18 条基础上**追加 6 条**命令通道用例
（stop 终止且不再重启、start 拉起、未知进程/动作 rejected、坏命令不崩、
正常崩溃仍重启），共 24 条。

**验证**：`pytest` 全量 **1793 passed**；`ruff` 全过。

**过程如实记录（一次真实的失误与修正）**：本批首次提交时，我用 `write_to_file`
直接覆盖了已存在的 `tests/test_supervisor.py`（**未先 `read_file`**），把原有
18 条测试替换成了自己新写的 11 条。全量从 1787 掉到 1780 时发现异常，核对
`git show HEAD~1` 后确认覆盖，已 `git show` 恢复原文件并**追加** 6 条新用例；
另有一处 `_FakeProc` 与原文件同名，导致 6 条原测试 `TypeError`，改名为
`_ControlProc` 后恢复。教训：**编辑任何已存在的文件前必须先读**，`write_to_file`
只用于新建文件。

**下一项**：3-4b——API（写守卫 + demo 限制）+ 前端进程表启停按钮（二次确认）
+ 审计展示；随后回到 2-1 配置视图。

### 8.83 MS5 收尾 3-4b：进程启停 API + 前端（2026-11-04）

**M10-6 完整落地**（接 3-4a 的命令通道）：
- **共享协议**：新增 `foundation/control.py`（`CONTROL_KEY`/`CONTROL_AUDIT_KEY`/
  `CONTROLABLE_PROCESSES`），dashboard 与 supervisor 用同一份定义，避免
  dashboard 反向依赖 CLI。
- **服务层**：`process_command(name, action, operator)` 做准入 + 入队；
  `process_commands(limit)` 读审计。
- **准入规则**（三道）：
  1. `dashboard` 不在可停集合——停掉后页面打不开，只能回命令行，属自锁；
  2. `okx.demo=false` 时默认拒绝（403），需配置
     `dashboard.allow_live_process_control=true` 才放行；
  3. supervisor 不在线（`quant:supervisor` 键不存在）返回 503——命令没人消费
     时不能假装成功。
- **API**：`POST /api/processes/{name}/stop|start`（写守卫）、
  `GET /api/processes/commands`（审计）；`ProcessControlError.status_code`
  映射 400/403/503。
- **进程表**：`desired` 透传，前端可区分「主动停止」与「异常离线」。
- **前端**：进程卡每行（除 dashboard）加「停止/启动」按钮（busy 防重复），
  卡片下方展示最近 5 条 supervisor 回执（含 rejected 原因）。

**测试**：后端 `TestProcessControl` 7 条（demo 允许入队、实盘需开关、
开关打开后放行、dashboard 不可停、未知进程拒绝、supervisor 离线 503、
审计读取）；前端 `controlProcess` 契约 1 条。

**验证**：`pytest` 全量 **1800 passed**（+7）；`npm test` **38 passed**（+1）；
`npx tsc --noEmit` / `npm run build` / `ruff` 全过。

**M10-6 至此完成**（3-4a 通道 + 3-4b 入口）。下一项：2-1 配置只读视图 +
来源标注（M10-1 + M10-9）。

### 8.84 MS5 收尾 2-1：配置只读视图 + 来源标注（2026-11-04）

**M10-1 + M10-9 落地**：
- 新增 `dashboard/config_view.py::config_snapshot`：展平 `Settings` 为点路径，
  逐项标注来源——`toml`（config.toml 里声明）/ `env`（`${VAR}` 引用）/
  `default`（未声明、Pydantic 默认值）。
- **脱敏**：`okx.api_key/secret/passphrase`、`smtp.password`、`dashboard.token`
  只显示 `***` 或空串，永不回显；`redis.url` 里的密码替换为 `://***@`。
- **降级**：配置文件缺失 → 全部标 `default` 并提示；TOML 语法错误 → 值仍来自
  已加载的 settings（不让整页 500），来源退化为 `default`。
- `GET /api/config`；`create_app` 新增 `config_path`，`quant dashboard` 传入
  实际 `--config` 路径（来源判断才准确）。
- 前端设置页从占位卡改为只读表格：配置项 / 值 / 来源三列，列宽与既有表格一致；
  仅展示，不提供在线修改入口（写操作留到 2-2 热改白名单）。

**测试**：`tests/test_config_view.py` 6 条——密钥脱敏、Redis 密码脱敏、
来源三态、缺失文件全 default、值反映生效配置、坏 TOML 降级。

**验证**：`pytest` 全量 **1806 passed**（+6）；`npm test` **38 passed**；
`npx tsc --noEmit` / `npm run build` / `ruff` 全过。

**下一项**：2-2 热改白名单（M10-10）——列出「哪些能在线改、哪些必须改文件」，
拒绝时说明原因。

### 8.85 MS5 收尾 2-2：热改白名单（M10-10）（2026-11-04）

**让「改这个配置该去哪」显性化**，避免用户以为改 `config.toml` 会热生效：
- 三类生效方式：`hot`（在线改立即生效）/ `restart`（改 toml 后重启）/
  `env_only`（只能环境变量，凭证类）。
- 规则表：
  | 前缀 | 方式 | 依据 |
  |---|---|---|
  | `risk.` | hot | `POST /api/risk/overrides`，worker 下一轮心跳生效 |
  | `okx.*` | env_only | 凭证只从环境变量读，看板永不回显 |
  | `smtp.password` | env_only | 同上 |
  | `dashboard.*` / `redis.*` / `state.*` | restart | 地址/路径变更必须重启进程 |
  | 其余 | restart | 默认不支持热改，宁可不承诺 |
- `GET /api/config` 的每一项新增 `reload` / `reload_hint`；
  `GET /api/config/hot-reload` 返回规则表与三类说明（供设置页图例/脚本）。
- 前端设置页加「生效方式」列（`在线生效` 绿色、`需重启`/`仅环境变量` 普通）
  与顶部图例；悬停显示具体操作提示（含真实入口）。

**测试**：`tests/test_config_view.py` 新增 2 条——三项代表性配置的 reload 分类
（且 hot 提示必须指向真实 `risk/overrides` 入口）、规则表覆盖 risk/okx/redis。

**验证**：`pytest` 全量 **1808 passed**（+2）；`npm test` **38 passed**；
`npx tsc --noEmit` / `npm run build` / `ruff` 全过。

**下一项**：2-3 凭证状态（M10-2）——OKX 权限/子账户/IP + SMTP 是否配置，
只读探测、不回显密钥。

### 8.86 MS5 收尾 2-3：凭证状态（M10-2）（2026-11-04）

**只读探测，永不回显密钥**：
- **OKX**：复用 `okx.live_check.run_checks`（交易/提币权限、子账户、IP 绑定），
  只返回 `name/status/detail`；`GET /api/credentials` 的响应结构里**没有**
  key/secret/passphrase 字段（有测试锁死结构，防「顺手加个回显」）。
- **SMTP**：只判断 `host`/`username`/`to_addrs` 是否齐全，**不真连**——每次刷新
  打一次 SMTP 既慢又可能触发风控。
- **60s 缓存**：看板 15s 轮询，不缓存会把 OKX 限频吃满；`?force=1` 手动重探。
- **失败降级**：缺凭证 → `configured=false` + 缺哪个；有凭证但探测失败（如 50101
  环境不匹配）→ `configured=true` + `error`，**不误报成「未配置」**。
- 前端设置页顶部「凭证状态」卡：OKX 模式（模拟/实盘）+ 权限项（pass 绿、
  warn 黄、fail 红）+ SMTP 配置情况，带「重新探测」按钮。

**测试**：`tests/test_credentials.py` 7 条——缺凭证、探测成功、探测失败降级、
缓存不重复探测 / force 绕过、SMTP 未配置/已配置、响应结构不含密钥字段。

**验证**：`pytest` 全量 **1815 passed**（+7）；`npm test` **38 passed**；
`npx tsc --noEmit` / `npm run build` / `ruff` 全过。

**下一项**：2-4 通知设置（M10-4）——SMTP 热改 + 测试发送（复用 `quant test-email`），
写入前做连通性校验。

### 8.87 MS5 收尾 2-6：保留策略分档（M10-3）（2026-11-04）

**M10-3 闭环**（此前只有全局天数 + 告警分档）：
- **按周期分档**：`StorageSettings.retention_by_bar`（如 `{"1m": 30, "5m": 90}`），
  `purge_bars` 按 K 线路径中的 `{bar}` 目录取对应天数；未列出的周期沿用
  `retention_days`。短周期数据量大、价值衰减快，统一 365 天是浪费。
- **死信纳入清理**：`purge_before` 增加 `dead_letters`（按 `created_at`），
  保留期内仍可回放，过期后随历史清理。
- **资金费率纳入清理**：新增 `purge_funding`，读 `data/funding_rates/*.json`
  过滤 `ts < cutoff` 后写回；整文件为空则删除。此前只增不减。
- `PurgeResult` 增加 `funding_deleted` / `dead_letters_deleted`，`summary()`
  与 `quant prune` 输出同步。
- `config/config.toml.example` 补 `retention_by_bar` 注释示例。

**测试**：`tests/test_retention.py` 新增 6 条——1m/1H 同刻不同保留期（前者删、
后者留）、不传 by_bar 退回全局、资金费率旧记录删除 / 空文件删除 / 目录缺失、
死信按 created_at 清理；并同步 `purge_before` 的精确字典断言（多 `dead_letters` 键）。

**验证**：`pytest` 全量 **1821 passed**（+6）；`ruff` 全过。纯后端。

**账本同步**：需求表 M10 全部条目更新为 14 ✅ / 1 🚫，合计 113/150（75.3%）；
MS5 剩余仅 M3-7（M3-13 仍 🟡）。

**下一项**：4-1 `quant research export`（M3-7 第一部分）。

### 8.88 MS5 收尾 4-1：`quant research export`（2026-11-04）

**M3-7 第一部分**：把研究所需数据导出成独立目录，notebook/脚本不必直连内部存储。
- `quant research export --inst BTC-USDT --bar 1H --start ... --end ... [--out DIR]
  [--run-id RUN]`
- 产物：
  - `bars.parquet`：区间内 K 线（数值保持 **Decimal 文本**，不转 float）
  - `trades.parquet`：`--run-id` 时复制该回测的成交产物；否则取本地 fills 并按
    品种/区间过滤
  - `backtest.json`：仅 `--run-id`，含 run 登记行与指标（`default=str` 兼容 Decimal）
  - `manifest.json`：导出时间、条数、区间、run_id、文件清单（供 notebook 校验
    数据版本）
- 边界：无 K 线直接报错退出（1）；日期格式错误报 `invalid date`；输出目录默认
  `research/{inst}-{bar}`。

**测试**：`tests/test_research_cli.py` 4 条——K线+本地成交（且不混入其它品种）、
`--run-id` 复制产物并写指标、无 K 线失败、非法日期失败。

**验证**：`pytest` 全量 **1825 passed**（+4）；`ruff` 全过。纯只读，不改运行逻辑。

**下一项**：4-2 `notebooks/` 模板 + README（M3-7 收尾）。

### 8.89 MS5 收尾 4-2：notebook 模板（M3-7 闭环）（2026-11-04）

**新增 `notebooks/` 模板**（M3-7 第二部分）：
- `README.md`：数据从 `quant research export` 来、启动方式、目录结构、
  `QUANT_RESEARCH_DIR` 环境变量、Decimal 文本注意点。
- `explore.ipynb`：4 个代码单元 + 2 个说明单元，**只用 pandas**（不引入
  matplotlib/jupyter 之外的新依赖）：
  1. 读 `bars.parquet` → 行数/时间范围/缺失检查；
  2. 读 `trades.parquet` → 按方向统计笔数/数量/手续费/名义；
  3. 读 `backtest.json`（存在才读）→ run 参数与指标；
  4. 下一步提示（用 `--run-id` 做参数对比、需要画图时再装 matplotlib）。
- **原则写进模板与测试**：notebook 只读导出目录，**不直连 SQLite/Redis/OKX**，
  研究代码不影响实盘进程。

**测试**：`tests/test_notebook_template.py` 3 条——ipynb JSON 结构合法且所有代码
单元可 `compile`（挡手写 JSON 的语法错）、代码里不出现 sqlite3/redis/okx、
README 说明导出命令与环境变量。

**验证**：`pytest` 全量 **1828 passed**（+3）；`ruff` 全过。

**账本**：M3-7 从 ⬜ 改为 ✅，M3 达 16 ✅ / 2 🟡 / 0 ⬜；合计 **114/150（76.0%）**，
**MS5 已无未开始项**，剩余 29 条全部是 MS7+（M5/M6）。

**下一项**：5-1 信号拦截原因契约（M3-5）——`Context.last_block_reason`，
让引擎卡能显示「被趋势过滤/撑压条件挡住」，而不是笼统的 `no_signal`。

### 8.90 MS5 收尾 5-1/5-2：信号拦截原因（M3-5 闭环）（2026-11-04）

**契约选择**：`Context.last_block_reason` 字段，而不是改 `on_bar` 返回类型——
现有一百多处策略实现无需改动，向后兼容。
- `sma_cross`：`warmup: need N bars`、`golden cross but trend filter blocked`、
  `golden cross but no support level`、`insufficient cash`、
  `golden cross ignored: already long`、`death cross ignored: no position`。
- `rsi`：`warmup: rsi not ready`、`oversold but no support level`、
  `insufficient cash`、`rsi=xx not in entry zone (<30 flat />70 long)`。
- 引擎：逐 bar **先清空**再回放（避免上一根原因残留），无信号时把
  `ctx.last_block_reason` 写进 `EngineTick.reason`，`publish_status` 原样透出。
- **5-2 无需改前端**：引擎卡已有 `engine-reason` 元素消费 `reason` 字段，
  只是此前 reason 恒为空；现在会显示具体拦截条件。
- 未开仓时不再只有笼统的 `no_signal`，「为什么没信号」可直接在页面读到。

**测试**：`tests/test_strategy_reasons.py` 5 条——SMA 预热/无交叉不编造原因/
趋势过滤/已持多忽略金叉、RSI 中性区间显示数值（逐根驱动，符合引擎调用方式）。

**验证**：`pytest` 全量 **1833 passed**（+5）；`ruff` 全过。纯后端（前端复用现有展示）。

**账本**：M3-5 🟡 → ✅；M3 达 17 ✅ / 1 🟡 / 0 ⬜；合计 **115/150（76.7%）**，
MS5 剩余 🟡 仅 M3-13、M4-8、M8-7、M8-8。

**下一项**：6-1 `quant check-strategies --strict`（M3-13 双副本一致性 CLI）。

### 8.91 MS5 收尾 6-1：`quant check-strategies`（M3-13 闭环）（2026-11-04）

**把双副本一致性校验从「测试级」提升为「可执行命令」**：
- 新增 `quant/strategies/consistency.py`：解析 Pine `input(...)`、toml `[params]`、
  `PARAM_SPEC` 默认值，输出扁平化的 `DriftItem`；`known` 字段区分**已登记**与
  **未登记**漂移。登记表（`KNOWN_PINE_DRIFT` / `KNOWN_SPEC_DRIFT`）从测试搬入
  模块，**测试与 CLI 共用同一份**，不会各说各话。
- 新增 `quant check-strategies [--strict] [--pine-dir] [--config-dir]`：
  默认列出全部差异并返回 0；`--strict` 时**只要存在未登记漂移就返回 1**（CI 用）。
  除参数差异外，还会检查「Pine 文件头未声明基线定位」「缺少 toml.example」。
- `tests/strategies/test_pine_python_consistency.py` 改为复用模块函数（断言不变）。

**测试**：新增 `tests/test_check_strategies.py` 4 条——仓库现状无未登记漂移、
自造 Pine 多参数在 `--strict` 下失败、非 strict 返回 0、完全一致时提示「未发现
任何差异」；原一致性测试 4 条保留。

**验证**：`pytest` 全量 **1837 passed**（+4）；`ruff` 全过（顺带修正
`cli/__init__.py` 的 import 排序）。

**账本**：**M3 达 18 ✅ / 0 🟡 / 0 ⬜**；合计 **116/150（77.3%）**；
MS5 剩余 🟡 仅 M4-8、M8-7、M8-8。

**下一项**：M5 因子库第一批（7-1 因子框架）。

### 8.92 M5 第一批 7-1：因子框架（2026-11-04）

**新增 `src/quant/factors/` 包**（PRD 4.5 的 M5-1~4 + M5-13 前置）：
- `protocol.py`：`Factor`（factor_id/name/category/scope/lookback/compute/
  default_params/aliases），构造时校验类别与 scope、`lookback >= 0`。
- `registry.py`：`@factor(...)` 装饰器 + 注册表；**同 ID/别名重复注册直接报错**
  （静默覆盖会让「跑的是哪个实现」变成谜）；`get_factor` 支持别名。
- `params.py`：`param_hash` = 规范化 JSON（`sort_keys` + 紧凑分隔符）的 sha256
  前 12 位；Decimal 转文本，避免 float 精度影响缓存键。
- `storage.py`：`FactorStore` 按
  `data/factors/{factor_id}/{param_hash}/{inst}/{bar}/{year}.parquet` 分片，
  **原子写**（tmp + replace），同 ts 以新值覆盖、历史保留。
- `builtin.py`：两个无参数示例因子 `momentum_20`、`volatility_20`，严格
  `(bars, params)` 契约、前 20 根为 `None`。
- **`Context.factors`**（M5-13 前置）：`dict[str, list[Decimal | None]]`，
  默认空、向后兼容；两处引擎构造无需改动（关键字参数 + 默认值）。

**测试**：`tests/test_factors.py` 16 条——内置注册/重复 ID/别名/未知 ID/非法类别/
清空注册表、`param_hash` 顺序无关/不同参/Decimal 文本、两个示例因子的数值、
缓存往返（含 None）/参数隔离/合并保留历史、`Context.factors` 默认与赋值。

**验证**：`pytest` 全量 **1853 passed**（+16）；`ruff` 全过。

**账本**：M5 达 **3 ✅ / 2 🟡 / 10 ⬜**（M5-1/2/3 ✅，M5-4/13 🟡）；
合计 **119/150（79.3%）**。

**下一项**：7-2 批量计算与增量更新（M5-5/12/13/14）——批量入口、回退
`lookback` 重叠计算、缺失/极值三选一、`FactorProvider`，以及全量=增量一致性测试。

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
