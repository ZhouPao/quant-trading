# 模拟盘运行手册

本项目在 OKX 模拟盘上运行信号消费 + 真实撮合 + 监控的启动与运维命令。

工作目录：`/Users/daxiongmao/Desktop/quant-trading`

---

## 1. 前置检查

```bash
# Redis 可用性（必须返回 PONG）
redis-cli ping

# 可选：清空旧队列/去重/心跳，从干净状态起步
redis-cli FLUSHDB

# 确认紧急停止开关处于关闭状态
quant kill-switch --status
```

配置文件：`config/config.toml`，其中 `[okx] demo = true` 表示模拟盘。
API 凭证来自环境变量：`OKX_API_KEY` / `OKX_SECRET` / `OKX_PASSPHRASE`。

---

## 2. 启动进程

日常运行推荐 2.5 的一条命令启动（自带进程自愈）；下面 2.1–2.4 是分进程手动
启动方式，适合排查单个进程问题时使用。两种方式不要同时用，避免端口/重复下单。

### 2.1 Webhook 服务（接收 TradingView / 模拟信号）

```bash
quant serve-webhook
```

默认监听 `127.0.0.1:8000`，路径 `/webhook/tv`。

### 2.2 Worker（消费信号并真实下单到 OKX 模拟盘）

```bash
quant run-worker --strategy sma_cross --broker okx --td-mode cash --paper-mode --ws
```

参数说明：

| 参数 | 含义 |
|---|---|
| `--strategy sma_cross` | 策略 ID，对应 `config/strategies/sma_cross.toml` |
| `--broker okx` | 使用 OKX REST 真实下单（模拟盘） |
| `--td-mode cash` | 现货模式；合约用 `cross` / `isolated` |
| `--paper-mode` | 放宽仓位上限到 100%，与回测 sizing 对齐 |
| `--ws` | 用 WebSocket 实时价；网络不稳时可去掉，回退 REST ticker |

去掉 `--paper-mode` 即按真实风控参数运行（单品种 30%、总仓位 80% 等）。

风控基数说明（排障必读）：

- 仓位比例与日亏损上限的基数取 monitor 快照里的 `account.total_equity`，
  与 engine 计算下单量读的是同一份快照（engine 用 `avail_usdt`）。
- 若 monitor 未运行或快照已过期（TTL 到期），worker 会退回
  `[backtest].initial_cash` 兜底，并在日志中打印
  `account snapshot unavailable, risk equity falls back to ...`。
  此时若真实账户权益远大于该值，下单量会被 `max_position_pct` 静默截断，
  日志表现为 `qty adjusted by max_position_pct`。
- `max_order_qty` 是独立的单笔名义上限（配置 10000），不受 `--paper-mode` 放宽。
  它恰好与回测 `initial_cash` 同量级，真实账户下会成为实际单笔上限；
  日志表现为 `qty adjusted by max_order_qty`，这是预期行为，不要当 Bug 调大——
  调大后模拟盘成交名义会超过回测规模，日报偏离检测将判 FAIL。

### 2.3 监控（账户/持仓/队列/心跳 + 告警）

```bash
quant monitor
```

默认每 60 秒一轮，监控项包括：账户权益、最大回撤、撤单率、最小强平价距离、
worker / engine 心跳、队列堆积、死信、保证金率、余额波动、连续亏损、API 错误率、
紧急停止状态。每轮同时把快照与告警发布到 Redis，供只读看板读取。

启动日志里的 `engine_heartbeat=on` 表示 engine 心跳规则已启用（键来自
`engine.status_key:{strategy}:{inst}`，engine 未启用时自动为 `off`）。

持续型规则（心跳、队列、保证金率、撤单率、错误率、kill switch）在恢复正常时
会补发一条 `INFO 已恢复（此前：…）`，邮件与看板都用绿色标记；
死信新增、余额突变、连续亏损属于事件，不会出现恢复通知。

宿主机休眠（合盖、系统睡眠、macOS App Nap）会让所有进程一起被冻结，
唤醒瞬间 worker / engine 的心跳看起来「陈旧」，但这并不是真实故障。
monitor 用自身的轮询间隔判断挂起：本轮与上一轮间隔超过
`interval_seconds × 3`（默认 180s）时，只跳过 `worker_heartbeat` /
`engine_heartbeat` 两条规则，并在日志留一条
`检测到系统挂起 Ns：本轮跳心跳类告警`；队列、死信、保证金、撤单率、
kill switch 仍照常评估。同时会丢弃这两个规则之前的误报状态，
所以不会补发一条假的「已恢复」。

