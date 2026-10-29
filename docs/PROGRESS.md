# 项目进度与经验总结

> 更新时间：2026-10-27
> 项目：`quant-trading`（OKX 模拟盘量化交易系统）
> 依据：[PRD v0.5](requirements/2026-10-17-quant-trading-platform-requirements.md)

---

## 1. 一句话现状

数据、回测、信号、下单、监控、看板六段链路全部打通，**OKX 模拟盘已在自动跑**；
M1–M3 完成，M4 完成约 95%（含 engine 心跳与恢复通知；只差填 SMTP 账号实测），M5 实盘未开始。

### 量化快照

| 指标 | 数值 |
|---|---|
| 源码 | 102 个 Python 文件 / 约 13,744 行 |
| 测试 | 53 个测试文件 / 约 18,298 行 / **949 条全部通过** |
| 前端 | 4 个文件 / 约 5,090 行（React 18 + TS + Vite + lightweight-charts） |
| CLI 子命令 | 19 个 |
| 本地行情 | BTC / ETH-USDT 各 1H 17,534 根、4H 4,383 根、1D 729 根（约 2 年，2.3 MB parquet） |
| 当前策略 | `sma_cross`（1H，fast=10 / slow=50） |
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

---

## 9. 相关文档

- [PRD v0.5](requirements/2026-10-17-quant-trading-platform-requirements.md)
- [M1 详设](specs/2026-10-17-m1-data-foundation-design.md)
- [M2 详设](specs/2026-10-17-m2-backtest-webhook-design.md)
- [模拟盘运行手册](reference/RUNBOOK_PAPER_TRADING.md)
- [M3 模拟盘测试报告](tests/2026-10-24-m3-paper-trading-test-report.md)
- [项目结构说明](PROJECT_STRUCTURE.md)
