# MS4-B 风控参数体系与执行可靠性 详细设计文档

> 文档版本：v1.0
> 创建日期：2026-10-30
> 状态：待实施
> 上游需求：[PRD v0.6](../requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md) §4.7（M7 风险控制）、§4.8（M8 交易执行）、§8.1（MS4 范围）
> 上游设计：[MS2 回测 + Webhook v1.1](2026-10-17-ms2-backtest-webhook-design.md) §10 风控、§9 Worker
> 相邻切片：[MS4-A 告警与首页](2026-10-30-ms4a-alerts-overview-design.md)（提供 `AlertService`）、[MS4-C 回测与策略](2026-10-30-ms4c-backtest-strategy-design.md)
> 覆盖条目：M7-4、M7-5、M7-10、M7-11、M7-15、M7-16、M8-14、M8-15、M8-16、M8-19

---

## 目录

1. [引言](#1-引言)
2. [现状与缺口](#2-现状与缺口)
3. [风控参数体系](#3-风控参数体系)
4. [风控事件与审计](#4-风控事件与审计)
5. [权益口径](#5-权益口径)
6. [kill switch 补齐撤单](#6-kill-switch-补齐撤单)
7. [订单状态机](#7-订单状态机)
8. [服务端仓位计算](#8-服务端仓位计算)
9. [下单幂等与重试边界](#9-下单幂等与重试边界)
10. [死信查看与重放](#10-死信查看与重放)
11. [存储设计](#11-存储设计)
12. [API 契约](#12-api-契约)
13. [配置扩展](#13-配置扩展)
14. [迁移与兼容](#14-迁移与兼容)
15. [错误处理与降级](#15-错误处理与降级)
16. [测试策略](#16-测试策略)
17. [实现顺序](#17-实现顺序)
18. [验收标准映射](#18-验收标准映射)
19. [风险与开放问题](#19-风险与开放问题)
20. [变更记录](#20-变更记录)

---

## 1. 引言

### 1.1 目的

MS4-B 把风控从「一张 toml 里的 5 个数字」升级为**可解释、可审计、可在线调整的三层参数体系**，并补齐执行侧的四个硬缺口：kill switch 不撤单、订单状态可回退、无服务端仓位计算、死信只能靠 `redis-cli` 看。

一句话概括本切片的价值：**当订单被拒绝时，能回答「是哪条规则、限值多少、当时权益多少、这个限值是谁改的」。**

### 1.2 范围

- **包含**：`RiskLimits` 三层合并与来源标注、`risk_events` 落库、`risk_overrides` 在线调整与审计、权益口径统一、kill switch 撤单、订单状态机、`PositionSizer`、下单幂等与重试、`dead_letters` 表与重放
- **不包含**：极端行情熔断（M7-6，MS5）、熔断恢复策略（M7-12）与职责分离（M7-13）、手工下单（M8-17）、三项对账（M8-18）、实盘接入（M8-7/8-8）

> 熔断（M7-6）与 kill switch 是两件事：前者按品种+自动冷却，后者全局+人工解除。本切片只做 kill switch，熔断留给 MS5，两者状态键必须分开（M7-13）。

---

## 2. 现状与缺口

| 现状 | 位置 | 缺口 |
|---|---|---|
| 风控只有 5 个参数，全部来自策略 toml `[risk]` | [risk/config.py](../../src/quant/risk/config.py) | 无全局默认、无在线覆盖、无来源标注 |
| 规则顺序：日亏损 → 连亏 → 单笔名义 → 单品种 → 总仓位 | [checker.py](../../src/quant/risk/checker.py) `check()` | 熔断不在 checker 内（由独立 kill switch 承担），文档需说明 |
| 权益取不到时退回 `backtest.initial_cash` | [run_worker.py](../../src/quant/cli/run_worker.py) `equity_provider` | 用配置的初始资金做真实下单判定，可能放大仓位 |
| 判定结果只在日志里 | `checker.py` | 无 `risk_events`，无法回答「昨天为什么拒了这单」 |
| worker 发布生效参数到 Redis，看板只读 | `worker._publish_risk_limits` | 只发布、不可写回，无法在线调整 |
| 看板 kill switch 只写标志 | [service.py](../../src/quant/dashboard/service.py)、[cli/kill_switch.py](../../src/quant/cli/kill_switch.py) | **挂单还留在交易所**，撤单只在 CLI `--on` 分支执行 |
| `update_order_status` 直接覆盖状态 | [sqlite_store.py](../../src/quant/state/sqlite_store.py) | 无状态机约束，可 `filled → pending` 回退；部分成交不累加 |
| `Signal.qty` 与 webhook `qty` 必填 | `backtest/signal.py`、`webhook/parser.py` | 无 `PositionSizer`，下单量全在策略侧算 |
| `clOrdId` 已由 `signal_id` 派生 | [okx_broker.py](../../src/quant/execution/okx_broker.py) `_cl_ord_id` | 幂等基础已有，但超时后仍可能直接重发 |
| 死信只在 Redis LIST | `queue/signal_queue.py`、`quant:signals:dead` | 无表、无查看、无重放、无原因分类 |

---

## 3. 风控参数体系

### 3.1 三层结构

```
优先级（后者覆盖前者）：
  ① 代码默认值             RiskConfig 的 field default
  ② 全局配置               config/config.toml  [risk]
  ③ 策略覆盖               config/strategies/{id}.toml  [risk]
  ④ 在线覆盖               risk_overrides 表（人工在页面改的）
```

取严规则（合并 ②③ 时）：

| 字段 | 取严方向 | 合并方式 |
|---|---|---|
| `max_position_pct` | 越小越严 | `min` |
| `max_total_pct` | 越小越严 | `min` |
| `max_order_qty`（名义价值） | 越小越严 | `min` |
| `daily_loss_limit` | 越小越严 | `min` |
| `consecutive_loss_limit` | 越小越严 | `min` |

> ④ 直接覆盖，不参与「取严」。理由：在线调整是人工决策，语义是「我明确要这个值」；若还和 toml 比 min，则「临时放宽」永远无效。**放宽属于高危操作，靠 `reason` 必填 + 审计留痕来约束，而不是靠取严悄悄否决。**

### 3.2 `RiskLimits`

```python
# src/quant/risk/limits.py

@dataclass(frozen=True, slots=True)
class RiskLimits:
    strategy_id: str
    max_position_pct: Decimal
    max_total_pct: Decimal
    max_order_qty: Decimal          # 名义价值（USDT），字段名沿用历史
    daily_loss_limit: Decimal
    consecutive_loss_limit: int
    applied: dict[str, str]         # 字段 → global / strategy / override

    def source(self, field: str) -> str:
        return self.applied.get(field, "default")
```

`resolve_limits(strategy_id, global_cfg, strategy_cfg, overrides)` 返回 `RiskLimits`，逐字段记录来源。

### 3.3 在线覆盖的读写

- **权威在 SQLite**：`risk_overrides` append-only（§11.2）。
- **热路径读 Redis**：worker 启动时把「当前生效覆盖」加载成 Redis hash：

```
quant:risk:overrides  →  { "sma_cross:max_position_pct": "0.2", ... }
```

无 TTL（覆盖是持久配置，不是瞬时状态）。看板写入时**先写库、再更新 Redis**，保证进程重启也能从库重建。

- **不得每次判定都查库**：1H bar 下每根一次尚可，但 1m 周期会变成每秒一次 DB 读。用 Redis hash 是刻意的。

### 3.4 覆盖值校验

写覆盖前必须校验，否则会出现 `max_position_pct = 5.0`（500% 仓位）这种能直接穿透风控的值：

| 字段 | 合法区间 |
|---|---|
| `max_position_pct` | `0 < v <= 1` |
| `max_total_pct` | `0 < v <= 3` |
| `max_order_qty` | `> 0` |
| `daily_loss_limit` | `0 < v <= 1` |
| `consecutive_loss_limit` | 整数 `>= 1` |

非法值返回 400 并指出字段与允许区间（与 PRD M3-3 的表单校验口径一致）。

---

## 4. 风控事件与审计

### 4.1 何时落库

不是每次判定都写——`approved` 且未调整的记录量大且无价值。落库条件：

```python
should_log = (
    not decision.approved              # 拒绝
    or decision.adjusted_qty != signal.qty   # 减量
    or decision.halt                   # 触发硬停止
)
```

### 4.2 记录内容

```python
@dataclass(frozen=True, slots=True)
class RiskEvent:
    event_id: str            # sha1(signal_id|ts|rule)[:16]
    ts: int
    strategy_id: str | None
    inst_id: str
    signal_id: str
    decision: str            # adjusted / rejected / halted
    rule: str | None         # max_order_qty / max_position_pct / ...
    limit_name: str | None   # 该规则对应的参数名
    limit_value: Decimal | None
    actual_value: Decimal | None
    qty_before: Decimal
    qty_after: Decimal
    equity: Decimal | None   # 判定时使用的权益
    reason: str
```

`equity` 必须记下来，否则事后无法判断「是限值太严」还是「权益缩水导致百分比超限」。

### 4.3 与 `RiskDecision` 的对接

现有 `RiskDecision` 已有 `approved/adjusted_qty/reason/halt`，需补 `rule/limit_name/limit_value/actual_value` 四个字段，由各规则在返回时填。这是对 `checker.py` 的最小侵入改动：每个 `RiskDecision(...)` 返回值加 4 个关键字参数，其余逻辑不动。

### 4.4 与告警联动（M7-17，部分）

`halt=True` 时调用 `AlertService.publish`（MS4-A），`rule="risk_halt"`，按 `signal_id` 去重：

```python
if decision.halt and AlertService:
    await alert_service.publish(AlertEvent(
        rule="risk_halt", level="ERROR",
        message=f"{strategy_id} halted: {decision.reason}",
        ts=now, inst_id=signal.inst_id, strategy_id=strategy_id,
    ))
```

> M7-17 完整实现（熔断/对账差异也接入）归 MS5，本切片只接 `halt` 这一条。

---

## 5. 权益口径

### 5.1 问题

现状 `equity_provider` 在拿不到 monitor 快照时返回 `None`，`RiskChecker` 退回构造时的 `initial_equity`（即 `backtest.initial_cash`）。这意味着：**如果 monitor 挂了，worker 会按配置的初始资金（可能是 10 万）而不是真实权益（可能只剩 3 万）计算百分比上限，实际敞口放大 3 倍以上。**

### 5.2 新口径（M7-15）

```python
if equity is None:
    if not settings.risk.allow_initial_equity_fallback:
        return RiskDecision(
            approved=False, adjusted_qty=Decimal("0"),
            rule="equity_unavailable", reason="account snapshot unavailable",
            halt=False,          # 不是账户异常，是数据缺失
        )
    log.warning("risk equity fallback to initial_cash (explicitly allowed)")
```

配置项 `[risk] allow_initial_equity_fallback = false`（默认 false）。

为什么 `halt=False`：权益缺失是**数据链路问题**，不是账户风险事件；置 `halt` 会把整个策略停掉，而实际上 monitor 恢复后一个轮询周期就能自愈。拒绝开仓 + 告警即可。

### 5.3 快照新鲜度校验

不只判断「有没有」，还要判断「新不新」：

```python
SNAPSHOT_MAX_AGE_MS = 3 * 60 * 1000      # 3 个采集周期

snapshot_ts = int(payload.get("ts") or 0)
if now - snapshot_ts > SNAPSHOT_MAX_AGE_MS:
    return None          # 视为不可用，走 §5.2 的拒绝路径
```

否则 monitor 卡死后，键还在 TTL 内但数值已经过时，仍会被当作有效权益。

---

## 6. kill switch 补齐撤单

### 6.1 问题

看板 `POST /api/kill-switch` 只写标志位，不撤单。用户按下「紧急停止」后，交易所里的挂单仍在，价格波动仍会成交。这是本次复核发现的**安全问题**，也是 M7-4 从 ✅ 降为 🟡 的原因。

### 6.2 为什么不让 dashboard 直接撤单

dashboard 进程当前**没有 OKX 私有客户端**，只有只读 Redis + SQLite。让它持有交易密钥会：

- 扩大密钥暴露面（多一个常驻进程能下单）
- 与「dashboard 只读」的架构约束冲突

### 6.3 命令通道方案

```
dashboard                          worker（持有 broker）
   │                                   │
   ├─ 1. 写 kill flag（同步）           │
   ├─ 2. LPUSH quant:kill:cancel-req   │
   │      {request_id, ts, reason}     │
   │                                   ├─ 3. 消费请求
   │                                   ├─ 4. cancel_all_open_orders()
   │                                   ├─ 5. SET quant:kill:cancel-result:{id}
   ├─ 6. 轮询结果 ≤ 2s ────────────────┘      {"ok":true,"canceled":3}
   └─ 7. 返回 {halted:true, cancel:"done"|"pending"|"failed"}
```

关键点：

- **第 1 步必须同步**：停止开仓是最高优先级，不能等 worker 轮询。标志位先落地，之后即使撤单失败，也不会继续下新单。
- **第 6 步超时不等于失败**：worker 可能正在处理其他信号，2 秒内没完成是正常的。返回 `cancel:"pending"`，前端显示「已停止开仓，撤单处理中」，并允许手动刷新查询。
- **重复点击幂等**：`request_id` 由 dashboard 生成，worker 侧记已处理集合（Redis `SETEX quant:kill:seen:{id} 600 1`）。

### 6.4 worker 侧实现

```python
async def _handle_kill_cancel(self, req: dict) -> None:
    rid = req["request_id"]
    if not await self.redis.set(f"quant:kill:seen:{rid}", 1, ex=600, nx=True):
        return                              # 已处理
    try:
        canceled = await self.broker.cancel_all_open_orders()
        result = {"ok": True, "canceled": canceled, "ts": now_ms()}
    except Exception as e:
        result = {"ok": False, "error": str(e), "ts": now_ms()}
    await self.redis.set(f"quant:kill:cancel-result:{rid}",
                         json.dumps(result), ex=300)
```

`cancel_all_open_orders()` 现不存在，需在 `OkxBroker` 新增：查 `/trade/orders-pending` 后逐单撤，或调用批量撤单接口。

---

## 7. 订单状态机

### 7.1 状态与迁移

```
                    submit
   (无) ──────────────────► pending
                              │
              ┌───────────────┼───────────────┬──────────────┐
              │               │               │              │
         remote live      rejected        expired        canceled
              ▼               ▼               ▼              ▼
            live         rejected        expired        canceled
              │
      ┌───────┴────────┐
      │                │
partially_filled    canceled
      │
      ├──────────► filled
      └──────────► canceled
```

允许的迁移（**单向，禁止回退**）：

| 从 | 到 |
|---|---|
| `pending` | `live` / `rejected` / `expired` / `canceled` / `filled` |
| `live` | `partially_filled` / `filled` / `canceled` / `expired` |
| `partially_filled` | `filled` / `canceled` |
| `filled` / `canceled` / `rejected` / `expired` | 终态，不可迁移 |

### 7.2 实现

```python
_TERMINAL = frozenset({"filled", "canceled", "rejected", "expired"})
_ALLOWED = {
    "pending": {"live", "rejected", "expired", "canceled", "filled"},
    "live": {"partially_filled", "filled", "canceled", "expired"},
    "partially_filled": {"filled", "canceled"},
}

def transition(current: str, target: str) -> bool:
    if current == target:
        return True                      # 幂等
    if current in _TERMINAL:
        return False                     # 终态不可变
    return target in _ALLOWED.get(current, set())
```

非法迁移**不抛异常**（对账时远端状态可能乱跳），记 WARNING 并保持原状态：

```python
if not transition(cur, new):
    log.warning(f"illegal transition {order_id}: {cur} -> {new}, ignored")
    return
```

### 7.3 部分成交累加

现有 `orders.filled_qty` / `filled_price` 是单值覆盖。改为累加：

```python
def apply_fill(self, order_id: str, fill_qty: Decimal, fill_price: Decimal) -> None:
    """累加成交并重算均价。"""
    row = self._get_order(order_id)
    prev_qty = Decimal(row["filled_qty"] or "0")
    prev_px = Decimal(row["filled_price"] or "0")
    total_qty = prev_qty + fill_qty
    avg_px = (
        (prev_qty * prev_px + fill_qty * fill_price) / total_qty
        if total_qty > 0 else fill_price
    )
```

> 这是**真实缺陷**而非重构：当前若两次部分成交，第二次会覆盖第一次，导致 `filled_qty` 偏小、均价错误，进而影响持仓与盈亏。

---

## 8. 服务端仓位计算

### 8.1 `PositionSizer`

```python
# src/quant/execution/sizing.py

@dataclass(frozen=True, slots=True)
class SizingResult:
    qty: Decimal
    notional: Decimal
    equity: Decimal
    capped_by: str | None        # max_order_qty / max_position_pct / lot_round / min_notional
    raw_qty: Decimal             # 取整前


class PositionSizer:
    def size(
        self,
        *,
        side: str,
        price: Decimal,
        equity: Decimal,
        size_pct: Decimal,          # 单笔目标占比，如 0.1
        spec: InstrumentSpec,       # lot_sz / min_sz / ct_val
        limits: RiskLimits,
    ) -> SizingResult: ...
```

计算链（每一步都可能截断，`capped_by` 记录首个触发项）：

```
raw      = equity * size_pct / price
by_risk  = min(raw, limits.max_order_qty / price)
by_lot   = floor(by_risk / lot_sz) * lot_sz        ← 必须向下取整
notional = by_lot * price
if by_lot < min_sz or notional < min_notional: 拒绝
```

### 8.2 与信号的关系

- 新增可选字段 `Signal.size_pct: Decimal | None`。
- `qty` 与 `size_pct` **二选一**：都给时以 `qty` 为准（显式优先），并记日志；都不给则拒绝入死信（`reason="no_qty_no_size_pct"`）。
- `qty` 仍保持「给了就必须 > 0」的现有校验，不放松。

### 8.3 审计

每次服务端算量写一条 `risk_events`（`decision="sized"`），带 `raw_qty`/`qty_after`/`capped_by`。这样「为什么这单只有 0.003 张」有据可查。

---

## 9. 下单幂等与重试边界

### 9.1 现状

`_cl_ord_id(signal)` 已由 `signal_id` 派生并截断到 32 位，交易所侧幂等的基础已具备。但**代码里没有「超时后先查后重试」的逻辑**，网络超时时存在重复下单风险。

### 9.2 规则

```python
async def submit_with_idempotency(signal, order) -> Order:
    for attempt in range(3):
        try:
            return await self._submit(signal, order)
        except TimeoutError:
            # 关键：不直接重发，先按 clOrdId 查
            existing = await self.broker.get_order_by_cl_ord_id(
                signal.inst_id, _cl_ord_id(signal)
            )
            if existing is not None:
                log.warning(f"submit timeout but order exists: {existing['ordId']}")
                return existing
            if attempt == 2:
                raise
            await asyncio.sleep(1.0 * (attempt + 1))
```

三条不可妥协的约束：

1. **超时后禁止直接重发**，必须先查 `clOrdId`。
2. **重试上限 3 次**，超过则入死信并告警（`rule="submit_failed"`）。
3. **`clOrdId` 生成必须稳定**：同一 `signal_id` 无论重试几次都是同一字符串。现有实现满足，但需要单测锁定——若将来改成「每次随机」，幂等立即失效且无报错。

### 9.3 交易所侧幂等的行为

OKX 对重复 `clOrdId` 返回错误码 `51016`（`Duplicated clOrdId`）。收到该错误码应视为「已成交」，转而查询订单，而不是当作失败重试。

---

## 10. 死信查看与重放

### 10.1 落库

死信当前只 `LPUSH quant:signals:dead`。改为**双写**（Redis 保留给 monitor 计数告警，SQLite 作为可查询的权威记录）：

```python
await self._redis.lpush(self.dead_key, payload)      # 兼容 monitor
await asyncio.to_thread(self.store.insert_dead_letter, dl)   # 权威
```

`stage` 取值：`parse`（webhook 解析失败）/ `risk`（风控拒绝且不可重试）/ `submit`（下单失败超限）/ `reconcile`（对账发现异常）。

> 注意：**风控拒绝不该进死信**。风控拒绝是正常业务结果（该拒就拒），重放它还会再被拒一次。只有「本来该成功但因技术原因失败」的才进死信。

### 10.2 重放

```python
async def replay(self, dead_id: str) -> str:
    dl = self.store.get_dead_letter(dead_id)
    if dl["replayed_at"]:
        return "already_replayed"

    payload = json.loads(dl["payload"])
    signal_id = payload.get("signal_id")
    # 幂等：已成功处理的信号不再重放
    if signal_id and self.store.signal_exists(signal_id):
        state = self.store.get_signal_status(signal_id)
        if state in ("done", "filled"):
            return "already_processed"

    await self.queue.push(payload)          # 回 pending
    self.store.mark_replayed(dead_id, "requeued")
    return "requeued"
```

重放前幂等检查是必须的：否则「下单成功但写状态时崩溃」的死信被重放，会变成真的下两次单（`clOrdId` 相同，交易所会拒，但仍是一次多余请求）。

---

## 11. 存储设计

沿用 MS4-A 建立的 `PRAGMA user_version` 迁移机制。**版本号顺序**：MS4-A 占 v2/v3，本切片占 v4/v5，MS4-C 占 v6。

### 11.1 `risk_events`（v4）

```sql
CREATE TABLE IF NOT EXISTS risk_events (
    event_id    TEXT PRIMARY KEY,
    ts          INTEGER NOT NULL,
    strategy_id TEXT,
    inst_id     TEXT NOT NULL,
    signal_id   TEXT NOT NULL,
    decision    TEXT NOT NULL,      -- adjusted / rejected / halted / sized
    rule        TEXT,
    limit_name  TEXT,
    limit_value TEXT,
    actual_value TEXT,
    qty_before  TEXT,
    qty_after   TEXT,
    equity      TEXT,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_risk_events_ts ON risk_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_risk_events_rule ON risk_events(rule, ts DESC);
CREATE INDEX IF NOT EXISTS idx_risk_events_signal ON risk_events(signal_id);
```

### 11.2 `risk_overrides`（v4）

**一张表同时承担「当前覆盖」和「变更审计」**（M7-8 + M7-14），append-only：

```sql
CREATE TABLE IF NOT EXISTS risk_overrides (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    param       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT NOT NULL,
    reason      TEXT,
    source      TEXT NOT NULL,      -- dashboard / cli
    changed_by  TEXT NOT NULL,
    changed_at  INTEGER NOT NULL,
    revoked_at  INTEGER             -- 清除覆盖的时间；NULL=生效中
);
CREATE INDEX IF NOT EXISTS idx_overrides_effective
    ON risk_overrides(strategy_id, param, revoked_at);
```

当前生效值 = 每个 `(strategy_id, param)` 下 `revoked_at IS NULL` 的**最新一行**的 `new_value`。

```sql
SELECT o.* FROM risk_overrides o
JOIN (
    SELECT strategy_id, param, MAX(id) AS max_id
      FROM risk_overrides
     WHERE revoked_at IS NULL
     GROUP BY strategy_id, param
) t ON o.id = t.max_id;
```

「一键清除覆盖」= `UPDATE ... SET revoked_at=? WHERE strategy_id=? AND param=? AND revoked_at IS NULL`，同时写一条 `new_value` 回落到配置值的审计行（`source='dashboard'`, `reason='clear override'`）。

> 为什么不拆成 `risk_overrides` + `risk_override_audit` 两张表：PRD M7-14 要求的字段（改前/改后/时间/原因/来源）与 M7-8 的「当前覆盖」完全重合，拆表会让「当前值」和「历史」出现两个真相源，反而更容易不一致。

### 11.3 `dead_letters`（v5）

```sql
CREATE TABLE IF NOT EXISTS dead_letters (
    dead_id     TEXT PRIMARY KEY,
    signal_id   TEXT,
    stage       TEXT NOT NULL,        -- parse / submit / reconcile
    reason      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    replayed_at INTEGER,
    replay_result TEXT                -- requeued / already_processed / failed
);
CREATE INDEX IF NOT EXISTS idx_dead_created ON dead_letters(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dead_stage ON dead_letters(stage, created_at DESC);
```

---

## 12. API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/risk-limits` | 扩展：每个字段附 `source`；新增 `overrides[]` |
| POST | `/api/risk-limits/override` | 设覆盖：`{strategy_id, param, value, reason, by}`；校验区间（§3.4） |
| DELETE | `/api/risk-limits/override/{strategy_id}/{param}` | 清除覆盖，回落配置值 |
| GET | `/api/risk-events` | 参数 `limit/offset/rule/decision/strategy_id/since/until`，返回 `{total, events[]}` |
| GET | `/api/risk-events/stats` | 今日各规则命中次数与最近一次详情（供 M7-9 页面） |
| POST | `/api/kill-switch` | 扩展 body `{reason, cancel_orders: true}`；返回 `{halted, cancel: done\|pending\|failed}` |
| GET | `/api/dead-letters` | 参数 `limit/offset/stage/since`，返回 `{total, items[]}`（含 `payload` 摘要，不含完整密钥类字段） |
| POST | `/api/dead-letters/{dead_id}/replay` | 重放，返回 `requeued / already_processed` |
| POST | `/api/dead-letters/replay` | 批量重放 body `{ids: []}`，逐条返回结果 |

写接口统一走 `_guard_write`。`POST /api/kill-switch` 保留二次确认语义（前端弹窗），但**服务端不依赖前端确认**——`reason` 必填，长度 >= 4，避免误触。

### 12.1 `/api/risk-limits` 响应变化

```json
{
  "ts": 1793374800000,
  "strategy_id": "sma_cross",
  "limits": {
    "max_position_pct": {"value": "0.2", "source": "override"},
    "max_total_pct":    {"value": "0.8", "source": "global"},
    "max_order_qty":    {"value": "10000", "source": "strategy"},
    "daily_loss_limit": {"value": "0.05", "source": "global"},
    "consecutive_loss_limit": {"value": 3, "source": "default"}
  },
  "overrides": [
    {"param": "max_position_pct", "old": "0.3", "new": "0.2",
     "reason": "降低单品种敞口", "by": "owner", "at": 1793370000000}
  ],
  "used": {"max_position_pct": "0.08"},
  "degraded": []
}
```

---

## 13. 配置扩展

```toml
[risk]
# 全局默认（策略 toml 的 [risk] 与之取严）
max_position_pct = 0.3
max_total_pct = 0.8
max_order_qty = 10000
daily_loss_limit = 0.05
consecutive_loss_limit = 3
# 新：权益缺失时是否允许退回 initial_cash
allow_initial_equity_fallback = false
# 新：快照最大可接受年龄（毫秒），超龄视为不可用
snapshot_max_age_ms = 180000

[execution]
# 新：是否由服务端计算数量（Signal.qty 可省略）
server_side_sizing = true
# 新：单笔目标占比（未给 size_pct 时使用）
default_size_pct = 0.1
# 新：下单重试上限
submit_max_attempts = 3
# 复用现有
order_timeout_seconds = 0
```

---

## 14. 迁移与兼容

| 阶段 | 动作 | 回滚 |
|---|---|---|
| 1 | v4 建 `risk_events` / `risk_overrides` | 删两表 |
| 2 | `RiskLimits` 三层合并 + `source` 标注（只读，不改判定结果） | 回退 resolver，行为不变 |
| 3 | `RiskDecision` 补 4 字段 + `risk_events` 落库（只写不读） | 关开关 `risk.event_log = false` |
| 4 | 权益口径改严（`allow_initial_equity_fallback=false`） | 置 true 退回旧行为 |
| 5 | v5 建 `dead_letters` + 双写 | 关 `dead_letter_persist` |
| 6 | 订单状态机 + 成交累加 | **不可回滚**（数据修正），必须先在备份库验证 |
| 7 | `PositionSizer` + `Signal.size_pct` | 关 `server_side_sizing`，回到 qty 必填 |
| 8 | kill switch 撤单 + 幂等重试 | 关 `kill_switch.cancel_orders` |

**第 6 步是唯一不可回滚项**，实施前必须先跑一次「历史订单重放」验证：把现有 `orders` 表按新状态机重算，确认无非法迁移（预计不会有，因为现状只有 `pending/filled/canceled` 三态）。

---

## 15. 错误处理与降级

| 故障 | 行为 |
|---|---|
| Redis 不可用 | 覆盖值读不到 → 回落 SQLite 读（慢但正确）；`risk_events` 照常落库 |
| SQLite 不可用 | 覆盖值读 Redis；`risk_events` 写失败记 ERROR，不阻断下单 |
| 权益快照缺失/超龄 | 拒绝开仓，`rule=equity_unavailable`，告警一次（冷却 600s） |
| 覆盖值非法 | 写入前拒绝 400；**读取时也校验**（防手工改库写入脏值），非法则忽略该覆盖并告警 |
| kill 撤单请求超时 | 返回 `cancel:"pending"`，不谎报成功；前端可轮询 |
| 状态机遇到非法迁移 | 记 WARNING 并保持原状态，不抛异常（对账场景常见） |
| 死信重放时信号已处理 | 返回 `already_processed`，不重复入队 |

---

## 16. 测试策略

### 16.1 必须用单测固定的边界（M7-16）

每条规则都要覆盖这 5 类：

| 场景 | 说明 |
|---|---|
| 恰好等于阈值 | `notional == max_order_qty` → 应**放行**（限值是上限含等于） |
| 略超阈值 | `notional = max_order_qty + 1` → 应下调 |
| 无可用价格 | `market_price=None` 且无持仓均价 → 跳过名义价值类检查并放行 |
| 空持仓 | 首次开仓，`position.qty == 0` |
| 权益为 0 / 负 | 不得出现除零，不得放大仓位 |
| 多规则同时命中 | 优先级：日亏损 > 连亏 > 单笔 > 单品种 > 总仓位，验证**返回的是第一条命中的规则** |

### 16.2 其他

| 层 | 用例 |
|---|---|
| 三层合并 | 全局 0.3 + 策略 0.2 → 生效 0.2 且 `source=strategy`；覆盖 0.5 → 生效 0.5 且 `source=override`（**放宽不被取严否决**） |
| 覆盖清理 | 清除后回落策略值；审计行 `new_value` 等于回落值 |
| 覆盖校验 | 6 个非法值各被拒；读库脏值被忽略并告警 |
| `risk_events` | 拒绝/减量/halt 各落一条；纯放行不落；`equity` 字段正确 |
| 权益口径 | 快照缺失 → 拒绝；快照超龄 → 拒绝；开关为 true → 退回并 WARNING |
| 状态机 | 11 条合法迁移通过；4 类非法迁移被拒且状态不变；重复同状态幂等 |
| 成交累加 | 两次部分成交 → `filled_qty` 为和、均价为加权平均 |
| `PositionSizer` | `lot_sz` 向下取整不多不少；低于 `min_sz` 拒绝；`capped_by` 正确 |
| 幂等重试 | 超时后先查后重发；查到已存在则不重发；3 次后入死信；`51016` 视为已成交 |
| 死信 | 双写一致；重放已处理信号返回 `already_processed`；批量重放逐条独立 |

**耦合测试**：`checker` 改动后必须重跑 `tests/test_worker.py` 与 `tests/test_risk*.py` 全量，因为 `RiskDecision` 新增 4 个字段会影响所有构造点。

---

## 17. 实现顺序

1. **v4 迁移 + `risk_events` / `risk_overrides` 表 + store 方法**
2. **`RiskLimits` 三层合并 + `applied` 来源**（只读，不改判定）
3. **`RiskDecision` 补字段 + 各规则填值 + `risk_events` 落库**
4. **权益口径改严**（默认拒绝，配置可退回）
5. **覆盖写入 API + 校验 + Redis 同步 + 审计**
6. **`/api/risk-limits` 扩展 + 风控页展示来源与今日触发**
7. **订单状态机 + 成交累加**（不可回滚，先备份验证）
8. **v5 + `dead_letters` 双写 + 查看/重放 API + 页面**
9. **`PositionSizer` + `Signal.size_pct` + `server_side_sizing`**
10. **幂等重试 + `51016` 处理**
11. **kill switch 撤单命令通道**

第 3 步之后就能回答「为什么拒了这单」，是投入产出比最高的一步。

---

## 18. 验收标准映射

| 条目 | 验收信号 | 落点 |
|---|---|---|
| M7-4 kill switch | 看板点击后交易所挂单被撤；返回 `cancel` 状态且不谎报 | §6 |
| M7-5 实时风险指标 | 已实现，本切片不改口径 | — |
| M7-10 两层参数合并 | 每字段有 `source`，页面可解释来源 | §3 |
| M7-11 风控事件落库 | 拒绝/减量/halt 均可查，含限值、实际值、权益 | §4 |
| M7-15 权益口径统一 | 快照缺失时拒绝而非退回初始资金 | §5 |
| M7-16 规则边界单测 | 6 类边界 × 5 条规则全覆盖 | §16.1 |
| M8-14 服务端仓位计算 | `qty` 可省略，按 `size_pct` 算并向下取整；审计记录 | §8 |
| M8-15 订单状态机 | 单向推进，非法迁移被拒，部分成交累加 | §7 |
| M8-16 死信查看与重放 | 列表可按 stage 筛；重放有幂等检查 | §10 |
| M8-19 下单幂等与重试 | 超时先查后发；上限 3 次；`clOrdId` 稳定 | §9 |

---

## 19. 风险与开放问题

### 19.1 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 订单状态机改动影响历史数据 | 持仓/盈亏计算偏差 | 先在备份库重放验证；不可回滚项单独提交 |
| kill 撤单依赖 worker 存活 | worker 死了撤不掉 | 标志位已同步落地（不再开新仓）；monitor 心跳告警；CLI 仍可手动撤 |
| 覆盖放宽失控 | 风控形同虚设 | `reason` 必填、审计留痕、区间校验；放宽上限不超过配置值的 2 倍（待定，见开放问题） |
| `risk_events` 增长 | 查询变慢 | 只在拒绝/减量/halt 落库；保留期由 `retention` 覆盖 |
| 权益改严后误伤 | monitor 短暂抖动即拒单 | 快照年龄阈值 3 个周期；拒绝不置 halt，恢复即自愈 |

### 19.2 开放问题

| 问题 | 决策时机 |
|---|---|
| 覆盖值是否设「相对配置值的上限」（如不超过 2×） | 实施时定；倾向**设上限**，否则一次误操作即可穿透 |
| 是否记录「放行」的抽样（如 1%）用于统计 | 暂不；需要时再加 |
| `cancel_all_open_orders` 用 REST 批量撤还是逐单撤 | 实施时看 OKX 批量撤单接口限制 |
| 死信 payload 可能含密钥类字段，是否需要脱敏 | 实施时确认 parser 不落原始 header |
| `server_side_sizing` 与策略侧算量并存期如何提示 | 前端在信号详情标注 `qty_source` |

---

## 20. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-30 | 初版。风控三层参数（全局/策略/覆盖）与来源标注；`risk_events` 审计；`risk_overrides` 单表承担当前值与变更历史；权益口径改为缺失即拒绝；kill switch 经命令通道撤单；订单状态机与成交累加（修复真实缺陷）；`PositionSizer`；下单幂等与重试边界；`dead_letters` 落库与重放 |