想彻底避免挂起，可调整电源策略（电池模式下 `pmset` 默认 `sleep 1`，
即 1 分钟无操作就睡）：`sudo pmset -b sleep 0`，或在运行期间保持
`caffeinate -i`。

存活邮件每 `alive_email_hours`（默认 4 小时）一封，上次发送时间存
`quant:monitor:alive-last`，monitor 重启不会立刻补发。

SMTP 配置与验证：

```toml
[smtp]
host = "${SMTP_HOST:-}"        # 如 smtp.qq.com
port = 465
username = "${SMTP_USERNAME:-}"
password = "${SMTP_PASSWORD:-}"
from_addr = "${SMTP_FROM:-}"   # 留空则用 username
to_addrs = ["me@example.com"]
use_ssl = true                 # 465 用 true；587 通常改 false
```

```bash
export SMTP_HOST=smtp.qq.com SMTP_USERNAME=you@qq.com SMTP_PASSWORD=授权码
quant test-email                       # 用 to_addrs 里的收件人
quant test-email --to a@b.com,c@d.com  # 临时改收件人
```

未配置时命令退出码 1 并提示缺哪些字段；告警链路本身不依赖它，
monitor 会降级为只写日志，不会因为发信失败而停摆。

分步操作（含 QQ 邮箱授权码获取、常见报错对照）见 [SMTP 配置指南](SMTP_SETUP.md)。

每轮权益还会写入 SQLite 的 `equity_snapshots` 表（同一毫秒覆盖）。最大回撤的
历史峰值从该表恢复，因此 **monitor 重启后回撤不会归零**。权益数据不会自动清理，
数据量大时可按需删除旧记录：

```bash
sqlite3 data/state/quant.db \
  "DELETE FROM equity_snapshots WHERE ts < strftime('%s','now','-90 day')*1000;"
```

### 2.4 只读看板（可选）

```bash
# 首次使用：构建前端
cd web && npm install && npm run build && cd ..
# 启动（默认 127.0.0.1:8080，仅本机访问，无鉴权）
quant dashboard
```

浏览器打开 <http://127.0.0.1:8080>。页面每 5 秒轮询一次，展示账户权益、
风险指标、策略引擎、K 线、持仓、告警、订单与成交；除「紧急停止」外全部只读，
不提供下单/撤单入口。
账户卡右上角切换权益曲线区间（24h / 7d / 30d），曲线下方给出区间最低、
区间收益、区间最大回撤与区间最高；回撤按区间内滚动峰值计算。
数据来自 `equity_snapshots`（至少两轮后才有趋势），保留多久就能看多久，
更长的区间需要 monitor 持续运行累积。

策略引擎卡（K 线上方）：

- 状态：engine 是否在线（状态超过 `status_timeout_seconds` 未更新即判离线）、
  最近一轮结果（已出信号/无信号/等待行情/已跳过/出错）
- 最近评估 bar：本轮实际评估到的 K 线时间
- 水位延迟：水位相对本地最新已收盘 bar 落后几根；`已追平` 表示无积压，
  超过 `max_catchup_bars` 标红（更早的 bar 会被丢弃）
- 本轮/累计补评估、跳过 tick、累计信号：engine 进程内计数，**重启归零**
- 本地最新 bar：本地 parquet 的最新已收盘 K 线，可与最近评估 bar 对照
- 策略参数：直接读 `config/strategies/*.toml`，用于确认运行参数与回测一致

告警面板（持仓右侧）：

- 默认按规则聚合：每行一条规则，显示级别、命中次数、最新消息与时间；
  点击该行展开全部明细，再点收起
- 顶部按钮按级别筛选（全部 / ERROR / WARN / INFO），数字为该级别条数；
  右侧下拉按规则筛选，筛到单条规则时自动展开明细
- 同一规则在 `monitor.dashboard_alert_cooldown_seconds`（默认 600s）内只留一条，
  避免心跳丢失这类持续状态刷屏；`kill_switch` 例外，每次激活都会单独留痕
  （持续激活期间只保留一条，原因变化或解除后重新激活才新增）
- 顶栏 `ALERTS` 芯片显示 ERROR 总数，有 ERROR 时标红

K 线面板：

