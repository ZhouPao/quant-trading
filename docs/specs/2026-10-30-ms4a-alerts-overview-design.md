# MS4-A 告警持久化与首页总览 详细设计文档

> 文档版本：v1.1
> 创建日期：2026-10-30
> 状态：待实施
> 上游需求：[PRD v0.6](../requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md) §4.1（M1 首页总览）、§4.9（M9 日志告警）、§8.1（MS4 范围）
> 上游设计：[MS1 数据底座 v1.2](2026-10-17-ms1-data-foundation-design.md)、[MS2 回测 + Webhook v1.1](2026-10-17-ms2-backtest-webhook-design.md)
> 覆盖条目（按 PRD §8.1 的 MS4 排期）：M1-1 ~ M1-7、M1-10、M9-1 ~ M9-5、M9-8、M9-9、M9-11、M9-13
> 同批设计但 PRD 排在 MS5：M1-9（告警摘要卡）、M9-7（确认与静音）、M9-10（静音粒度）—— 这三条所需的 `alerts.state` / `alert_silences` 表与 ack/silence 接口在本切片一并给出，否则 M9-8 建出来的表无人使用；实施可随本切片或后移至 MS5
> 不在本文档范围：M9-12（按级别区分保留天数，见 §12）

> **编号约定**：本文档的 **MS4-A** 是里程碑切片编号；**M1/M9** 是 PRD 模块编号。

---

## 目录

