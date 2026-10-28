# M2 Step 7-12 测试报告 — Webhook / 队列 / 状态 / 风控 / Worker

> 日期：2026-10-18
> spec 对照：§6 Webhook、§7 队列、§8 去重、§9 Worker、§10 风控、§11 状态、§14 测试
> 上游报告：[Step 4-6 策略与寻优](./2026-10-18-step4-6-strategies-optimizer-test-report.md)

---

## 1. 产出文件

| 文件 | 说明 |
|---|---|
| `src/quant/webhook/parser.py` | AlertParser：JSON → Signal，失败抛 ParseError |
| `src/quant/webhook/auth.py` | 共享 token + HMAC-SHA256 常量时间校验 |
| `src/quant/webhook/server.py` | FastAPI：鉴权 → 解析 → 去重 → 入队；`/health` |
| `src/quant/queue/signal_queue.py` | 可靠队列（pending/inflight/dead）+ Deduper |
| `src/quant/state/sqlite_store.py` | signals/orders/fills/positions/heartbeat 五表 |
| `src/quant/risk/config.py` | 从 `config/strategies/*.toml` 读 `[risk]` |
| `src/quant/risk/checker.py` | 5 条风控规则 + 自动下调数量 |
| `src/quant/execution/executor.py` | M2 模拟执行器（Order + Fill） |
| `src/quant/execution/worker.py` | 消费循环、心跳、崩溃恢复、幂等 |
| `src/quant/cli/serve_webhook.py` | `quant serve-webhook` |
| `src/quant/cli/run_worker.py` | `quant run-worker --strategy sma_cross` |
| `tests/test_webhook.py` | 22 测试 |
| `tests/test_queue.py` | 14 测试 |
| `tests/test_sqlite_store.py` | 17 测试 |
| `tests/test_risk.py` | 19 测试 |
| `tests/test_worker.py` | 9 测试 |
| `tests/test_integration_m2.py` | 9 测试 |
| `tests/smoke/test_e2e_m2_pipeline.py` | 全链路烟测（14 场景） |

---

## 2. 测试结果

| 测试集 | 结果 |
|---|---|
| `test_webhook.py` | 22 passed |
| `test_queue.py` | 14 passed |
| `test_sqlite_store.py` | 17 passed |
| `test_risk.py` | 19 passed |
| `test_worker.py` | 9 passed |
| `test_integration_m2.py` | 9 passed |
| **新增合计** | **90 passed** |
| **全量回归** | **510 passed** |
| ruff | All checks passed |
| 全链路烟测 | **14/14 PASS** |

---

## 3. 全链路烟测场景

真实 uvicorn 监听 `127.0.0.1:8765` + fakeredis + httpx：

1. webhook 服务启动
2. 401 未授权拒绝
3. buy 信号入队
4. 重复 alert 去重（pending 保持 1）
5. sell 信号入队
6. `/health` 队列计数正确
7. 非法 payload → 400 + 死信
8. worker 消费全部信号（2 条）
9. 信号状态 `executed`
10. 买 1 卖 1 → 持仓归零
11. 两笔成交落库
12. inflight 崩溃恢复
13. 恢复后信号执行
14. 恢复后持仓正确

---

## 4. 关键设计决策

### 队列可靠性

- `BRPOPLPUSH pending → inflight` 原子搬移；处理成功才 `LREM`
- 启动时 `RPOPLPUSH inflight → pending` 全量回滚，保持 FIFO 顺序
- 损坏的 inflight payload 直接进 dead，不阻塞恢复

### 去重指纹

- `sha256(规范 JSON)`，**忽略 `signal_id` 与 `ts`**
- 原因：TV 重发时这两个字段由服务端/每次请求生成，必须排除才能识别重发
- `SET NX EX 300` 原子写入；TTL 到期后可再次接收同内容信号

### 幂等与崩溃恢复

- 处理前查 SQLite：`executed` / `rejected` 直接丢弃
- 执行成功后才写状态 + 从 inflight 移除；两者之间崩溃 → 恢复时按状态跳过
- 单条异常 → 死信 + 移除 inflight，主循环不退出

### 风控

| 规则 | 行为 |
|---|---|
| 单笔最大名义价值 | 超限自动下调 qty |
| 单品种持仓比例 | 超限下调；无空间则拒绝 |
| 总仓位上限 | 跨品种加总后下调或拒绝 |
| 日亏损上限 | 当日已实现亏损超阈值直接拒绝 |
| 连续亏损 | 最近 N 个回合全亏直接拒绝 |

- 仅在**加仓方向**检查名义价值；平仓卖出不受单笔上限阻止
- 无市价且无持仓时跳过名义价值类规则（M2 限制）

### 状态库

- Decimal 以 TEXT 存储，避免浮点误差
- 新增 `fills` 表（spec 未列）：风控盈亏统计与崩溃恢复需要成交明细
- 线程安全：单连接 + Lock；worker 通过 `asyncio.to_thread` 调用

---

## 5. 与 spec 的偏差（已确认合理）

1. **`Executor.execute` 返回 `ExecutionResult(order, fill)`** 而非仅 `Order`：持仓更新与成交落库需要 fill
2. **新增 `fills` 表**：见上
3. **去重忽略 `ts`**：spec 未明确，但不忽略会导致重发去重失效
4. **`n_jobs > 1` 仍串行**：M2 数据量下可接受，接口已预留

---

## 6. 运行方式

```bash
# 终端 1
quant serve-webhook --config config/config.toml

# 终端 2
quant run-worker --strategy sma_cross --config config/config.toml

# 发送测试 alert
curl -X POST http://127.0.0.1:8000/webhook/tv \
  -H 'Content-Type: application/json' \
  -H 'X-Auth-Token: <WEBHOOK_AUTH_TOKEN>' \
  -d '{"strategy_id":"sma_cross","side":"buy","order_type":"market","inst_id":"BTC-USDT","qty":"0.001"}'
```

前置条件：本地 Redis（`REDIS_URL`，默认 `redis://localhost:6379/0`）。
本机未安装 Redis，因此烟测使用 fakeredis + 真实 HTTP 端口验证协议链路。

---

## 7. M2 完成度

| Step | 内容 | 状态 |
|---|---|---|
| 1 | 数据模型 + Strategy ABC | 完成 |
| 2 | Matcher + 回测引擎 | 完成 |
| 3 | 绩效指标 + CSV/HTML 报告 | 完成 |
| 4 | SMA Cross 双副本 | 完成 |
| 5 | RSI 双副本 | 完成 |
| 6 | 网格参数寻优 | 完成 |
| 7 | Webhook server | 完成 |
| 8 | Redis 队列 + 去重 | 完成 |
| 9 | SQLite 状态 | 完成 |
| 10 | 风控 | 完成 |
| 11 | Worker + 模拟执行 | 完成 |
| 12 | 测试报告 | 完成 |

M2 交付完成。下一步 M3：OKX 模拟盘下单、实时行情、真实 Redis 部署验证。