- 数据来自本地 parquet（`data/candles/`），不联网、不依赖 TradingView
- 周期按钮只显示磁盘上已有数据的周期；没有 4H/1D 时先跑
  `quant sync-candles --inst BTC-USDT --bar 4H --loop`
- 绿色三角为买入成交、红色三角为卖出成交（来自本地 `fills` 表）
- 悬浮虚线为当前持仓均价，仅在本地持仓非 0 时显示
- 右上角会提示已标注的成交笔数；无数据时提示先同步行情

回测面板（K 线下方）：

- 两种模式：单次回测 / 参数寻优，口径与 `quant backtest`、`quant optimize` 一致
- 只计算不落盘：不写 CSV/HTML、不写 SQLite，页面刷新即丢弃
- 参数覆盖与网格语法同 CLI：`fast_len=10;slow_len=50`、`fast_len=5,10;slow_len=20,50`
- 单次回测展示指标卡、权益曲线与成交明细；寻优展示参数排名，非法组合自动跳过
- 网格组合上限 500 组，计算串行执行，避免多请求同时打满 CPU
- 每次单次回测自动记入「历史对比」（存浏览器 `localStorage`，最多 20 条，
  只存指标摘要不存曲线）；勾选 2–4 条显示横向对比表，绿字为该指标最优值
- 同一配置重跑会替换旧记录而不是追加；「清空」只删本地历史，不影响任何数据

成交盈亏卡（订单/成交上方）：

- 口径与风控一致：全部 `fills` 按 FIFO 配对成回合，手续费计入成本
- 六个指标：已实现盈亏、胜率、盈亏比（总盈利/总亏损）、平均盈利、
  平均亏损、累计手续费；页脚给出最好/最差回合与统计时间区间
- 未平仓部分不计入回合，所以这里只反映**已实现**盈亏，不含浮动盈亏
- 没有成交时显示空态；要等 engine 出信号并真实成交后才会有数字

模拟盘日报卡（成交盈亏下方）：

- 每天 08:00 由 monitor 生成一份「模拟盘 vs 回测」偏离报告，
  文件写在 `reports/daily/{日期}-{策略}-{品种}.csv`
- 面板按时间倒序列出，点击一行展开五项比对：成交笔数、成交量、
  名义金额、手续费、方向序列；偏离超过 `report.threshold` 标红
- 只读取已有 CSV，不会触发重新计算；当天没有成交时不会生成日报，
  卡片显示空态属正常

紧急停止卡（风险指标下方）：

- 卡片标注「只拦新信号 · 不撤挂单 · 不停进程」：点击「停止开新仓」
  再填原因并二次确认；停止后 worker 拒绝新信号并写入死信队列，
  engine 本轮跳过（但仍推进水位，解除后不补单）
- **不会撤挂单**：需要同时撤销挂单请改用 `quant kill-switch --on --broker okx`
- 只写 Redis 键 `quant:kill-switch`，改完立即生效；顶栏 `KILL SWITCH`
  芯片会变红，monitor 当轮产生一条 ERROR 告警（含原因）
- 写操作守卫：只接受本机 Host + `X-Dashboard-Request: 1` 自定义头
  （挡 CSRF/DNS 重绑定）；如需在非本机或多人环境开放，配置
  `DASHBOARD_TOKEN` 环境变量（或 `config.toml` 的 `dashboard.token`），
  页面会出现 token 输入框，输入后存 `sessionStorage`
- 风控自动触发（日亏损上限、连续亏损保护）仍由 worker 执行，
  网页按钮只是把同一个标志做成可点入口

账户与指标来自 monitor 发布到 Redis 的快照，订单/成交直接读本地 SQLite。
monitor 未运行时页面会提示快照过期。

接口：

- `GET /api/equity?hours=24`（`hours` 取值 1~720）
- `GET /api/engine`（引擎健康度：在线状态、水位延迟、补评估/跳过计数）
- `GET /api/candles?bar=1H&limit=300`（`limit` 取值 20~1000，默认取 `engine.inst`）
- `GET /api/strategies`（策略清单，读 `config/strategies/*.toml`）
- `POST /api/backtest`（body：`strategy/start/end/inst/bar/params/warmup_bars`）
- `POST /api/optimize`（额外传 `grid/metric/top`；参数错误返回 400 与可读原因）
- `POST /api/kill-switch`（body：`action: "on" | "off"`、`reason`；唯一的写接口，
  需本机 Host + `X-Dashboard-Request: 1`，配置 token 时再校验 `X-Dashboard-Token`）