1. [引言](#1-引言)
2. [现状与缺口](#2-现状与缺口)
3. [总体设计](#3-总体设计)
4. [存储设计](#4-存储设计)
5. [AlertService 设计](#5-alertservice-设计)
6. [告警规则与冷却](#6-告警规则与冷却)
7. [进程健康](#7-进程健康)
8. [事件流](#8-事件流)
9. [数据新鲜度](#9-数据新鲜度)
10. [API 契约](#10-api-契约)
11. [前端改造](#11-前端改造)
12. [配置扩展](#12-配置扩展)
13. [迁移与兼容](#13-迁移与兼容)
14. [错误处理与降级](#14-错误处理与降级)
15. [测试策略](#15-测试策略)
16. [实现顺序](#16-实现顺序)
17. [验收标准映射](#17-验收标准映射)
18. [风险与开放问题](#18-风险与开放问题)
19. [变更记录](#19-变更记录)

---

## 1. 引言

### 1.1 目的

MS4-A 解决两个问题：

1. **告警会丢**。当前告警只写 Redis list（`quant:dashboard:alerts`，`LTRIM` 保留 200 条），Redis 重启或 list 溢出即永久丢失，无法复盘「昨天 14:00 到底报过什么」。
2. **首页缺三个视图**。进程健康只有 worker/monitor 两个真心跳；没有跨模块的今日事件流；没有「引擎数据到底落后几根」的量化指标。

### 1.2 范围

- **包含**：`alerts` / `alert_silences` 两张表、`AlertService`、告警落库与确认静音、7 进程心跳、事件流、数据新鲜度、首页降级标注
- **不包含**：日志检索（M9-6，🚫 本期不做）、告警趋势统计图表（M9-14 归 MS5）、按级别区分保留天数（M9-12 归 MS5）、邮件正文模板改版（M9-3 已在 MS2 完成，本切片只补发信前置的静音判断）

### 1.3 设计原则

| 原则 | 落地 |
|---|---|
| SQLite 是权威，Redis 是缓存 | 告警先落库再推 Redis；Redis 挂掉只影响首屏速度，不影响数据 |
| 静音只压通知，不压留痕 | 静音期内告警仍落库并标 `suppressed=1`，事后能查到「报了但被静音」 |
| 缺失必须显式 | 任何数据源失败都进 `degraded[]` 并在 UI 标「数据截至 HH:MM」，禁止渲染成 0 或 `—` |
| 读路径不回写 | 首页所有接口只读，不触发落库、不触发拉取 |

---

## 2. 现状与缺口

以下为 2026-10-30 实测结论，是本节设计的出发点。

| 现状 | 位置 | 缺口 |
|---|---|---|
| 告警只进 Redis LIST，`keep=200` | [monitor.py](../../src/quant/monitor/monitor.py) `_publish_alerts` | 无持久化、无确认、无静音、无时间区间查询 |
| `Alert` 只有 `rule/level/message/ts` | [alerter.py](../../src/quant/monitor/alerter.py) | 没有 `alert_id`、没有对象维度（品种/策略）、无法幂等 |
| 冷却按 `rule` 记在进程内存 `_alert_last_published` | [monitor.py](../../src/quant/monitor/monitor.py) | monitor 重启后冷却窗口丢失，可能立即重报 |
| 只有 worker/monitor 写心跳 | `quant:heartbeat:worker` / `:monitor` | candles/webhook/dashboard/supervisor 无心跳；engine 用自己的 `quant:engine:status:*` |
| `heartbeat` 表存在但从未被调用 | [sqlite_store.py](../../src/quant/state/sqlite_store.py) `save_heartbeat` | 死代码，需决定删除还是启用 |
| `/api/alerts` 只读 Redis | [service.py](../../src/quant/dashboard/service.py) `alerts()` | 无分页、无时间区间、无状态筛选 |
| 现有 SQLite 6 张表，无版本号 | `_SCHEMA` | 无 `PRAGMA user_version`，加表/加索引无迁移机制 |

**结论**：本切片不是「加几个接口」，而是要把告警从「内存 + Redis 的瞬时结构」升级为「有 ID、有状态、可查询、可静音的持久实体」，同时顺手建立 SQLite 迁移机制——否则后续每个切片都要面对「老库怎么加列」。

---

## 3. 总体设计

```
                    ┌──────────────────────────────┐
                    │ monitor 轮询（interval 60s）   │
                    │  ├ 11 条规则评估              │
                    │  ├ 状态跃迁（命中/恢复）       │
                    │  └ 冷却判定（M9-9）           │
                    └───────────┬──────────────────┘
                                │ AlertService.publish()
                    ┌───────────▼──────────────────┐
                    │ 1. 静音匹配 → suppressed?     │
                    │ 2. INSERT OR IGNORE alerts    │  ← 权威
                    │ 3. LPUSH quant:dashboard:alerts│  ← 缓存
                    │ 4. 未静音 → EmailAlerter.send  │
                    └───────────┬──────────────────┘
                                │
        ┌───────────────────────┼────────────────────────┐
        │                       │                        │
┌───────▼───────┐    ┌──────────▼──────────┐   ┌─────────▼─────────┐
│ /api/alerts   │    │ /api/processes      │   │ /api/timeline     │
│ 读 SQLite     │    │ 聚合 7 心跳键        │   │ 4 表 UNION        │
│ +ack/silence  │    │ + supervisor 进程表  │   │ + /api/freshness  │
└───────────────┘    └─────────────────────┘   └───────────────────┘
```

三条数据流互不阻塞：告警落库失败不影响监控主循环（monitor 已有 try/except 包裹），心跳缺失不影响告警，事件流读不到某张表只让该类事件缺席并在 `degraded` 标注。

---

## 4. 存储设计

### 4.1 MVP 迁移机制（本切片新增）

现有 `_SCHEMA` 用 `executescript` 幂等建表，但加索引/加列无法表达。引入最小迁移：

```python
# 约定：现有 _SCHEMA（executescript 幂等建表）视为 v1 基线。
# _MIGRATIONS[i] 对应「升到版本 i+2」，故枚举从 2 开始。
_MIGRATIONS: list[str] = [
    # v1 → v2：为 alerts / alert_silences 建表（MS4-A）
    """
    CREATE TABLE IF NOT EXISTS alerts (...);
    CREATE TABLE IF NOT EXISTS alert_silences (...);
    CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);
    ...
    -- 删除从未被调用的死表（§4.4）
    DROP TABLE IF EXISTS heartbeat;
    """,
    # v2 → v3：为事件流 UNION 补索引（MS4-A）
    """
    CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
    CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_ts);
    CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
    """,
]

def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)          # 幂等；老库也会安全重放
    cur = conn.execute("PRAGMA user_version")
    version = int(cur.fetchone()[0])
    for target, script in enumerate(_MIGRATIONS, start=2):
        if version >= target:
            continue
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {target}")
        conn.commit()
```

约束：

- **版本号起点是 2，不是 1**。`_SCHEMA` 就是 v1；若从 1 开始枚举，第一段脚本会被当作 v1 而跳过（老库 `user_version=0` 时行为正确，但一旦有人在 v2 之后新增脚本，整体会错位一格）。MS4-B 的 v4/v5、MS4-C 的 v6 都依赖这个偏移，必须对齐。
- 每个迁移脚本必须**幂等**（`IF NOT EXISTS`），因为 SQLite 的 DDL 与 `user_version` 写入不在同一事务里，中途崩溃会重放。
- 迁移只加不删；删列需新建表 + 数据搬迁，本切片不涉及。`DROP TABLE heartbeat` 是删整表，属于例外（表已确认无调用方）。
- 老库（`user_version=0` 但已有 6 张表）会先跑 `_SCHEMA`（全是 `IF NOT EXISTS`，无副作用），再从 v2 依次跑到最新。

### 4.2 `alerts` 表

```sql
CREATE TABLE IF NOT EXISTS alerts (
    alert_id     TEXT PRIMARY KEY,          -- sha1(rule|fingerprint|ts)[:16]
    rule         TEXT NOT NULL,             -- 规则名，如 engine_heartbeat
    level        TEXT NOT NULL,             -- INFO / WARNING / ERROR
    message      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,             -- rule + 归一化 message，用于聚合同类
    inst_id      TEXT,                      -- 对象维度，可空
    strategy_id  TEXT,                      -- 对象维度，可空
    state        TEXT NOT NULL DEFAULT 'open',  -- open / acked / resolved
    suppressed   INTEGER NOT NULL DEFAULT 0,    -- 1=命中静音，仍留痕
    created_at   INTEGER NOT NULL,
    acked_at     INTEGER,
    acked_by     TEXT,
    resolved_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_rule ON alerts(rule, created_at DESC);
```

字段决策：

| 字段 | 为什么需要 |
|---|---|
| `alert_id` 而非自增 | monitor 可能重发同一轮告警（进程重启后内存冷却丢失），用确定性 ID + `INSERT OR IGNORE` 天然幂等 |
| `created_at` 存的是**事件的 `event.ts`，不是落库时刻** | `alert_id` 由 `rule\|fingerprint\|ts` 派生，若 `created_at` 用 `now()`，同一行就再也反推不出自己的 ID，且监控轮次与落库时刻的偏差会让事件流排序失真。字段名沿用 MS1 的习惯（`created_at`），含义以本条为准 |
| `fingerprint` | 把 `消息里的数字` 归一化后做哈希，便于「同一规则同一对象」聚合；**不用于去重**，去重靠冷却窗口 |
| `suppressed` | 静音期内仍落库，用该列区分「报了但没通知」，复盘时才看得出静音是否掩盖了真问题 |
| `state` | `open` → `acked`（人工确认）→ `resolved`（恢复类 INFO 到达或人工关闭） |
| 无 `deleted_at` | 删除走保留清理（M9-12），不做软删 |

`fingerprint` 归一化规则：

```python
_NUM = re.compile(r"-?\d+(?:\.\d+)?")

def fingerprint(rule: str, message: str) -> str:
    """把消息里的数字与空白归一，便于同类聚合。"""
    return hashlib.sha1(
        f"{rule}|{_NUM.sub('#', message).strip()}".encode()
    ).hexdigest()[:16]
```

> 例：`engine heartbeat stale 245s` 与 `engine heartbeat stale 301s` 归一为同一指纹，但仍是两条独立记录（`created_at` 不同 → `alert_id` 不同）。

### 4.3 `alert_silences` 表

```sql
CREATE TABLE IF NOT EXISTS alert_silences (
    silence_id  TEXT PRIMARY KEY,
    rule        TEXT NOT NULL,           -- '*' 表示所有规则
    inst_id     TEXT,                    -- NULL 表示该规则下所有品种
    until_ts    INTEGER NOT NULL,
    reason      TEXT,
    created_by  TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    revoked_at  INTEGER                  -- 手动解除时间，NULL=未解除
);
CREATE INDEX IF NOT EXISTS idx_silences_active ON alert_silences(revoked_at, until_ts);
```

匹配规则（M9-10 三种粒度）：

```python
def match(silences: list[Row], rule: str, inst_id: str | None, now: int) -> Row | None:
    for s in silences:
        if s["revoked_at"] is not None or s["until_ts"] <= now:
            continue
        if s["rule"] not in ("*", rule):
            continue
        if s["inst_id"] is not None and s["inst_id"] != inst_id:
            continue
        return s            # 任一命中即静音
    return None
```

- `rule='*'` + `inst_id=NULL` → 全局临时静音
- `rule='margin_ratio'` + `inst_id=NULL` → 该规则所有品种
- `rule='margin_ratio'` + `inst_id='BTC-USDT-SWAP'` → 单品种

### 4.4 `heartbeat` 表处置

现有表结构 `(process TEXT PRIMARY KEY, ts INTEGER)`；`save_heartbeat/get_heartbeat` 在**生产代码中**没有任何调用方（只有单测引用）。**决策：不启用，直接删除**。理由：

- 心跳是「秒级覆盖」数据，写 SQLite 每 30 秒一次 × 7 进程 = 每天 2 万行写入，与「SQLite 只存业务状态」的分工冲突。
- Redis 键 + TTL 本身就是「过期即离线」的正确语义，多此一举。
- 删表需迁移；本切片在 v2 迁移中执行 `DROP TABLE IF EXISTS heartbeat`，并删除两个未调用的方法。

> 注意：`tests/test_sqlite_store.py` 里 `TestHeartbeat` 的 3 个用例，以及 `TestSchemaCreation.test_reopen_existing_db` 里的一处调用，都依赖这两个方法（[test_sqlite_store.py](../../tests/test_sqlite_store.py#L194-L227)）。删方法时必须同步处理，否则测试会以 `AttributeError` 失败——「方法没有调用方」不等于「没有引用方」。

---

## 5. AlertService 设计

### 5.1 接口

```python
# src/quant/monitor/alert_service.py

@dataclass(frozen=True, slots=True)
class AlertEvent:
    rule: str
    level: str                     # INFO / WARNING / ERROR
    message: str
    ts: int
    inst_id: str | None = None
    strategy_id: str | None = None


class AlertService:
    def __init__(
        self,
        store: SQLiteStore,
        redis: Redis,
        alerter: EmailAlerter,
        settings: MonitorSettings,
    ) -> None: ...

    async def publish(self, event: AlertEvent) -> str:
        """落库 → 推 Redis → 按静音决定是否发邮件。返回 alert_id。"""

    def list(
        self,
        limit: int = 50,
        *,
        level: str | None = None,
        rule: str | None = None,
        state: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """返回 (分页结果, 命中总数)。全部走 SQLite。"""

    async def ack(self, alert_id: str, by: str) -> bool:
        """标记已读；幂等，已 acked 再调返回 True 不覆盖首次时间。"""

    async def resolve(self, rule: str, *, inst_id: str | None) -> int:
        """把该规则（+品种）下所有 open/acked 记录置 resolved，返回影响行数。

        由 monitor 的 `_with_recovery` 在发出恢复 INFO 时调用（§5.3）。
        """

    async def silence(
        self, rule: str, *, inst_id: str | None,
        hours: float, reason: str, by: str,
    ) -> str:
        """创建静音记录，返回 silence_id。"""

    async def revoke_silence(self, silence_id: str) -> bool: ...

    def active_silences(self, now_ms: int) -> list[dict]: ...
```

### 5.2 `publish` 执行顺序

```python
async def publish(self, event: AlertEvent) -> str:
    key = self._settings.dashboard_alerts_key       # 现有键名
    keep = self._settings.dashboard_alert_keep      # 现有键名，默认 200

    fp = fingerprint(event.rule, event.message)
    alert_id = hashlib.sha1(
        f"{event.rule}|{fp}|{event.ts}".encode()
    ).hexdigest()[:16]

    silence = self._match_silence(event.rule, event.inst_id, event.ts)
    suppressed = silence is not None and event.level != "INFO"

    # 1) 权威落库（失败不抛给调用方，monitor 主循环不能被告警拖死）
    try:
        await asyncio.to_thread(
            self.store.insert_alert, alert_id, event, fp, suppressed
        )
    except Exception as e:
        log.error(f"alert persist failed id={alert_id}: {e}")

    # 2) 实时缓存（失败只影响首屏）
    try:
        await self.redis.lpush(key, json.dumps(payload, ensure_ascii=False))
        await self.redis.ltrim(key, 0, keep - 1)
    except Exception as e:
        log.warning(f"alert cache push failed: {e}")

    # 3) 通知（INFO 恢复通知不受静音约束）
    if not suppressed and self.alerter.enabled() and event.level != "INFO":
        await asyncio.to_thread(
            self.alerter.send, subject, body
        )
    return alert_id
```

三点刻意的设计：

1. **INFO 不受静音**：「已恢复」是好消息，静音期间更需要知道故障结束。
2. **落库失败不抛**：`publish` 的调用方是 monitor 主循环，任何告警系统的问题都不该让监控停摆。
3. **邮件从「批量聚合」改为「单条」**：现状是每轮把所有告警合成一封（`monitor.py` 的 `body = "\n".join(...)`）。改为单条是为了让 `suppressed` 精确到单条——混在一封里无法逐条判断静音。

> 代价：突发多条告警会发多封邮件。用冷却窗口（600s）兜住，同一规则不会连发。

### 5.3 状态机

```
       publish()                 ack()
  ───────────────► open ───────────────► acked
                    │                       │
                    │ resolve()             │ resolve()
                    ▼                       ▼
                 resolved ◄──────────────────┘
```

- `resolve()` 由 monitor 的恢复逻辑触发：`_with_recovery` 生成 `level=INFO` 的恢复告警时，同时把该 `rule`（+`inst_id`）下所有 `open`/`acked` 记录置为 `resolved`。
- 人工关闭本期不做（PRD 未要求），保留 `state` 枚举的扩展位。

### 5.4 Redis 键职责调整

| 键 | 之前 | 之后 |
|---|---|---|
| `quant:dashboard:alerts` | 唯一存储，keep 200 | **实时缓存**，keep 200，TTL 不需要（看板首屏） |
| `quant:dashboard:alert-cooldown:{rule}:{inst_id}` | 进程内存，仅按 `rule` | **改为 Redis**，`EX=cooldown_seconds`，monitor 重启不丢冷却，**且按品种区分** |

冷却迁移到 Redis 的写法：

```python
async def _in_cooldown(self, rule: str, inst_id: str | None, ts: int) -> bool:
    if rule in _NO_COOLDOWN_RULES:
        return False
    # inst_id 为 None 时统一写成 '-'，避免键里出现空段
    key = f"quant:dashboard:alert-cooldown:{rule}:{inst_id or '-'}"
    if await self.redis.exists(key):
        return True
    await self.redis.set(key, ts, ex=self._settings.dashboard_alert_cooldown_seconds)
    return False
```

> **为什么要加 `inst_id` 维度**：现在的 11 条规则里 `position_drift`、`margin_ratio` 都是按品种评估的，但冷却键只有 `rule`（现状代码 `self._alert_last_published.get(alert.rule)`）。单品种时无差别；MS4-C 打开多品种绑定后，BTC 的 `position_drift` 会把 ETH 的同类告警在 600s 内吞掉——而「哪个品种对不上账」正是这个告警唯一有用信息。
>
> 兼容性：改造前所有 `Alert.inst_id` 都是 `None`，键退化为 `...:{rule}:-`（多一个后缀，但语义与现状一致）；已有内存态冷却在重启时本来就会清空，不存在需要迁移的存量键。

`_DEDUPE_RULES`（kill_switch 防刷屏）仍保留内存态，因为它比的是「消息是否变化」，属于本轮内比较，不需要跨重启。它同样要带 `inst_id` 维度，理由同上。

**冷却由谁判定**：仍在 **monitor 侧**（`_in_cooldown` 在 `Monitor` 上，位置与现状一致），`AlertService.publish` **不判冷却**——它的职责是「给定事件就落库并通知」，冷却属于「要不要产生这个事件」的上游决策。这个边界必须固定，否则同一条规则可能被两处各判一次，冷却窗口翻倍。

**一处行为变更（必须显式记录）**：现状邮件在第 `_run_once` 的「邮件聚合」段发送，用的是**未经过冷却过滤的** `alerts` 全量；只有 Redis list 受冷却约束。改写后邮件改为逐条发送、且受冷却约束，即「持续状态每 10 分钟最多一封」而非每轮一封。这是有意的修好，但属于可感知的行为变化，需在发布说明中写明。

由此 `_run_once` 的结构必须调整为「**先冷却过滤，再逐条 publish**」，原来的「邮件聚合」段整体删除：

```python
alerts = await self._evaluate_all(ts)          # 11 条规则
for a in alerts:
    # INFO（恢复）与 kill_switch 不受冷却；其余按规则+redis 冷却窗口过滤
    if a.level != "INFO" and a.rule not in _NO_COOLDOWN_RULES:
        if await self._in_cooldown(a.rule, a.inst_id, a.ts):
            continue
    await self._alert_service.publish(AlertEvent(
        rule=a.rule, level=a.level, message=a.message, ts=a.ts,
        inst_id=a.inst_id,          # Alert 需在本切片补该字段
    ))
# 后续：发布快照 / 日报 / 保留清理 / 心跳，与现状一致
```

> 注意 `Alert` 目前没有 `inst_id` 字段（§2 已列为缺口），第 2 步要给 `Alert` 补上，否则静音与事件流无法按品种归集。
>
> **必须追加在末尾且带默认值**：`Alert(rule, level, message, ts, inst_id: str | None = None)`。现有代码与单测里有大量位置参数构造（如 `Alert("worker_heartbeat", "ERROR", "心跳丢失", now)`，见 [test_monitor.py](../../tests/test_monitor.py#L1299)），把 `inst_id` 插到中间不会报错，只会让 `ts` 与 `inst_id` 悄悄错位——这是最隐蔽的一类改坏方式。

---

## 6. 告警规则与冷却

现有 11 条规则保持不变（M9-2），本切片只做两件事（规则本身的第 12 条 `data_quality` 由 MS4-C 引入，配置项在此一并预留）：

### 6.1 规则级启停（M9-2 的 🟡 收口）

新增配置，**不改代码逻辑**：

```toml
[monitor.rules]
worker_heartbeat   = true
engine_heartbeat   = true
queue_backlog      = true
dead_letter        = true
margin_ratio       = true
balance_change     = true
position_drift     = true
cancel_rate        = true
consecutive_losses = true
api_error_rate     = true
kill_switch        = true   # 不允许关闭，加载时强制 true
data_quality       = true   # MS4-C 引入（数据质量缺口告警，见其详设 §9.3）
```

实现：`MonitorSettings` 增加 `rules: dict[str, bool]`，每轮评估前过滤：

```python
def _enabled(self, rule: str) -> bool:
    if rule == "kill_switch":
        return True                     # 安全规则不可关
    return self._settings.rules.get(rule, True)
```

> `kill_switch` 强制开启是刻意的：允许关掉「有人按了紧急停止」的告警本身就是风险。

> 后续切片还会引入新规则名（MS4-B 的 `risk_halt`、MS4-C 的 `data_quality`）。`_enabled` 对未出现在 `[monitor.rules]` 里的规则返回 `True`，所以**不配置也能正常工作**；上面的示例里列出 `data_quality` 只是为了让它在配置里可见，不是必须项。

### 6.2 冷却与恢复的交互

下表按代码里的**集合**列，而不是按「规则类型」重新分类——这些集合是重叠的，硬拆成互斥分类反而会写错：

| 集合 / 成员 | 冷却 | 恢复通知 |
|---|---|---|
| `_STATEFUL_RULES`（8 条：`worker_heartbeat`/`engine_heartbeat`/`queue_backlog`/`margin_ratio`/`cancel_rate`/`api_error_rate`/`position_drift`/`kill_switch`） | 600s | 有，`level=INFO`，不受冷却与静音 |
| `_NO_COOLDOWN_RULES`（`kill_switch`） | **无冷却** | 有 |
| `_DEDUPE_RULES`（`kill_switch`） | 持续命中只发一次 | 有 |
| 不在 `_STATEFUL_RULES` 里的事件型（3 条：`dead_letter`/`balance_change`/`consecutive_losses`） | 600s | 无（没有「恢复」概念） |
| `data_quality`（MS4-C 新增，见其详设 §9.3） | 由 MS4-C 自己按 `(inst, bar)` 判 3600s，**AlertService 对它不设冷却**（否则两层冷却叠加，实际间隔会变成 2 小时以上） | 无（缺口修复体现在质量报告里） |

注意 `kill_switch` 同时属于三个集合（stateful + 不冷却 + 去重），这是刻意的：它必须每次激活都留痕，又必须防刷屏。

保持既有语义不变，仅把冷却的存储位置从内存挪到 Redis（§5.4）。**新增规则必须声明自己属于哪一类**，默认落到「事件型 600s」。

---

## 7. 进程健康

### 7.1 目标形态

7 个进程各自可判活：

| 进程 | 心跳键 | 写入方 | 周期 | TTL | 状态 |
|---|---|---|---|---|---|
| supervisor | `quant:process:supervisor` | supervisor 主循环 | 5s | 15s | 新增 |
| candles | `quant:heartbeat:candles` | candles 进程每轮结束（默认 300s） | 300s | 600s | 新增 |
| webhook | `quant:heartbeat:webhook` | webhook 后台 task | 30s | 60s | 新增 |
| worker | `quant:heartbeat:worker` | 已有（`Worker._heartbeat_loop`） | 60s | 120s（已有） | 不变 |
| engine | `quant:heartbeat:engine` | engine 每轮（`engine.interval_seconds`，默认 60s） | 60s | 120s | 新增 |
| monitor | `quant:heartbeat:monitor` | 已有 | 60s | **300s（现有值，不改）** | 不变 |
| dashboard | `quant:heartbeat:dashboard` | FastAPI 后台 task | 30s | 60s | 新增 |

TTL 规则：**`max(2 × 周期, 60s)`**，即最短 60 秒容忍。worker 现有实现恰好就是 2×（60s → 120s），规则与现状一致。

两个**显式例外**，都写在表里，不是「另有隐情」：

1. **supervisor 取 15s**：它 5 秒一轮，`max(2×5, 60)=60s` 意味着挂了要等一分钟才发现，而它正是「托管进程最快报警」的那一环，等于把这个能力作废。故取 `3 × 5s = 15s`。
2. **monitor 保持 300s**：monitor 每轮要发多次 OKX 请求（持仓、余额、K线），单轮耗时波动远大于其它进程。把 300s 改成 120s 会让它在交易所慢响应时误报离线——而「误报离线」比「晚 3 分钟发现」代价更大。**改既有判活阈值需要单独评估，不在本切片顺手做**（`[monitor] monitor_heartbeat_timeout_seconds` 继续生效）。

### 7.2 统一心跳工具

```python
# src/quant/foundation/heartbeat.py

class Heartbeat:
    """定时写带 TTL 的心跳键，供看板与 monitor 判活。"""

    def __init__(
        self, redis: Redis, name: str, interval: int = 30,
        ttl: int | None = None,
    ) -> None:
        self._redis, self._name = redis, name
        self._interval = interval
        # 默认 max(2×周期, 60s)；supervisor 显式传 ttl=15 走例外
        self._ttl = ttl if ttl is not None else max(interval * 2, 60)
        self._task: asyncio.Task | None = None

    async def beat(self, extra: dict | None = None) -> None:
        payload = {"ts": int(time.time() * 1000), "pid": os.getpid()}
        if extra:
            payload.update(extra)
        await self._redis.set(
            f"quant:heartbeat:{self._name}",
            json.dumps(payload, ensure_ascii=False),
            ex=self._ttl,
        )

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
```

各进程接入点：

- **candles**：同步 CLI 进程，在每轮拉取结束后直接 `await beat()`，不起后台 task。
- **webhook**：FastAPI `startup` 事件创建 `Heartbeat("webhook").start()`，`shutdown` 停止。
- **dashboard**：同上，`Heartbeat("dashboard")`。
- **engine**：已有 `engine:status:{strategy}:{inst}`，额外写 `quant:heartbeat:engine`，`extra={"strategy": ..., "inst": ...}`。
- **supervisor**：**不额外写 `quant:heartbeat:supervisor`**，它写的进程表键 `quant:process:supervisor`（§7.3，TTL 15s）本身就可当判活信号——同一进程写两个键，只会让「哪个才是真的」变成一个问题。supervisor 若挂了，进程表键过期即最强信号。

### 7.3 supervisor 进程表

```python
# 键：quant:process:supervisor
{
  "ts": 1793374800000,
  "pid": 12345,
  "children": {
    "candles":   {"pid": 12346, "started_at": 1793370000000, "restarts": 0, "last_exit": null},
    "webhook":   {"pid": 12347, "started_at": 1793370000000, "restarts": 2, "last_exit": 1},
    ...
  }
}
```

TTL = 3 × 5s = 15s：supervisor 是主循环，5 秒一轮，15 秒未更新即视为死亡。

### 7.4 `ProcessHealth` 视图

```python
@dataclass(frozen=True, slots=True)
class ProcessHealth:
    name: str
    online: bool
    last_ts: int | None
    age_seconds: float | None
    pid: int | None
    restarts: int | None
    last_exit_code: int | None
    interval_seconds: int
    reason: str | None          # offline 时说明原因：no_key / stale / parse_error
```

聚合逻辑（`DashboardService.processes()`）：

1. 读 `quant:process:supervisor` → 得到 7 个 child 的 pid/restarts。
2. 逐个读心跳键（children 读 `quant:heartbeat:{name}`，supervisor 读 `quant:process:supervisor`），用各自的 TTL 算 `age_seconds`。
3. `online = raw is not None and age <= 该进程的 TTL`。TTL 取 §7.1 表里的值（派生规则 `max(2×周期, 60s)`，例外：supervisor 15s、monitor 300s），**不是**统一用 `2×interval` 现算——否则 monitor 会按 120s 判活，与它实际的 300s TTL 对不上，出现「键还在、页面却说离线」。
4. supervisor 与 children 的对应关系若缺失（如 `--no-dashboard` 启动），该进程 `online=False, reason="not_managed"`——**要区分「托管但死了」和「本就没启用」**。

> 现状缺口（M1-5 标 🟡 的原因）：只有 2/7 有心跳。本切片补齐 7 个后，M1-5 可转 ✅。

---

## 8. 事件流

### 8.1 查询设计

M1-6 要求「当日信号/下单/成交/告警按时间倒序合并」。四表 `UNION ALL`：

```sql
SELECT ts, kind, severity, title, detail, ref_id FROM (
    SELECT created_at AS ts, 'alert' AS kind, level AS severity,
           rule AS title, message AS detail, alert_id AS ref_id
      FROM alerts WHERE created_at >= :since
    UNION ALL
    SELECT ts, 'signal', NULL,
           side || ' ' || inst_id, strategy_id || ' qty=' || qty, signal_id
      FROM signals WHERE ts >= :since
    UNION ALL
    SELECT created_ts, 'order', status,
           'order ' || order_id, 'filled=' || COALESCE(filled_qty, '0'), order_id
      FROM orders WHERE created_ts >= :since
    UNION ALL
    SELECT ts, 'fill', side,
           side || ' ' || inst_id, 'qty=' || qty || ' @' || price, order_id
      FROM fills WHERE ts >= :since
)
ORDER BY ts DESC
LIMIT :limit OFFSET :offset;
```

要点：

- **每张表都用「事件发生时间」而非「写入时间」**：`signals.ts`（bar 时间）、`orders.created_ts`、`fills.ts`、`alerts.created_at`（落库时写入的是事件时间）。若误用 `signals.created_at`（写入时间），补评估的历史 bar 会被排到「今天」，事件流时间轴失真。
- **单条 SQL 而非 Python 合并**：`ORDER BY + LIMIT` 才能正确分页；Python 合并四路各自 `LIMIT N` 会在跨类型时错位。
- **`since` 为当日 00:00 UTC+8**（`day_start_ms_utc8`），与风控日切口径一致；UI 明示时区（PRD §5.6 要求）。
- **索引**：`signals(ts)`、`orders(created_ts)`、`fills(ts)`、`alerts(created_at)` —— 前三个在 v3 迁移中补。
- **`fills` 的复合主键 `(order_id, ts)` 已足够支持排序与跳转**，`ref_id` 用 `order_id`；但同一订单同一毫秒的两笔成交无法区分（现实中不会出现，列入开放问题）。

### 8.2 `DailyTimelineItem`

```python
@dataclass(frozen=True, slots=True)
class DailyTimelineItem:
    ts: int
    kind: str          # alert / signal / order / fill
    severity: str | None
    title: str
    detail: str
    ref_id: str
    module: str        # 跳转目标：alerts / strategies / execution
```

`module` 映射：`alert→alerts`、`signal→strategies`、`order/fill→execution`。

### 8.3 筛选

`GET /api/timeline?kinds=alert,fill&limit=50`。`kinds` 为空表示全部；非法值忽略并回落到全部，不报 400——前端筛选组件不该因拼错参数白屏。

---

## 9. 数据新鲜度

### 9.1 定义

对每个「引擎实际消费的 `品种 × 周期`」计算：

```
latest_closed_ts = 该品种该周期最新一根已收盘 bar 的 ts
expected_ts      = 最近一根应该已收盘的 bar 的 ts
                 = ((now_ms - bar_ms) // bar_ms) * bar_ms
                    （bar 的 ts 是开盘时间，T + bar_ms <= now 才算收盘）
lag_bars         = (expected_ts - latest_closed_ts) / bar_size_ms
status           = OK      lag_bars <= 1
                 | LAGGING lag_bars <= 6
                 | STALE   lag_bars > 6
```

> 只统计 `confirm=1` 的行。`confirm=0` 是未收盘 bar，拿来算滞后会永远为 0，掩盖真实延迟。

### 9.2 不扫全表的实现

parquet 的 row group 带 `min/max` 统计，取 `ts` 最大值是 O(1)——但**只用 stats 会算错**：采集器会把当前未收盘的 bar（`confirm=0`）一起落盘，它的 `ts` 就是最大值，直接取会导致 `lag_bars` 恒为 0，正好掩盖真实延迟。所以必须做一次「末根校正」：

```python
_TS_COL = "ts"          # parquet 中时间列名，与 bar_storage 写入口径一致

def _latest_closed_ts(path: Path, bar_ms: int) -> int | None:
    """返回最新一根已收盘（confirm=1）bar 的 ts，不加载整份文件。"""
    pf = pq.ParquetFile(path)
    meta = pf.metadata
    if meta.num_row_groups == 0:
        return None

    # 快路径：全文件 ts 最大值（来自 row group 统计，O(1)）
    best: int | None = None
    for i in range(meta.num_row_groups):
        stats = meta.row_group(i).column(_TS_COL).statistics
        if stats is not None and stats.has_min_max:
            best = stats.max if best is None else max(best, stats.max)
    if best is None:                     # 统计缺失的退路，见约束 2
        return _scan_closed_ts(pf)

    # 末根校正：只读最后一个 row group 的 ts/confirm（有界，见约束 4）
    tail = pf.read_row_group(
        meta.num_row_groups - 1, columns=[_TS_COL, "confirm"]
    ).to_pandas()
    if tail.empty:
        return best
    last = tail[tail[_TS_COL] == best]
    if not last.empty and int(last["confirm"].iloc[-1]) == 1:
        return best
    return best - bar_ms               # 最新一行未收盘，回退一个周期


def _scan_closed_ts(pf: pq.ParquetFile) -> int | None:
    """统计缺失时的退路：只读 ts/confirm 两列做全表兜底。"""
    df = pf.read(columns=[_TS_COL, "confirm"]).to_pandas()
    closed = df[df["confirm"] == 1]
    if closed.empty:
        return None
    return int(closed[_TS_COL].max())
```

四个约束：

1. **只看当年文件**：路径 `data/candles/{inst_type}/{inst_id}/{bar_size}/{year}.parquet`，只读当前年份。跨年第一天会短暂显示 `STALE`（去年文件有数据、今年文件还没写），可接受，元旦后的第一根 bar 落盘即恢复。
2. **`statistics` 可能缺失**：pyarrow 写 parquet 时默认带统计，但若历史文件是别的工具写的，`stats is None` 时退化为读 `ts, confirm` 两列扫描，并在 `degraded` 标注 `freshness:{inst}:{bar}`。
3. **只查引擎在用的组合**：从 `[engine] inst` 与 `engine:status:*` 的键后缀取，不遍历全部目录（否则品种多了以后每次请求扫描几十个文件）。
4. **`read_row_group` 必须是有界开销**：`bar_storage` 写 parquet 时显式设 `row_group_size=20_000`，保证单次读取最多 2 万行（百 KB 级）。若不设，pyarrow 默认整表一个 row group，这一步等于读全文件。

> 也可选择「落盘时过滤 `confirm=0`」从根上消除问题，但那会改变 `bar_storage` 的写入口径并影响回测/对齐，超出本切片范围，暂不采用。

### 9.3 `DataFreshness`

```python
@dataclass(frozen=True, slots=True)
class DataFreshness:
    inst_id: str
    bar_size: str
    latest_closed_ts: int | None
    lag_bars: float | None
    status: str            # OK / LAGGING / STALE / UNKNOWN
    source: str            # parquet / cache
```

`UNKNOWN` 用于「文件不存在」——与 `STALE` 区分：前者是没数据，后者是有数据但不更新。

---

## 10. API 契约

### 10.1 新增

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/processes` | 7 进程健康，返回 `ProcessHealth[]` |
| GET | `/api/timeline` | 今日事件流，参数 `kinds` / `limit` / `offset` |
| GET | `/api/freshness` | 数据新鲜度，返回 `DataFreshness[]` |
| POST | `/api/alerts/{alert_id}/ack` | 标记已读，body `{"by": "owner"}` |
| POST | `/api/alerts/silence` | 创建静音，body `{"rule","inst_id","hours","reason","by"}` |
| DELETE | `/api/alerts/silence/{silence_id}` | 解除静音 |

所有写接口走既有 `_guard_write`（本机 Host + `X-Dashboard-Request: 1` + 可选 token，M10-8）。

### 10.2 变更

`GET /api/alerts` 增加参数并改数据源：

```
GET /api/alerts?limit=50&offset=0&level=ERROR&rule=margin_ratio&state=open&since=&until=
→ {
    "ts": ...,
    "total": 137,            // 新：命中总数，用于分页
    "alerts": [...],
    "silences": [...],       // 新：当前生效静音，UI 顶部提示
    "degraded": []
  }
```

`degraded` 语义：SQLite 读失败 → `["sqlite"]`，此时**回落读 Redis list**（`alerts` 字段仍有值，但 `total=None`），UI 标注「仅显示最近 200 条，历史不可用」。

### 10.3 `/api/overview` 扩展

新增字段（不破坏现有结构）：

```json
{
  "processes": [ ... ],        // 新：7 进程摘要（online 计数 + 离线名单）
  "freshness": [ ... ],        // 新：仅引擎在用组合
  "alert_summary": {           // 新：未读计数，供 M1-9
    "unread_error": 2,
    "unread_warning": 5,
    "by_rule": {"margin_ratio": 3, "queue_backlog": 2}
  }
}
```

`alert_summary` 走 `SELECT level, rule, COUNT(*) FROM alerts WHERE state='open' AND suppressed=0 GROUP BY level, rule`。

---

## 11. 前端改造

| 页面 | 改动 |
|---|---|
| 首页 | 新增「进程健康」卡片（7 个圆点 + 离线原因 tooltip）、「今日事件流」列表（带类型筛选）、「数据新鲜度」条（`engine 落后 N 根` 标红）、告警摘要卡接 `alert_summary` |
| 告警页 | 改为服务端分页 + 时间区间/状态筛选；每行加「确认」按钮；顶部显示生效静音（可解除）；新增「静音此规则」入口 |
| 全局 | 所有卡片底部统一渲染 `degraded` 提示条：「数据截至 HH:MM」+ 降级项列表 |

---

## 12. 配置扩展

```toml
[monitor]
# 不新增心跳周期/超时配置项：周期复用各进程现有 interval 配置，
# TTL 由 max(2×周期, 60) 派生（§7.1 的两个例外直接用常量）。
dashboard_alert_cooldown_seconds = 600   # 沿用现有键，不改名
dashboard_alert_keep = 200               # 沿用现有键，不改名
alerts_persist = true                    # 新：告警落库总开关（§13 回滚用）

# 新：规则级启停（kill_switch 强制 true）。TOML 子表必须写在 [monitor] 之后。
[monitor.rules]
worker_heartbeat = true
engine_heartbeat = true
queue_backlog = true
dead_letter = true
margin_ratio = true
balance_change = true
position_drift = true
cancel_rate = true
consecutive_losses = true
api_error_rate = true
kill_switch = true
data_quality = true                  # MS4-C 引入（见其详设 §9.3）

[storage]
retention_alerts_days = 90           # 新键；未配置时回落现有 retention_days
freshness_lag_warn_bars = 1          # 新
freshness_lag_stale_bars = 6         # 新
```

> **不复用 `retention_days` 一个键管全部**：告警是「按天也值得留」的文本，与 K 线 parquet 的量级差几个数量级，用同一阈值会导致要么 K 线留太久、要么告警删太快。`retention_alerts_days` 未配置时回落 `retention_days`，保证老配置不改也能跑。
>
> 按级别区分保留天数（ERROR 90 / WARNING 30 / INFO 7，M9-12）是 MS5 范围；本切片为单一值，先保证「不无限增长」。

---

## 13. 迁移与兼容

| 阶段 | 动作 | 可回滚点 |
|---|---|---|
| 1 | 加 `_MIGRATIONS` 与 `PRAGMA user_version`，跑 v2 建表 + `DROP heartbeat` | 备份 `data/state/*.db` |
| 2 | `AlertService` 上线，monitor 改为调用它；Redis list 双写保持 | 关配置 `[monitor] alerts_persist = false` 退回纯 Redis |
| 3 | `/api/alerts` 切 SQLite，Redis 作 fallback | 同上开关 |
| 4 | 7 进程心跳接入 | 逐进程接入，未接入的显示 `not_managed` 而非 `offline` |
| 5 | `/api/processes`、`/api/timeline`、`/api/freshness` 上线 | 新增接口，删除即回滚 |

回滚开关（`[monitor] alerts_persist = true`）：置 `false` 时 `AlertService.publish` 跳过落库与读库，退化为当前行为。用于「迁移出问题但不想回滚代码」的应急。

---

## 14. 错误处理与降级

| 故障 | 行为 |
|---|---|
| SQLite 写失败（磁盘满/锁） | `publish` 记 ERROR 日志，继续推 Redis，不影响监控循环 |
| SQLite 读失败 | `/api/alerts` 回落 Redis list，`degraded=["sqlite"]`，`total=null` |
| Redis 挂 | 落库照常；`/api/processes` 全部 `online` 未知 → `degraded=["redis"]`，UI 显示「无法判断」而不是「全部离线」 |
| parquet 统计缺失 | 退化为读 `ts` 列，`degraded=["freshness:{inst}:{bar}"]` |
| 迁移中途崩溃 | 迁移脚本幂等，重启重放；`user_version` 只在脚本执行成功后写 |
| 时钟回拨 | `age_seconds` 为负时钳到 0 并记 WARNING；`lag_bars` 为负同样钳为 0 |

---

## 15. 测试策略

| 层 | 用例 |
|---|---|
| 迁移 | 空库跑到最新；老库（v0 有 6 表）跑到最新；重复执行不报错；`user_version` 正确 |
| AlertService | 同一 `AlertEvent` 发布两次只落一行（幂等）；静音期内 `suppressed=1` 且不发邮件；INFO 不受静音；落库失败仍推 Redis 且不抛 |
| 静音匹配 | 3 种粒度各自命中；过期静音不生效；`revoked_at` 后不生效；多条叠加任一命中 |
| 冷却 | 600s 内同规则同品种只推一次；**不同品种互不抑制**；`kill_switch` 不看冷却但消息不变只留一条；冷却跨 monitor 重启仍生效（Redis） |
| 状态机 | `open→acked→resolved`；重复 `ack` 不覆盖首次时间；恢复 INFO 把同规则 open 置 resolved |
| 事件流 | 四表混合排序正确；`kinds` 筛选；分页 `offset` 边界；单表读失败时其余三类仍返回 |
| 新鲜度 | `confirm=0` 不计；`lag_bars` 计算；当年文件缺失 → `UNKNOWN`；统计缺失退化路径 |
| 进程健康 | 7 进程全在线；单进程键过期；`not_managed` 与 `offline` 区分 |
| 降级 | Redis 不可用时 `/api/processes` 不报 500 |

**必须用单测固定的口径**（这类最容易悄悄漂移）：

1. `alert_id` 生成规则（改动会导致幂等失效、历史重复）
2. `fingerprint` 归一化（数字替换）
3. 静音匹配的三级优先级
4. `lag_bars` 的分母是「周期毫秒数」而非「根数 × 60s」

---

## 16. 实现顺序

1. **迁移机制**（`user_version` + v2 建表 + 删 `heartbeat`）——后续一切的地基
2. **`SQLiteStore` 告警方法与单测**
3. **`AlertService`** + 单测（幂等/静音/冷却/状态机）
4. **monitor 切换到 `AlertService`**，保留 Redis 双写
5. **`/api/alerts` 切 SQLite + 前端告警页**（ack/silence 接口与表结构同批给出；按 PRD 排期，其前端入口可在 MS5 再接）
6. **统一 `Heartbeat` 工具 + 5 个进程接入**
7. **supervisor 进程表 + `/api/processes` + 首页进程卡片**
8. **v3 索引迁移 + `/api/timeline` + 首页事件流**
9. **`/api/freshness` + 首页新鲜度条**
10. **`/api/overview` 扩展 `alert_summary` + 降级提示条统一**

每步独立可交付、可回滚。第 4 步完成即解决「告警会丢」这个最大问题。

---

## 17. 验收标准映射

| 条目 | 验收信号 | 主要落点 |
|---|---|---|
| M1-1 账户总览卡 | `/api/overview.account` 有值，缺失时进 `degraded` 且 UI 标「数据截至」 | §10.3 |
| M1-2 持仓摘要 | 一行一品种，可跳执行页 | 现有能力，补跳转 |
| M1-3 风险摘要 | 保证金率/回撤/强平价距离/撤单率四项齐 | 现有能力 |
| M1-4 权益曲线 | 24h/7d/30d 三档 | 现有能力 |
| M1-5 进程健康 | 7 个进程均可判活，离线标红并给原因 | §7 |
| M1-6 今日事件流 | 四类事件合并倒序，可筛选，可跳转 | §8 |
| M1-7 数据新鲜度 | 显示 `engine 落后 N 根`，超阈值标红 | §9 |
| M1-9 告警摘要卡 | 未读 ERROR/WARNING 计数 + 按规则分组（设计就绪，排期 MS5） | §10.3 |
| M1-10 降级标注 | 任一源失败均有提示，无静默 0/— | §14 |
| M9-2 规则阈值与启停 | 11 条规则可独立启用/关闭，`kill_switch` 不可关 | §6.1 |
| M9-4 监控自身心跳 | 已有 | — |
| M9-5 告警列表页 | 时间区间/级别/规则/状态筛选 + 分页 | §10.2 |
| M9-7 确认与静音 | 确认记操作人与时间；静音期 `suppressed=1` 且不发邮件（设计就绪，排期 MS5） | §5 |
| M9-8 告警持久化 | 重启/Redis 清空后历史仍在 | §4.2 |
| M9-9 去重与冷却 | 600s 冷却，kill_switch 防刷屏，INFO 不受限 | §6.2 |
| M9-10 静音粒度 | 三种粒度 + 叠加任一命中（设计就绪，排期 MS5） | §4.3 |
| M9-11 与业务事件联动 | 风控 halt/死信/回测失败/对账差异统一走 `publish` | §5.2 |
| M9-13 存活邮件 | 已有 | — |

---

## 18. 风险与开放问题

### 18.1 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 迁移在用户真实库上失败 | 数据不可用 | 迁移前自动备份 `data/state/*.db` 到 `data/backups/pre-migrate-{ts}.db` |
| 单条邮件发送过多 | 收件箱噪声 | 冷却 600s 兜底；后续可加「N 分钟内合并」（MS5） |
| `alerts` 表增长快 | 首页查询变慢 | `retention_alerts_days=90` + 三索引；量大后按月分区（MS5） |
| pyarrow 统计缺失 | 新鲜度全量读变慢 | 退化为只读 `ts` 列；记录降级项 |
| 7 进程心跳均写 Redis | Redis QPS + | 30s 一次 × 7 = 0.23 QPS，可忽略 |

### 18.2 开放问题

| 问题 | 决策时机 |
|---|---|
| `fills` 表无自增主键，事件流分页在「同 ts 多行」时可能跨页重复 | MS4-A 实施时验证；必要时加 `rowid` 排序兜底 |
| dashboard 进程心跳是否用 supervisor 写入（而非自写） | 实施时定：自写更准（能反映事件循环是否卡死） |
| 告警趋势统计（M9-14）的预聚合表 | MS5 |
| 是否把 `logs/` 与告警关联（点告警看日志片段） | 需 M9-6 已决策为 🚫，不做 |

---

## 19. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-30 | 初版。建立 SQLite 迁移机制；新增 `alerts`/`alert_silences` 两表与 `AlertService`；告警从 Redis 升级为 SQLite 权威 + Redis 缓存；补齐 7 进程心跳与 supervisor 进程表；新增事件流（四表 UNION）与数据新鲜度（parquet 统计 O(1)）；规则级启停；`heartbeat` 死表删除 |
| v1.1 | 2026-10-30 | 复核修订。修 §12 配置示例 `[monitor]` 重复声明（TOML 语法错误）并沿用现有键名；supervisor 心跳 TTL 15s 作为统一规则的显式例外；v2 迁移补 `DROP TABLE heartbeat`；补 `AlertService.resolve()`；明确冷却判定归 monitor 侧、邮件改为受冷却约束（行为变更）并给出 `_run_once` 新结构；事件流改用各表事件时间（`signals.ts`）并同步 v3 索引；**修正数据新鲜度算法**（`confirm=0` 的末根会被 `stats.max` 取到，导致滞后恒为 0），补末根校正、`row_group_size` 约束与 `expected_ts` 公式；`Alert` 需补 `inst_id`；覆盖条目与 PRD §8.1 对齐（M1-9/M9-7/M9-10 标注 MS5 排期，M9-12 移出范围）；**修正迁移版本号偏移**（`_MIGRATIONS` 从 2 开始枚举，`_SCHEMA` 即 v1，否则 MS4-B 的 v4/v5 会整体错位）；进程 TTL 表补 monitor 300s 例外并说明不做统一收紧的原因；规则集合按 `_STATEFUL_RULES`/`_NO_COOLDOWN_RULES`/`_DEDUPE_RULES` 如实描述（原表把它们当成互斥分类）；预留 `data_quality`/`risk_halt` 规则名；补 `publish` 中 `key`/`keep` 的取值来源；说明 `alerts.created_at` 存 `event.ts`；**冷却键补 `inst_id` 维度**（否则 MS4-C 多品种下同规则告警互相吞掉）；补 `heartbeat` 删除时须同步处理 `test_sqlite_store.py` 用例 |