后台运行：

```bash
nohup quant dashboard > data/logs/dashboard.log 2>&1 &
```

### 2.5 一条命令启动 + 进程自愈（推荐）

```bash
quant supervisor --strategy sma_cross --broker okx --td-mode cash --paper-mode --ws
```

默认拉起 `candles` / `webhook` / `worker` / `engine` / `monitor` / `dashboard`
六个子进程。

| 参数 | 含义 |
|---|---|
| `--strategy sma_cross` | 传给 run-worker 与 run-engine |
| `--broker okx` | 默认 okx（模拟盘）；`mock` 为纯内存模拟 |
| `--td-mode cash` | 现货 cash；合约用 cross/isolated |
| `--paper-mode` / `--ws` | 传给 run-worker |
| `--no-dashboard` | 不起看板（其余五个照常） |
| `--dry-run` | 只打印各子进程命令，不启动 |

行为：

- 任一子进程退出后自动重启；连续快速失败按指数退避（2s 起，最多 60s），
  稳定运行满 60 秒后退避清零
- 子进程输出写到 `logs/candles.log`、`logs/webhook.log`、`logs/worker.log`、
  `logs/engine.log`、`logs/monitor.log`、`logs/dashboard.log`（追加）
- `Ctrl+C` 或 `kill <supervisor pid>` 会向全部子进程发 SIGTERM，
  10 秒内未退出则 SIGKILL
- supervisor 自身崩溃不会自愈，需要 launchd 托管

macOS launchd 常驻示例（保存为
`~/Library/LaunchAgents/com.quant.supervisor.plist`，路径按实际替换）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.quant.supervisor</string>
  <key>WorkingDirectory</key><string>/Users/daxiongmao/Desktop/quant-trading</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Library/Frameworks/Python.framework/Versions/3.13/bin/quant</string>
    <string>supervisor</string>
    <string>--strategy</string><string>sma_cross</string>
    <string>--broker</string><string>okx</string>
    <string>--td-mode</string><string>cash</string>
    <string>--paper-mode</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key>
  <string>/Users/daxiongmao/Desktop/quant-trading/logs/supervisor.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/daxiongmao/Desktop/quant-trading/logs/supervisor.err.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.quant.supervisor.plist    # 启动并常驻
launchctl unload ~/Library/LaunchAgents/com.quant.supervisor.plist  # 停止
```

### 2.6 K线增量同步（candles）

`supervisor` 已自动托管，无需单独启动。手动排查时：

```bash
quant sync-candles                          # 按 config [candles] 同步一次并退出
quant sync-candles --inst BTC-USDT,ETH-USDT --bar 1H
quant sync-candles --loop --interval 300    # 常驻轮询
```

行为：

- 终点自动取「最近一根已收盘的 bar」，未收盘的最后一根不落盘（close 还会变）
- 每轮回退 `lookback_bars + 1` 根重拉，覆盖上一轮可能未收盘时落盘的数据，
  顺带补断点附近的洞
- 断点存 `data/.checkpoints/{inst}_{bar}.json`；单个品种失败不影响其他品种

配置（`config/config.toml`）：

| 配置项 | 含义 |
|---|---|
| `insts` | 同步的品种列表 |
| `bar` | K线周期 |
| `interval_seconds` | 轮询间隔；`0` 表示只跑一次 |
| `lookback_bars` | 每轮额外回看几根 |

查看同步结果：

```bash
tail -f logs/candles.log
```

首次回填历史（同步只补最近几根，不做深历史）：

```bash
quant fetch-candles --inst BTC-USDT --bar 1H --start 2024-10-26 --end 2026-10-26
```

深拉注意两点：

1. `fetch()` 的 cursor 取 `max(start, 断点+1根)`，已有断点会让它直接跳过。
   回填前先删 `data/.checkpoints/{inst}_{bar}.json`（建议先备份整个目录）。
2. 拉完断点会停在最旧一页，必须把 `{inst}_{bar}.json` 的 `last_ts` 改回最新
   已收盘 bar，否则 `sync-candles` 每轮都会重新下载整段历史。

当前本地已回填 BTC/ETH 的 1H/4H/1D 各约 2 年（2024-10-26 起）。

日报与回测都读本地 parquet，所以 candles 停掉会让两者数据失真。

### 2.7 本地策略实时信号引擎（engine）

`supervisor` 已自动托管，无需单独启动。单独排查：

```bash
quant run-engine --once                       # 评估一轮后退出
quant run-engine                              # 常驻
quant run-engine --strategy rsi --inst ETH-USDT --bar 4H --interval 60
```

工作方式：

- 每 `interval_seconds` 检查一次；用本地 parquet 回放 `warmup_bars` 根 K线，
  只对**最近一根已收盘 bar** 出信号，历史 bar 不补发
- 持仓取 SQLite `positions`，可用资金取 monitor 发布的账户快照
  （`avail_usdt`），读不到时用 `cash_fallback`
- 信号写 Redis 队列 `quant:signals`，与 TradingView webhook 完全同一条链路，
  由 worker 做风控与下单
- 每根 bar 只处理一次，水位存 `quant:engine:last-bar:{strategy}:{inst}`
- 水位之后漏掉的 bar 会**逐根补评估**（日志 `engine catching up N bar(s)`），
  休眠/停机期间出现的交叉不会丢；补评估时用本根收盘价同步虚拟持仓，
  保证后续死叉能看到多头并正常平仓
- 单轮补评估上限 `max_catchup_bars`（默认 24），超限时丢弃更早的 bar 并告警
  （`catch-up truncated`）——停机太久时早期信号已失去意义
- 首次启动（无水位）只评估最新一根，不补发窗口内的历史信号
- 命中以下情况会跳过并推进水位：kill switch 生效、该品种有未完成挂单
- 每轮把状态 JSON 写到 `quant:engine:status:{strategy}:{inst}`，看板据此展示
  在线状态、最近评估 bar、补评估与跳过计数（只读，不影响交易链路）
- 策略配置复用 `config/strategies/{id}.toml`，与回测、日报同一份

配置（`config/config.toml`）：

| 配置项 | 含义 |
|---|---|
| `enabled` | false 时进程启动即退出 |
| `strategy` / `inst` / `bar` | 策略与标的 |
| `interval_seconds` | 轮询间隔（每根 bar 只评估一次） |
| `warmup_bars` | 回放窗口，需覆盖策略最慢周期（SMA 50 建议 ≥ 200） |
| `max_catchup_bars` | 单轮最多补评估几根漏掉的 bar（1H 默认 24 = 一天） |
| `cash_fallback` | 读不到账户可用 USDT 时的仓位基数 |
| `status_timeout_seconds` | 状态超过该秒数未更新即判引擎离线（默认 180） |

查看运行情况：

```bash
tail -f logs/engine.log
sqlite3 data/state/quant.db "SELECT strategy_id,side,qty,ts FROM signals ORDER BY ts DESC LIMIT 5;"
# 或直接看板「策略引擎」卡；Redis 原始状态：
redis-cli get quant:engine:status:sma_cross:BTC-USDT
redis-cli get quant:engine:last-bar:sma_cross:BTC-USDT
```

注意：engine 只产生信号，撤单率/回撤等风控与 kill switch 仍由 worker 执行。

---

## 3. 验证链路

```bash
# 推一条模拟信号（模拟 TradingView alert）
quant simulate-alert --strategy sma_cross --side buy --inst BTC-USDT --qty 0.01

# 查看最近成交是否落库
sqlite3 data/state/quant.db \
  "SELECT inst_id,side,qty,price,fee,ts FROM fills ORDER BY ts DESC LIMIT 5;"

# 查看当前持仓
sqlite3 data/state/quant.db \
  "SELECT inst_id,qty,avg_price FROM positions;"

# 模拟盘 vs 回测偏离报告
quant paper-report --strategy sma_cross --inst BTC-USDT --bar 1H --window-hours 60
```

### 3.1 每日偏离日报（自动）

`config/config.toml` 中 `[report] enabled = true` 后，monitor 在本地时间过了
`at`（默认 08:00）的第一轮自动生成当天报告：

| 配置项 | 含义 |
|---|---|
| `at` | 本地时间 `HH:MM`，过了该时刻生成 |
| `strategy` / `inst` / `bar` | 报告的对比标的，需与 worker 一致 |
| `window_hours` | 回测对比时间窗 |
| `threshold` | 最大允许偏离，超过记 FAIL |
| `out_dir` | CSV 输出目录，文件名 `YYYY-MM-DD-<strategy>-<inst>.csv` |
| `email` | true=有 SMTP 时发邮件，未配置则仅日志 |

行为：

- 当天只生成一次，标记存 Redis 键 `quant:daily-report:last`，monitor 重启不会重复
- monitor 在 `at` 之后才启动，会立刻补一次当天报告
- 无成交或无本地 K线时只记日志（`daily report ... skipped`），并标记当天已处理，
  避免每分钟重跑回测；需要时手动 `quant paper-report` 重跑
- 报告结果同步写日志：`daily report <date> PASS/FAIL max_dev=... -> <csv>`

口径说明（与 CLI 回测的区别，容易误判为 Bug）：

- 日报的回测从**本地最早一根 K 线开始连续成交**，再只比较窗口内的成交。
  原因是模拟盘在窗口起点可能持有更早建立的仓位，回测必须还原同样的持仓，
  否则首笔平仓就对不上：实测只回看 200 根时会产生 52% 的假偏离，全量历史为 0%。
- 因此日报不使用 `quant backtest --warmup-bars` 的「预热不成交」语义——那里只需
  指标就绪，日报还要还原持仓。两者数字不同属于预期，不是计算错误。
- 前提是本地 K 线不晚于模拟盘开户时间；若晚于，需先 `fetch-candles` 补齐早期数据。

查看历史日报：

```bash
ls -lt reports/daily | head
```

### 3.2 数据保留清理（自动 + 手动）

`[storage] retention_days` 决定保留多久，monitor 每日在 `retention_at` 后清理一次
（Redis 键 `quant:retention:last` 做当天去重）。也可手动触发：

```bash
quant prune                # 按 config 的 retention_days
quant prune --days 180     # 临时覆盖保留天数
```

| 配置项 | 含义 |
|---|---|
| `retention_days` | 保留天数（<=0 视为关闭，不会误删全量） |
| `retention_enabled` | monitor 是否每日自动清理 |
| `retention_at` | 本地时间 `HH:MM` |

清理范围：

- K线 parquet（`data/candles/**`）：删除过期行，整年文件清空则删文件
- SQLite：signals / orders / fills 过期记录；equity_snapshots 只保留峰值那一行
  （否则 monitor 重启后历史回撤峰值会丢失）
- 日报 CSV（`report.out_dir`）：按文件修改时间删除

日志：`retention 2026-10-25 cutoff=... bars=.. bar_files=.. signals=.. orders=.. fills=.. equity=.. reports=..`

注意：保留期必须覆盖回填的历史跨度。当前本地有约 2 年 K 线，
`config.toml` 已把 `retention_days` 调为 1095；若沿用默认 365，
每日清理会把一年前的 K 线删掉，表现为「回测最早数据突然变晚」。

### 3.3 策略回测与参数寻优

在本地 parquet 上验证策略，是「改参数 → 验证 → 上模拟盘」的前置环节。

```bash
# 单次回测：打印指标，并写 CSV + HTML 到 reports/backtest/
quant backtest --strategy sma_cross --inst BTC-USDT --bar 1H \
               --start 2025-01-01 --end 2025-12-31

# 覆盖策略参数（优先级：--params > config/strategies/*.toml）
quant backtest --strategy sma_cross --inst BTC-USDT \
               --start 2025-01-01 --end 2025-12-31 \
               --params "fast_len=10;slow_len=60"

# 只看指标不落盘
quant backtest --strategy rsi --inst BTC-USDT \
               --start 2025-01-01 --end 2025-12-31 --no-report
```

| 参数 | 含义 |
|---|---|
| `--start` / `--end` | 回测区间（含首尾两天，UTC） |
| `--warmup-bars` | 预热 K线数，默认 200；只驱动策略状态，不成交、不计入指标 |
| `--params` | 覆盖策略参数，用 `;` 分隔 |
| `--config-dir` | 策略配置目录，默认 `config/strategies` |
| `--out-dir` | 报告目录，默认 `reports/backtest` |

预热的意义：`--start` 之前的 bar 仍会喂给策略，保证 SMA 窗口和 RSI 的 Wilder
平滑在区间起点就已就绪；但这些 bar 产生的信号被丢弃，所以指标只反映
`[start, end]` 区间。预热不足会直接报错，避免回测结果被冷启动污染。

网格寻优：

```bash
quant optimize --strategy sma_cross --inst BTC-USDT --bar 1H \
               --start 2025-01-01 --end 2025-12-31 \
               --grid "fast_len=5,10,20,60;slow_len=30,50,80" \
               --metric sharpe --top 10
```

- `--metric` 默认 `sharpe`；`max_drawdown` 与 `avg_holding_bars` 按「越小越好」排序
- 组合数 = 各维度取值数相乘；非法组合（如 `fast_len >= slow_len`）自动跳过并在表头提示
- 固定参数（配置文件里的 `commission_rate` 等）会与每组网格参数合并后再回测

报告文件：`reports/backtest/<strategy>-<inst>-<bar>-<start>_<end>.csv|.html`
（CSV 含权益/回撤曲线与交易明细，HTML 含指标表与内联 SVG 曲线）。

#### 样本内 / 样本外验证（选参数必做）

网格寻优的最优参数只能信「样本外还成立」的那一组。固定两步：

```bash
# 1. 样本内寻优（例：2024-11-10 ~ 2025-09-30）
quant optimize --strategy sma_cross --inst BTC-USDT --bar 4H \
               --start 2024-12-01 --end 2025-09-30 \
               --grid "fast_len=5,10,20;slow_len=20,50,100" --metric sharpe

# 2. 把 Top 参数原样拿到后一段跑（例：2025-10-01 ~ 2026-10-26）
quant backtest --strategy sma_cross --inst BTC-USDT --bar 4H \
               --start 2025-10-01 --end 2026-10-26 \
               --params "fast_len=10;slow_len=100" --no-report
```

判读要点：

- 必须同时看**同期买入持有**的涨跌幅（本轮 OOS 是熊市，绝对亏损不等于策略无效）
- 盈亏比 × 胜率的小幅差异会被成本吃掉：每轮往返约 0.3%
  （`commission_rate=0.001` 单边 + `slippage_bps=5` 单边），
  60 轮 ≈ 18pp。**先比轮数，再比指标**
- 样本内夏普 0.2~0.4 这种量级基本不可信；本次实测样本外全部转负

预热长度决定最早可回测起点：1H 需 200 根 ≈ 9 天，4H ≈ 34 天，1D ≈ 200 天。
起点早于「本地最早 bar + 预热」会直接报 `not enough warmup bars`。

---

## 4. 紧急停止

```bash
# 激活：拒绝所有新信号 + 撤销本地记录中的 pending 挂单
quant kill-switch --on --reason "manual" --broker okx

# 查询状态
quant kill-switch --status

# 解除（恢复交易）
quant kill-switch --off
```

看板「紧急停止」卡也能切换同一个标志（`POST /api/kill-switch`），但**不撤挂单**；
需要一并撤销 pending 挂单时仍用上面的 `quant kill-switch --on --broker okx`。

自动触发：日亏损上限或连续亏损保护命中时，worker 会自动启用 kill switch，
原因记为 `auto: ...`。**自动启用后需人工 `--off` 恢复**，不会自行重启交易。

---

## 4.1 极端行情熔断（按品种，M7-6 / M7-12）

engine 每轮用已收盘 bar 判 `(high − low) / open`，**严格大于**
`[risk] circuit_amplitude_pct`（默认 10%）即熔断该品种，冷却
`circuit_cooldown_seconds`（默认 900 秒）。冷却结束后**不会自动恢复交易**，
状态转 `awaiting_resume`，必须人工确认。

熔断只影响单个品种：其它品种照常交易，也没有动 kill switch
（状态键 `quant:risk:circuit:{inst_id}` 与 `quant:kill-switch` 完全分开）。

**风险（务必知道）**：`circuit_resume_requires_confirm = true`（默认）时，
熔断键**不设 TTL、不会自己消失**。只要没人去看板点「确认恢复」，
该品种就会一直不开新仓——这不是故障，是刻意用「宁可少交易」换「不踩第二波」。
看板长期没人看、或者熔断告警邮件被忽略时，漏掉的是整段行情，
而日志与告警里只有一条 `circuit breaker tripped`。因此：

- 收到 `circuit_breaker` 告警后，**当天**就要决定恢复还是保持暂停；
- `awaiting_resume` 期间的行情不会被补评估（水位已推进到最新 bar），
  恢复后从下一根开始，不会用过期价格补单；
- Redis 未开启持久化并发生重启时，熔断键会一起丢失，相当于自动恢复交易，
  此时必须人工核对行情，不能当成「系统已评估过风险」；
- 不想承担「忘记确认导致长期不开仓」的风险时，把
  `[risk] circuit_resume_requires_confirm` 设为 `false`（重启 engine 生效），
  退回「TTL 到期自动恢复」的旧行为。

```bash
# 查看当前熔断中的品种（值为 JSON）
redis-cli --scan --pattern 'quant:risk:circuit:*'
redis-cli GET quant:risk:circuit:BTC-USDT

# 人工确认恢复（写操作守卫：本机 Host + 自定义头；配置了 token 还要带 X-Dashboard-Token）
curl -X POST -H 'Host: 127.0.0.1:8080' -H 'X-Dashboard-Request: 1' \
  http://127.0.0.1:8080/api/risk/circuits/BTC-USDT/resume
# {"inst_id":"BTC-USDT","resumed":true,"triggered_ts":...,"reason":"..."}

# 紧急起量：直接删键等同于「跳过确认」，会立即恢复该品种交易，用前先看行情
redis-cli DEL quant:risk:circuit:BTC-USDT
```

冷却期未结束时接口返回 `{"resumed": false, "reason": "cooldown not finished"}`，
这是刻意的：冷却没走完就放行，等于把阈值变成摆设。恢复动作会写一条
`decision=resumed` 的 `risk_events`，并在看板告警流里留一条 INFO；
触发与恢复都可事后核对：

```bash
sqlite3 data/state/quant.db \
  "SELECT datetime(ts/1000,'unixepoch','localtime'), inst_id, decision, reason
     FROM risk_events WHERE rule='circuit_breaker' ORDER BY ts DESC LIMIT 20;"
```

---

## 5. 停止进程

- 手动分进程启动：各终端 `Ctrl+C` 即可优雅退出。
- supervisor：前台 `Ctrl+C`，后台 `kill <supervisor pid>`；会先停掉所有子进程。
- launchd：`launchctl unload ~/Library/LaunchAgents/com.quant.supervisor.plist`。

worker 退出时会把 inflight 信号回滚，下次启动自动恢复。

---

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| `50110` IP 白名单错误 | 确认 OKX 模拟盘 Key 白名单与当前代理出口 IP 一致，见 `docs/reference/HOW_TO_GET_PROXY_EXIT_IP.md` |
| `50101` API Key 不匹配 | 确认 `.env` 中为**模拟盘** Key（实盘 Key 加 `x-simulated-trading` 头会报此错） |
| `51020` 最小金额不足 | 现货市价单需按基础币数量下单（代码已固定 `tgtCcy=base_ccy`） |
| worker 心跳告警 | 确认 `run-worker` 进程在运行；心跳键 `quant:heartbeat:worker` TTL 120s。若告警时间点与 `monitor.log` 的空档吻合（整机休眠），新版已自动跳过 |
| 偏离报告 FAIL | 适当调大 `--window-hours`，覆盖回测信号的完整窗口 |
| engine 一直 `no signal`、`fills` 为空 | 先确认本地数据里真的有交叉，而不是链路故障。见下方排查步骤 |
| 下单量比预期小 | 看日志是 `qty adjusted by max_order_qty` 还是 `max_position_pct`，两者原因不同 |
| 某品种一直不开仓，日志有 `circuit breaker awaiting manual resume` | 该品种熔断冷却已结束但没人确认。看板「风险规则」点「确认恢复」，或按 §4.1 调接口。**熔断键不设 TTL，不确认就一直暂停** |

`no signal` 排查步骤：

1. `grep 'no signal' logs/engine.log | tail` 确认是「无信号」而非报错
2. 用本地数据独立算一遍双均线交叉，确认最后一次交叉时间：

   ```bash
   quant backtest --strategy sma_cross --inst BTC-USDT --bar 1H \
     --start 2026-09-01 --end 2026-10-26 --params fast_len=10,slow_len=50
   ```

   若回测在近期也没有成交，说明策略确实未触发，属正常。
3. 检查水位是否落后于最新 bar：水位键
   `quant:engine:last-bar:{strategy}:{inst}`。落后属正常，下一轮会自动补评估；
   日志会打印 `engine catching up N bar(s)`。
4. 检查是否被跳过：`grep -E 'skipped|kill switch|circuit breaker|catch-up truncated' logs/engine.log`。
   kill switch 生效、品种熔断中或有未完成挂单时会跳过并推进水位；
   `circuit breaker awaiting manual resume` 说明熔断已过冷却但没人工确认（见 §4.1）；
   `catch-up truncated` 表示停机超过 `max_catchup_bars`，更早的信号已丢弃。
