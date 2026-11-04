"""只读看板的数据聚合。

数据来源：
- Redis：monitor 发布的账户/持仓/指标快照与告警，worker/monitor 心跳，
  信号队列长度，kill-switch 标志
- SQLite：本地订单、成交、持仓记录

本模块只读：不下单、不写库、不改 Redis。单个数据源失败时把来源名
放进响应的 `degraded` 列表，其余数据照常返回。
"""
from __future__ import annotations

import asyncio
import csv
import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from redis.asyncio import Redis

from quant.engine.live import EVAL_HISTORY_LIMIT
from quant.execution.dead_letter import DeadLetterService
from quant.execution.kill_switch import KillSwitch
from quant.foundation.config import Settings, SmtpSettings
from quant.foundation.heartbeat import (
    HEARTBEAT_PREFIX,
    PROCESS_SPECS,
    SUPERVISOR_KEY,
    default_ttl,
    parse_heartbeat,
)
from quant.foundation.logger import get_logger
from quant.foundation.schedule import day_start_ms_utc8
from quant.monitor.alert_service import AlertEvent, AlertService
from quant.monitor.alerter import EmailAlerter
from quant.okx.ws_client import ws_status_key
from quant.queue.signal_queue import SignalQueue
from quant.risk.circuit_breaker import STATUS_AWAITING_RESUME, CircuitBreaker
from quant.risk.config import RiskConfig
from quant.risk.events import build_event_id
from quant.risk.overrides import RiskOverrideService
from quant.risk.pnl import round_trip_pnls
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage
from quant.storage.quality import QualityReporter
from quant.storage.validator import DataValidator
from quant.strategies.loader import load_strategy_class
from quant.strategies.params import ParamError, coerce_value

log = get_logger("dashboard.service")

#: M1-7：落后达到该根数即视为不新鲜。PRD 验收要求「落后超过 1 根必须标红」，
#: 而落后 1 根时引擎已经错过一根 bar，提前一根报出更有用。
FRESHNESS_LAG_THRESHOLD = 1


def _age_seconds(ts: int | None, now_ms: int) -> int | None:
    if ts is None:
        return None
    return max(int((now_ms - ts) / 1000), 0)


def _as_int(raw: object) -> int | None:
    """宽松转 int；None、非数字返回 None。"""
    if raw is None:
        return None
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _field_int(payload: dict | None, field: str) -> int | None:
    """从已解析的快照里取整数字段。"""
    if payload is None:
        return None
    return _as_int(payload.get(field))


def _ratio_text(value: Decimal | None) -> str | None:
    """比率转字符串并保留 6 位，避免 28 位长串传到前端。"""
    if value is None:
        return None
    return str(value.quantize(Decimal("0.000001")))


def _as_decimal(raw: object, default: Decimal) -> Decimal:
    """宽松转 Decimal；非法值退回默认值。"""
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return value if value.is_finite() else default


def _as_decimal_or_none(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


# 风控限额配色阈值：70% 以上提醒，100% 触发硬性上限
_LIMIT_WARN_RATIO = Decimal("0.7")
_RISK_DEFAULTS = RiskConfig(strategy_id="")

# 看板规则 key → `risk_events.rule`（M7-9 触发次数与最近详情）
# 页面展示的是「规则」，事件表记的是「命中的限值字段」，两者需映射。
_RISK_EVENT_RULES: dict[str, str] = {
    "daily_loss": "daily_loss_limit",
    "consecutive_loss": "consecutive_loss_limit",
    "order_notional": "max_order_qty",
    "inst_position": "max_position_pct",
    "total_position": "max_total_pct",
}


def _limit_ratio(used: Decimal, limit: Decimal) -> Decimal | None:
    if limit <= 0:
        return None
    return used / limit


def _limit_level(ratio: Decimal | None) -> str:
    if ratio is None:
        return "ok"
    if ratio >= 1:
        return "halt"
    if ratio >= _LIMIT_WARN_RATIO:
        return "warn"
    return "ok"


def _limit_rule(
    key: str,
    label: str,
    used: Decimal | None,
    limit: Decimal | None,
    detail: str,
    source: str | None = None,
) -> dict[str, object]:
    ratio = (
        _limit_ratio(used, limit)
        if used is not None and limit is not None
        else None
    )
    return {
        "key": key,
        "label": label,
        "used": str(used) if used is not None else None,
        "limit": str(limit) if limit is not None else None,
        "ratio": _ratio_text(ratio),
        "level": _limit_level(ratio),
        "detail": detail,
        # 该限值来自哪一层（default/global/strategy/override），
        # 用于区分「策略里写死的」和「今天临时放宽的」
        "source": source,
    }


def _risk_event_view(row: dict | None) -> dict[str, object] | None:
    """把一条 `risk_events` 行整理成页面需要的「最近一次详情」（M7-9）。"""
    if not row:
        return None
    return {
        "ts": _as_int(row.get("ts")) or 0,
        "decision": row.get("decision"),
        "strategy_id": row.get("strategy_id"),
        "inst_id": row.get("inst_id"),
        "signal_id": row.get("signal_id"),
        "limit_value": row.get("limit_value"),
        "actual_value": row.get("actual_value"),
        "qty_before": row.get("qty_before"),
        "qty_after": row.get("qty_after"),
        "reason": row.get("reason"),
    }


def _read_report_file(path: Path, threshold: float) -> dict[str, object] | None:
    """解析一份日报 CSV，返回展示用结构；无法解析时返回 None。"""
    stem = path.stem
    # 文件名 {YYYY-MM-DD}-{strategy}-{inst}.csv，inst 自身带连字符
    date_str = "-".join(stem.split("-")[:3])
    rest = stem[len(date_str) + 1 :]
    strategy, _, inst = rest.partition("-")
    if not date_str or not strategy or not inst:
        return None

    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("metric"):
                continue
            rows.append({k: (v or "") for k, v in row.items() if k})

    deviations: list[float] = []
    side_ok = True
    for row in rows:
        metric = row.get("metric", "")
        if metric == "side_sequence":
            side_ok = row.get("paper") == "match"
            continue
        try:
            deviations.append(abs(float(row.get("deviation", ""))))
        except (TypeError, ValueError):
            continue

    max_dev = max(deviations) if deviations else 0.0
    return {
        "name": path.name,
        "date": date_str,
        "strategy": strategy,
        "inst": inst,
        "mtime": int(path.stat().st_mtime * 1000),
        "size": path.stat().st_size,
        "status": "PASS" if side_ok and max_dev <= threshold else "FAIL",
        "max_deviation": max_dev,
        "rows": rows,
    }


def _read_reports(
    out_dir: Path, limit: int, threshold: float
) -> list[dict[str, object]]:
    """按修改时间倒序读取最近的日报 CSV。"""
    if not out_dir.is_dir():
        return []
    paths = sorted(
        out_dir.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:limit]
    out: list[dict[str, object]] = []
    for path in paths:
        try:
            item = _read_report_file(path, threshold)
        except OSError as e:
            log.warning(f"read report {path.name} failed: {e}")
            continue
        if item is not None:
            out.append(item)
    return out


def _heartbeat(
    raw: object, timeout_seconds: int, now_ms: int
) -> dict[str, object]:
    """心跳键 → 在线状态。键不存在或值非法都视为离线。"""
    offline = {"online": False, "last_ts": None, "age_seconds": None}
    if raw is None:
        return offline
    last, _pid = parse_heartbeat(raw)
    if last is None:
        return offline
    age = _age_seconds(last, now_ms)
    return {
        "online": age is not None and age <= timeout_seconds,
        "last_ts": last,
        "age_seconds": age,
    }


def _child_int(child: dict, field: str) -> int | None:
    """读取 supervisor 进程表里的整数字段；缺失或非法返回 None。"""
    value = child.get(field) if isinstance(child, dict) else None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ws_status(raw: dict | None) -> dict[str, object] | None:
    """规范化 WS 状态（M2-2）。

    字段缺失或类型不符时给安全默认值：卡片上少一行信息，好过整个
    进程列表因一条脏数据解析失败。
    """
    if raw is None:
        return None
    next_delay = raw.get("next_delay")
    last_error = raw.get("last_error")
    return {
        "ts": _child_int(raw, "ts"),
        "connected": bool(raw.get("connected")),
        "failures": _child_int(raw, "failures") or 0,
        "last_error": last_error if isinstance(last_error, str) else None,
        "last_error_ts": _child_int(raw, "last_error_ts"),
        "connected_since": _child_int(raw, "connected_since"),
        "next_delay": (
            float(next_delay) if isinstance(next_delay, (int, float)) else None
        ),
        # 下次重连墙钟时刻（UTC ms）：前端按本地时钟倒数（P1 口径）
        "next_retry_at": _child_int(raw, "next_retry_at"),
    }


def _process_item(
    name: str,
    *,
    raw_present: bool,
    ts: int | None,
    pid: int | None,
    ttl: int,
    now_ms: int,
    interval_seconds: int,
    restarts: int | None,
    last_exit: int | None,
    child_pid: int | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    """单个进程的判活结果。

    `reason` 区分四种离线原因：没写过键（no_key）、键过期（stale）、
    值解析不出来（parse_error）、supervisor 没托管（not_managed）——
    UI 需要知道是「死了」还是「本来就没开」。
    """
    if reason is None:
        if not raw_present:
            reason = "no_key"
        elif ts is None:
            reason = "parse_error"
        else:
            age = (now_ms - ts) / 1000
            if age > ttl:
                reason = "stale"
    age_seconds = None if ts is None else (now_ms - ts) / 1000
    return {
        "name": name,
        "online": reason is None,
        "last_ts": ts,
        "age_seconds": age_seconds,
        "pid": child_pid if child_pid is not None else pid,
        "restarts": restarts,
        "last_exit_code": last_exit,
        "interval_seconds": interval_seconds,
        "reason": reason,
    }


def _merge_position(local, remote: dict | None) -> dict[str, object]:
    """合并本地持仓记录与 monitor 发布的市场快照。

    `local_*` 是策略记账口径（只含策略自己的成交），`qty/avg_price/...`
    是账户口径（含外部转入与手工交易）。两者数量常常不同，因此盈亏各自
    独立计算，不拿一方的数量乘另一方的成本。
    """
    remote = remote or {}
    mark = remote.get("mark_price")
    # 现货没有交易所侧成本基数：monitor 以 0 表示「未知」。直接透出会
    # 被前端当作真实成本价显示成 0.00，故按未知处理（回退本地均价或 —）。
    avg_price = remote.get("avg_price")
    try:
        if avg_price is not None and Decimal(str(avg_price)) <= 0:
            avg_price = None
    except (InvalidOperation, ValueError):
        avg_price = None
    unrealized = remote.get("unrealized_pnl") if avg_price is not None else None

    # 策略自己的持仓盈亏：数量与成本都取本地记账，只借用快照的标记价。
    # 账户里有策略之外的币（外部转入/手工交易）时，这个数才是策略战绩。
    strategy_upl = None
    if local is not None and local.qty > 0 and mark is not None:
        try:
            strategy_upl = (
                Decimal(str(mark)) - local.avg_price
            ) * local.qty
        except (InvalidOperation, ValueError):
            strategy_upl = None

    return {
        "inst_id": local.inst_id if local is not None else remote.get("inst_id"),
        "local_qty": str(local.qty) if local is not None else None,
        "local_avg_price": str(local.avg_price) if local is not None else None,
        "realized_pnl": str(local.realized_pnl) if local is not None else None,
        "strategy_unrealized_pnl": (
            str(strategy_upl) if strategy_upl is not None else None
        ),
        "qty": remote.get("qty"),
        "avg_price": avg_price,
        "mark_price": mark,
        "unrealized_pnl": unrealized,
        "leverage": remote.get("leverage"),
        "liq_price": remote.get("liq_price"),
        "liq_distance": remote.get("liq_distance"),
    }


def _fill_to_dict(fill, now_ms: int) -> dict[str, object]:
    return {
        "order_id": fill.order_id,
        "inst_id": fill.inst_id,
        "side": fill.side,
        "qty": str(fill.qty),
        "price": str(fill.price),
        "fee": str(fill.fee),
        "notional": str(fill.qty * fill.price),
        "ts": fill.ts,
        "age_seconds": _age_seconds(fill.ts, now_ms),
    }


class DashboardService:
    """看板只读查询。"""

    def __init__(
        self, store: SQLiteStore, redis: Redis, settings: Settings
    ) -> None:
        self._store = store
        self._redis = redis
        self._settings = settings
        self._kill_switch = KillSwitch(redis)
        self._bars = BarStorage(settings.storage.data_dir)
        # 看板只做「查告警 / 确认 / 静音」，不发邮件，故用未配置的 alerter
        self._alerts = AlertService(
            store, redis, EmailAlerter(SmtpSettings()), settings.monitor
        )
        # 重放必须回到 worker 消费的同一个 pending key
        self._dead = DeadLetterService(
            store,
            SignalQueue(
                redis,
                pending_key=settings.redis.queue_key,
                inflight_key=settings.redis.inflight_key,
                dead_key=settings.redis.dead_key,
            ),
        )
        # 阈值在线调整：覆盖层在 Redis，审计在 SQLite（M7-8 / M7-14）
        self._overrides = RiskOverrideService(
            store, redis, key=settings.monitor.risk_overrides_key
        )

    @property
    def settings(self) -> Settings:
        """供同进程的其他只读模块（如回测面板）复用同一份配置。"""
        return self._settings

    @property
    def store(self) -> SQLiteStore:
        """回测面板要读 strategy_params 覆盖值与回测历史，共用同一连接。"""
        return self._store

    @property
    def redis(self) -> Redis:
        """启停矩阵写入后需要重建 Redis 绑定缓存。"""
        return self._redis

    # ------------------------------------------------------------------
    # 概览
    # ------------------------------------------------------------------

    async def _redis_read(self, name: str, degraded: list[str], coro):
        """读一个 Redis 数据源；失败只降级该源，不影响其它源。

        返回 None 既可能是键不存在，也可能是读取失败——后者会在 degraded
        里留下 `redis:{name}` 标记。
        """
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            degraded.append(f"redis:{name}")
            log.warning(f"overview redis read failed [{name}]: {e}")
            return None

    async def overview(self) -> dict[str, object]:
        now = int(time.time() * 1000)
        degraded: list[str] = []
        snapshot: dict | None = None
        worker: dict[str, object] = _heartbeat(None, 0, now)
        monitor: dict[str, object] = _heartbeat(None, 0, now)
        queue: dict[str, int | None] = {
            "pending": None,
            "inflight": None,
            "dead": None,
        }
        kill: dict[str, object] = {
            "active": False,
            "reason": None,
            # Redis 全挂时也要带上该字段：前端据此决定是否要求 token（P1）
            "auth_required": bool(self._settings.dashboard.token),
            "known": False,
        }

        # 各数据源独立降级并并发读取：原先一个大 try 会让「快照读失败」连带放弃
        # 心跳 / 队列 / 急停，且只留一个笼统的 "redis" 标记、定位不到子源（P2）
        mon = self._settings.monitor
        redis = self._redis
        (
            raw,
            worker_raw,
            monitor_raw,
            pending,
            inflight,
            dead,
            kill_status,
        ) = await asyncio.gather(
            self._redis_read(
                "snapshot", degraded, redis.get(mon.dashboard_snapshot_key)
            ),
            self._redis_read(
                "worker", degraded, redis.get(mon.worker_heartbeat_key)
            ),
            self._redis_read(
                "monitor", degraded, redis.get(mon.monitor_heartbeat_key)
            ),
            self._redis_read(
                "queue", degraded, redis.llen(self._settings.redis.queue_key)
            ),
            self._redis_read(
                "queue", degraded, redis.llen(self._settings.redis.inflight_key)
            ),
            self._redis_read(
                "queue", degraded, redis.llen(self._settings.redis.dead_key)
            ),
            self._redis_read("kill", degraded, self._kill_switch.status()),
        )

        snapshot = _loads(raw) if raw is not None else None
        worker = _heartbeat(worker_raw, mon.worker_heartbeat_timeout_seconds, now)
        monitor = _heartbeat(monitor_raw, mon.monitor_heartbeat_timeout_seconds, now)
        queue = {
            "pending": None if pending is None else int(pending),
            "inflight": None if inflight is None else int(inflight),
            "dead": None if dead is None else int(dead),
        }
        if kill_status is not None:
            k_active, k_reason = kill_status
            kill = {
                "active": k_active,
                "reason": k_reason,
                "auth_required": bool(self._settings.dashboard.token),
                "known": True,
            }

        # 订单计数与持仓数是两次独立 SQLite 读：并发执行、各写各的降级标记
        async def _order_counts_safe() -> dict[str, int]:
            try:
                return await asyncio.to_thread(
                    self._store.get_order_status_counts
                )
            except Exception as e:  # noqa: BLE001
                degraded.append("sqlite")
                log.warning(f"overview sqlite read failed: {e}")
                return {}

        order_counts, positions_count = await asyncio.gather(
            _order_counts_safe(),
            self._position_count(snapshot),
        )

        snapshot_ts = None
        if snapshot is not None:
            try:
                snapshot_ts = int(snapshot.get("ts") or 0) or None
            except (TypeError, ValueError):
                snapshot_ts = None
            if snapshot_ts is None or snapshot.get("account") is None:
                # 快照在但关键字段缺失（monitor 每轮都写 ts+account）：说明
                # 发布方写坏/结构变更，不能静默显示成 —（P2-17）
                degraded.append("snapshot:malformed")
            if snapshot_ts is not None and snapshot_ts > now + 5_000:
                # 快照时间在未来 = 跨机时钟漂移。年龄仍按 0 显示，但必须显式
                # 暴露，否则「数据很新」的假象会掩盖时钟不可信（P2-18）
                degraded.append("clock-skew")
                log.warning(f"snapshot ts {snapshot_ts} ahead of now {now}")
        if snapshot is None and "redis:snapshot" not in degraded:
            # 键缺失或已过 TTL（monitor 每轮刷新，停机后自然过期）：账户列会
            # 静默变成 —，必须和「读取失败」一样显式降级，否则界面看起来一切正常。
            degraded.append("snapshot")

        if positions_count is None:
            # 读库失败必须显式降级：否则前端把「取不到」显示成 0，看起来像没持仓
            degraded.append("sqlite:positions")

        # 同一来源的多次读取只保留一个标记（queue 读三个键）
        degraded = list(dict.fromkeys(degraded))

        return {
            "ts": now,
            "snapshot_ts": snapshot_ts,
            # 交易环境：来自配置而非快照。看板据此把「实盘」标红，
            # 避免在真实资金下误以为是模拟盘（M8-7）。
            "trading_mode": "demo" if self._settings.okx.demo else "live",
            "snapshot_age_seconds": _age_seconds(snapshot_ts, now),
            "account": snapshot.get("account") if snapshot else None,
            "metrics": snapshot.get("metrics") if snapshot else None,
            "positions_count": positions_count,
            "kill_switch": kill,
            "worker": worker,
            "monitor": monitor,
            "queue": queue,
            "orders": order_counts,
            # 阈值由后端下发，避免前端再硬编码一份（口径单一来源）
            "thresholds": {
                "snapshot_stale_seconds": int(
                    self._settings.risk.snapshot_max_age_ms / 1000
                ),
                "margin_ratio": self._settings.monitor.margin_ratio_threshold,
            },
            "degraded": degraded,
        }

    async def _position_count(self, snapshot: dict | None) -> int | None:
        """持仓数：与持仓表同口径（本地记账 ∪ monitor 快照）。

        只数快照会漏掉本地有仓、快照还没跟上的品种；读不到 SQLite 时返回
        None，让前端退回已拉到的持仓表长度。
        """
        try:
            local = await asyncio.to_thread(self._store.get_all_positions)
        except Exception as e:  # noqa: BLE001
            log.warning(f"overview positions read failed: {e}")
            return None
        insts = {p.inst_id for p in local if p.qty != 0}
        if snapshot is not None:
            insts |= {
                str(p.get("inst_id"))
                for p in snapshot.get("positions") or []
                if isinstance(p, dict) and p.get("inst_id")
            }
        return len(insts)

    # ------------------------------------------------------------------
    # 风控限额
    # ------------------------------------------------------------------

    async def _read_risk_limits(self) -> dict | None:
        """读取 worker 发布的生效风控参数，多策略时取最新一份。"""
        prefix = self._settings.monitor.risk_limits_key
        latest: dict | None = None
        latest_ts = -1
        keys = [
            key
            async for key in self._redis.scan_iter(
                match=f"{prefix}:*", count=50
            )
        ]
        for key in keys:
            parsed = _loads(await self._redis.get(key))
            if parsed is None:
                continue
            ts = _as_int(parsed.get("ts")) or 0
            if ts >= latest_ts:
                latest, latest_ts = parsed, ts
        return latest

    async def risk_limits(self) -> dict[str, object]:
        """风控限额仪表：生效上限 + 当前已用额度。

        上限来自 worker 发布的生效值（含 paper-mode 覆盖）；「已用」用
        本地成交/持仓现算，与风控实际判定口径一致。权益优先取 monitor
        快照，读不到时退回初始资金并标注来源。
        """
        now = int(time.time() * 1000)
        day_start = day_start_ms_utc8(now)
        degraded: list[str] = []

        # 熔断状态：与 kill switch 完全独立的按品种键（M7-13）
        circuits: list[dict[str, object]] = []
        try:
            circuits = [
                state.as_dict()
                for state in await CircuitBreaker(self._redis).states(now=now)
            ]
        except Exception as e:  # noqa: BLE001
            degraded.append("redis")
            log.warning(f"circuit states read failed: {e}")

        payload: dict | None = None
        try:
            payload = await self._read_risk_limits()
        except Exception as e:  # noqa: BLE001
            degraded.append("redis")
            log.warning(f"risk limits redis read failed: {e}")

        if payload is None:
            return {
                "ts": now,
                "known": False,
                "strategy": None,
                "paper_mode": False,
                "equity": None,
                "equity_source": None,
                "day_start_ts": day_start,
                "rules": [],
                "circuits": circuits,
                "degraded": degraded,
            }

        raw_rules = payload.get("rules")
        rules_src = raw_rules if isinstance(raw_rules, dict) else {}
        max_position_pct = _as_decimal(
            rules_src.get("max_position_pct"),
            _RISK_DEFAULTS.max_position_pct,
        )
        max_total_pct = _as_decimal(
            rules_src.get("max_total_pct"), _RISK_DEFAULTS.max_total_pct
        )
        max_order_qty = _as_decimal(
            rules_src.get("max_order_qty"), _RISK_DEFAULTS.max_order_qty
        )
        daily_loss_limit = _as_decimal(
            rules_src.get("daily_loss_limit"),
            _RISK_DEFAULTS.daily_loss_limit,
        )
        consecutive_loss_limit = (
            _as_int(rules_src.get("consecutive_loss_limit"))
            or _RISK_DEFAULTS.consecutive_loss_limit
        )

        raw_sources = payload.get("sources")
        sources_src = raw_sources if isinstance(raw_sources, dict) else {}

        def _source(name: str) -> str | None:
            value = sources_src.get(name)
            return value if isinstance(value, str) else None

        snapshot: dict | None = None
        try:
            snapshot = _loads(
                await self._redis.get(
                    self._settings.monitor.dashboard_snapshot_key
                )
            )
        except Exception as e:  # noqa: BLE001
            if "redis" not in degraded:
                degraded.append("redis")
            log.warning(f"risk limits snapshot read failed: {e}")

        equity = None
        if snapshot is not None:
            account = snapshot.get("account")
            if isinstance(account, dict):
                equity = _as_decimal_or_none(account.get("total_equity"))
        if equity is None or equity <= 0:
            equity = self._settings.backtest.initial_cash
            equity_source = "initial_cash"
        else:
            equity_source = "snapshot"

        # 标记价优先取 monitor 快照：本地均价可能停留在建仓时
        marks: dict[str, Decimal] = {}
        for item in (snapshot or {}).get("positions") or []:
            if not isinstance(item, dict):
                continue
            inst = item.get("inst_id")
            price = _as_decimal_or_none(item.get("mark_price"))
            if inst and price is not None and price > 0:
                marks[str(inst)] = price

        def mark_for(inst_id: str) -> Decimal | None:
            if inst_id in marks:
                return marks[inst_id]
            for key, price in marks.items():
                # 本地记录常为 BTC-USDT，账户持仓为 BTC-USDT-SWAP
                if key.startswith(inst_id):
                    return price
            return None

        positions: list = []
        try:
            positions = await asyncio.to_thread(self._store.get_all_positions)
        except Exception as e:  # noqa: BLE001
            degraded.append("sqlite")
            log.warning(f"risk limits positions read failed: {e}")

        total_used = Decimal("0")
        inst_used = Decimal("0")
        inst_name: str | None = None
        for pos in positions:
            price = mark_for(pos.inst_id) or pos.avg_price
            value = abs(pos.qty) * price
            total_used += value
            if value > inst_used:
                inst_used, inst_name = value, pos.inst_id

        today_pnl = Decimal("0")
        try:
            today_pnl = await asyncio.to_thread(
                self._store.get_today_realized_pnl, day_start
            )
        except Exception as e:  # noqa: BLE001
            if "sqlite" not in degraded:
                degraded.append("sqlite")
            log.warning(f"risk limits daily pnl read failed: {e}")

        streak = 0
        try:
            fills = await asyncio.to_thread(
                self._store.get_recent_trades,
                max(consecutive_loss_limit * 4, 8),
            )
            for pnl in reversed(round_trip_pnls(fills)):
                if pnl < 0:
                    streak += 1
                else:
                    break
        except Exception as e:  # noqa: BLE001
            if "sqlite" not in degraded:
                degraded.append("sqlite")
            log.warning(f"risk limits trades read failed: {e}")

        daily_used = max(-today_pnl, Decimal("0"))
        rules = [
            _limit_rule(
                "daily_loss",
                "日亏损",
                daily_used,
                equity * daily_loss_limit,
                f"UTC+8 当日已实现盈亏 {today_pnl}",
                source=_source("daily_loss_limit"),
            ),
            _limit_rule(
                "consecutive_loss",
                "连续亏损",
                Decimal(streak),
                Decimal(consecutive_loss_limit),
                f"最近连续亏损 {streak} 笔",
                source=_source("consecutive_loss_limit"),
            ),
            _limit_rule(
                "order_notional",
                "单笔上限",
                None,
                max_order_qty,
                f"单笔最大名义 {max_order_qty} USDT",
                source=_source("max_order_qty"),
            ),
            _limit_rule(
                "inst_position",
                "单品种仓位",
                inst_used,
                equity * max_position_pct,
                f"{inst_name or '暂无持仓'} 名义 {inst_used}",
                source=_source("max_position_pct"),
            ),
            _limit_rule(
                "total_position",
                "总仓位",
                total_used,
                equity * max_total_pct,
                f"全部持仓名义 {total_used}",
                source=_source("max_total_pct"),
            ),
        ]

        # 今日触发次数与最近一次详情（M7-9）：阈值「从哪来」由 source 回答，
        # 这里回答「今天有没有真的挡住过」。读失败只降级，不影响限额展示。
        stats: dict[str, dict] = {}
        try:
            stats = await asyncio.to_thread(
                self._store.risk_rule_stats, day_start
            )
        except Exception as e:  # noqa: BLE001
            if "sqlite" not in degraded:
                degraded.append("sqlite")
            log.warning(f"risk rule stats read failed: {e}")

        for rule in rules:
            event_rule = _RISK_EVENT_RULES.get(str(rule["key"]))
            stat = stats.get(event_rule) if event_rule else None
            rule["triggers"] = int(stat["count"]) if stat else 0
            rule["last_event"] = (
                _risk_event_view(stat.get("last")) if stat else None
            )

        return {
            "ts": now,
            "known": True,
            "strategy": payload.get("strategy"),
            "paper_mode": bool(payload.get("paper_mode")),
            "equity": str(equity),
            "equity_source": equity_source,
            "day_start_ts": day_start,
            "rules": rules,
            "circuits": circuits,
            "degraded": degraded,
        }

    async def risk_overrides(
        self,
        *,
        strategy_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_revoked: bool = True,
    ) -> dict[str, object]:
        """在线阈值覆盖与变更审计（M7-8 / M7-14）。"""
        try:
            data = await self._overrides.list(
                strategy_id=strategy_id,
                limit=limit,
                offset=offset,
                include_revoked=include_revoked,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"list risk overrides failed: {e}")
            return {
                "ts": int(time.time() * 1000),
                "strategy_id": strategy_id,
                "effective": {},
                "items": [],
                "total": None,
                "degraded": ["sqlite"],
            }
        data["degraded"] = []
        return data

    async def set_risk_override(
        self,
        strategy_id: str,
        param: str,
        value: str | int | float | Decimal,
        *,
        reason: str,
        by: str = "dashboard",
    ) -> dict[str, object]:
        """写入一条在线覆盖。

        Raises:
            OverrideError: 字段名/值/原因不合法，转由 API 层返回 400。
        """
        return await self._overrides.set(
            strategy_id, param, value, reason=reason, by=by
        )

    async def clear_risk_override(
        self, strategy_id: str, param: str, *, by: str = "dashboard"
    ) -> dict[str, object]:
        """清除覆盖，回落到配置层。"""
        return await self._overrides.clear(strategy_id, param, by=by)

    async def resume_circuit(self, inst_id: str) -> dict[str, object]:
        """人工确认恢复某个品种的熔断（M7-12）。

        只允许在冷却期结束后操作：冷却还没走完就放行，等于把熔断阈值
        变成摆设。恢复动作写 `risk_events` 并发一条 INFO 告警，保证
        「谁在什么时候解除了熔断」可追溯。
        """
        now = int(time.time() * 1000)
        breaker = CircuitBreaker(self._redis)
        state = await breaker.state(inst_id, now=now)
        if state is None:
            return {
                "inst_id": inst_id,
                "resumed": False,
                "reason": "not tripped",
            }
        if state.status != STATUS_AWAITING_RESUME:
            return {
                "inst_id": inst_id,
                "resumed": False,
                "reason": "cooldown not finished",
                "resume_at": state.resume_at,
            }
        resumed = await breaker.resume(inst_id)
        if resumed is None:
            # 竞态：读取与删除之间别的进程已经恢复
            return {
                "inst_id": inst_id,
                "resumed": False,
                "reason": "not tripped",
            }

        try:
            await asyncio.to_thread(
                self._store.insert_risk_event,
                event_id=build_event_id(
                    f"circuit-resume:{inst_id}", now, "circuit_breaker"
                ),
                ts=now,
                inst_id=inst_id,
                signal_id=f"circuit-resume:{inst_id}",
                decision="resumed",
                rule="circuit_breaker",
                limit_name="circuit_amplitude_pct",
                reason=f"manual resume: {resumed.reason}",
            )
        except Exception as e:  # noqa: BLE001
            log.error(f"record circuit resume event failed: {e}")

        try:
            await self._alerts.publish(
                AlertEvent(
                    rule="circuit_breaker",
                    level="INFO",
                    message=f"{inst_id} 熔断已人工确认恢复，恢复交易",
                    ts=now,
                    inst_id=inst_id,
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"publish circuit resume alert failed: {e}")

        return {
            "inst_id": inst_id,
            "resumed": True,
            "triggered_ts": resumed.triggered_ts,
            "reason": resumed.reason,
        }

    # ------------------------------------------------------------------
    # 策略引擎
    # ------------------------------------------------------------------

    @property
    def _engine_marker_key(self) -> str:
        cfg = self._settings.engine
        return f"{cfg.last_bar_key}:{cfg.strategy}:{cfg.inst}"

    @property
    def _engine_status_key(self) -> str:
        cfg = self._settings.engine
        return f"{cfg.status_key}:{cfg.strategy}:{cfg.inst}"

    async def engine(self) -> dict[str, object]:
        """引擎健康度：状态心跳、已评估 bar、水位延迟与补评估计数。

        数据来源都是只读键：engine 每轮发布的 `status_key` 与水位
        `last_bar_key`；本地最新已收盘 bar 由 parquet 现取，用于判断
        水位落后了几根。
        """
        now = int(time.time() * 1000)
        cfg = self._settings.engine
        degraded: list[str] = []

        status = None
        watermark_ts = None
        try:
            status = _loads(await self._redis.get(self._engine_status_key))
            watermark_ts = _as_int(await self._redis.get(self._engine_marker_key))
        except Exception as e:  # noqa: BLE001
            degraded.append("redis")
            log.warning(f"engine redis read failed: {e}")

        bar_ms = None
        try:
            bar_ms = DataValidator._bar_size_ms(cfg.bar)  # type: ignore[attr-defined]
        except ValueError:
            degraded.append("bar")

        local_bar_ts = None
        if bar_ms is not None:
            last_closed = ((now - bar_ms) // bar_ms) * bar_ms
            try:
                bars = await asyncio.to_thread(
                    self._bars.load_bars,
                    cfg.inst,
                    cfg.bar,
                    last_closed - 72 * bar_ms,
                    last_closed,
                )
                closed = [b for b in bars if b.ts <= last_closed]
                if closed:
                    local_bar_ts = closed[-1].ts
            except Exception as e:  # noqa: BLE001
                degraded.append("candles")
                log.warning(f"engine local bar read failed: {e}")

        last_bar_ts = _field_int(status, "bar_ts")
        status_ts = _field_int(status, "ts")
        lag_bars = None
        if (
            bar_ms is not None
            and watermark_ts is not None
            and local_bar_ts is not None
        ):
            lag_bars = max((local_bar_ts - watermark_ts) // bar_ms, 0)

        online = (
            status_ts is not None
            and (now - status_ts) / 1000 <= cfg.status_timeout_seconds
        )

        return {
            "ts": now,
            "strategy": cfg.strategy,
            "inst": cfg.inst,
            "bar": cfg.bar,
            "online": online,
            "status_age_seconds": _age_seconds(status_ts, now),
            "timeout_seconds": cfg.status_timeout_seconds,
            "status": status.get("status") if status else None,
            "reason": status.get("reason") if status else None,
            "last_bar_ts": last_bar_ts,
            "last_bar_age_seconds": _age_seconds(last_bar_ts, now),
            "watermark_ts": watermark_ts,
            "watermark_age_seconds": _age_seconds(watermark_ts, now),
            "local_bar_ts": local_bar_ts,
            "local_bar_age_seconds": _age_seconds(local_bar_ts, now),
            "lag_bars": lag_bars,
            "catch_up": _field_int(status, "catch_up"),
            "catch_up_total": _field_int(status, "catch_up_total"),
            "skipped_total": _field_int(status, "skipped_total"),
            "signals_total": _field_int(status, "signals_total"),
            "ticks": _field_int(status, "ticks"),
            "started_ts": _field_int(status, "started_ts"),
            "max_catchup_bars": cfg.max_catchup_bars,
            "degraded": degraded,
        }

    async def evaluations(
        self, strategy_id: str, limit: int = 10
    ) -> dict[str, object]:
        """M3-15 策略详情页：该策略最近 N 个「评估状态变化点」。

        数据来自 engine 每轮 `publish_status` 写入 Redis 的定长列表，按绑定
        的 `inst` 分别读取后合并、按 bar 时间倒序。连续相同状态（数据落后时
        的 waiting_data、正常运行时高频的 duplicate）折叠为最新一条——否则
        定长列表会被低信息量轮次占满，有信号的轮次根本挤不进来（P2）。
        原始轮次仍完整留在 Redis，只是展示层折叠。
        """
        now = int(time.time() * 1000)
        limit = max(1, min(limit, 10))
        # 扫描全部原始轮次再折叠；条数直接引用引擎常量，改一处即同步
        history_scan = EVAL_HISTORY_LIMIT
        degraded: list[str] = []

        try:
            bindings = await asyncio.to_thread(
                self._store.list_bindings, strategy_id
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"evaluations bindings read failed: {e}")
            bindings = []
            degraded.append("sqlite")

        pairs = [
            (str(b["inst_id"]), str(b.get("bar") or "")) for b in bindings
        ]
        if not pairs:
            cfg = self._settings.engine
            pairs = [(cfg.inst, cfg.bar)]

        status_key = self._settings.engine.status_key
        items: list[dict[str, object]] = []
        for inst_id, bar in pairs:
            key = f"{status_key}:history:{strategy_id}:{inst_id}"
            try:
                raw = await self._redis.lrange(key, 0, history_scan - 1)
            except Exception as e:  # noqa: BLE001
                log.warning(f"evaluations redis read failed: {e}")
                if "redis" not in degraded:
                    degraded.append("redis")
                continue
            for chunk in raw:
                payload = _loads(chunk)
                if payload is None:
                    continue
                items.append(
                    {
                        "inst_id": payload.get("inst") or inst_id,
                        "bar": payload.get("bar") or bar,
                        "status": payload.get("status"),
                        "bar_ts": _as_int(payload.get("bar_ts")),
                        "reason": payload.get("reason"),
                        "signal_count": _as_int(
                            payload.get("signal_count")
                        )
                        or 0,
                        "ts": _as_int(payload.get("ts")),
                    }
                )

        items.sort(key=lambda row: row["bar_ts"] or 0, reverse=True)
        # 折叠同一组合连续相同的 (status, reason)：每段只留最新一条
        collapsed: list[dict[str, object]] = []
        last_state: dict[str, tuple[str, str]] = {}
        for row in items:
            inst = str(row["inst_id"])
            state = (str(row["status"]), str(row["reason"]))
            if last_state.get(inst) == state:
                continue
            last_state[inst] = state
            collapsed.append(row)
            if len(collapsed) >= limit:
                break
        return {
            "ts": now,
            "strategy_id": strategy_id,
            "items": collapsed,
            "degraded": degraded,
        }

    async def freshness(self) -> dict[str, object]:
        """M1-7 数据新鲜度：引擎消费的 `inst × bar` 是否落后。

        `lag_bars` 直接复用 `engine()` 的口径（本地最新已收盘 bar 与引擎
        水位之差）。另算 `missing_bars`——本地最新 bar 相对「此刻应有 bar」
        的缺口：断网时 candles 停止同步，引擎水位与本地末尾仍然相等
        （lag_bars=0），只看 lag_bars 会漏掉这种「一起卡住」的情况。

        当前 engine 只发布主组合（`engine.inst` × `engine.bar`）的水位与
        状态键，因此只返回这一条；多绑定的逐组合新鲜度需要 engine 另行
        发布各组合水位，不在本期范围。
        """
        now = int(time.time() * 1000)
        cfg = self._settings.engine
        eng = await self.engine()
        degraded = list(eng.get("degraded") or [])  # type: ignore[arg-type]

        bar_ms: int | None = None
        try:
            bar_ms = DataValidator._bar_size_ms(cfg.bar)  # type: ignore[attr-defined]
        except ValueError:
            if "bar" not in degraded:
                degraded.append("bar")

        local_bar_ts = eng.get("local_bar_ts")
        local_ts = local_bar_ts if isinstance(local_bar_ts, int) else None
        expected_ts: int | None = None
        missing_bars: int | None = None
        if bar_ms is not None:
            expected_ts = ((now - bar_ms) // bar_ms) * bar_ms
            if local_ts is not None:
                missing_bars = max((expected_ts - local_ts) // bar_ms, 0)

        lag_raw = eng.get("lag_bars")
        lag_bars = lag_raw if isinstance(lag_raw, int) else None
        stale = (
            lag_bars is not None and lag_bars >= FRESHNESS_LAG_THRESHOLD
        ) or (missing_bars is not None and missing_bars >= FRESHNESS_LAG_THRESHOLD)

        item = {
            "inst_id": cfg.inst,
            "bar": cfg.bar,
            "last_bar_ts": local_ts,
            "lag_seconds": _age_seconds(local_ts, now),
            "lag_bars": lag_bars,
            "expected_ts": expected_ts,
            "missing_bars": missing_bars,
            "engine_watermark_ts": eng.get("watermark_ts"),
            "stale": stale,
            "degraded": degraded,
        }
        return {
            "ts": now,
            "threshold_bars": FRESHNESS_LAG_THRESHOLD,
            "items": [item],
            "stale_count": 1 if stale else 0,
            "degraded": degraded,
        }

    # ------------------------------------------------------------------
    # 明细
    # ------------------------------------------------------------------

    async def positions(self) -> dict[str, object]:
        now = int(time.time() * 1000)
        degraded: list[str] = []
        local = []
        try:
            local = await asyncio.to_thread(self._store.get_all_positions)
        except Exception as e:  # noqa: BLE001
            degraded.append("sqlite")
            log.warning(f"positions sqlite read failed: {e}")

        snapshot = None
        try:
            snapshot = _loads(
                await self._redis.get(
                    self._settings.monitor.dashboard_snapshot_key
                )
            )
        except Exception as e:  # noqa: BLE001
            degraded.append("redis")
            log.warning(f"positions redis read failed: {e}")
        if snapshot is None and "redis" not in degraded:
            # 快照缺失/过期：账户数量、均价、标记价都会是空，不能装作正常
            degraded.append("snapshot")

        remote = {
            str(p.get("inst_id")): p
            for p in (snapshot or {}).get("positions") or []
            if p.get("inst_id")
        }
        merged: list[dict[str, object]] = []
        for pos in local:
            # 已平掉的仓位不再作为持仓展示；若快照里仍有（本地落后），
            # 会走下面的 remote 分支单独成行
            if pos.qty == 0:
                continue
            merged.append(_merge_position(pos, remote.pop(pos.inst_id, None)))
        for item in remote.values():
            merged.append(_merge_position(None, item))

        return {
            "ts": now,
            "positions": merged,
            "degraded": degraded,
        }

    async def orders(self, limit: int = 50) -> dict[str, object]:
        now = int(time.time() * 1000)
        try:
            rows = await asyncio.to_thread(self._store.get_recent_orders, limit)
        except Exception as e:  # noqa: BLE001
            log.warning(f"orders sqlite read failed: {e}")
            return {"ts": now, "orders": [], "degraded": ["sqlite"]}
        return {"ts": now, "orders": rows, "degraded": []}

    async def fills(self, limit: int = 50) -> dict[str, object]:
        now = int(time.time() * 1000)
        try:
            trades = await asyncio.to_thread(
                self._store.get_recent_trades, limit
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"fills sqlite read failed: {e}")
            return {"ts": now, "fills": [], "degraded": ["sqlite"]}

        items = [_fill_to_dict(f, now) for f in reversed(trades)]
        total_fee = sum((f.fee for f in trades), Decimal("0"))
        return {
            "ts": now,
            "fills": items,
            "total_fee": str(total_fee),
            "degraded": [],
        }

    async def pnl(self) -> dict[str, object]:
        """成交盈亏汇总：按 FIFO 配对统计已平仓回合。

        统计口径与风控 `round_trip_pnls` 完全一致（含手续费），
        未平仓部分不计入回合数，因此总盈亏是「已实现」而非浮动盈亏。
        """
        now = int(time.time() * 1000)
        try:
            fills = await asyncio.to_thread(self._store.get_all_fills)
        except Exception as e:  # noqa: BLE001
            log.warning(f"pnl sqlite read failed: {e}")
            return {
                "ts": now,
                "fill_count": 0,
                "first_ts": None,
                "last_ts": None,
                "turnover": "0",
                "fee": "0",
                "round_trips": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": None,
                "profit_factor": None,
                "total_pnl": "0",
                "avg_win": None,
                "avg_loss": None,
                "best": None,
                "worst": None,
                "degraded": ["sqlite"],
            }

        pnls = round_trip_pnls(fills)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins, Decimal("0"))
        gross_loss = -sum(losses, Decimal("0"))

        fee = sum((f.fee for f in fills), Decimal("0"))
        turnover = sum((f.qty * f.price for f in fills), Decimal("0"))
        # 无亏损回合时盈亏比无定义，返回 None 而非无穷大
        profit_factor = gross_win / gross_loss if gross_loss > 0 else None
        win_rate = (
            Decimal(len(wins)) / Decimal(len(pnls)) if pnls else None
        )

        return {
            "ts": now,
            "fill_count": len(fills),
            "first_ts": fills[0].ts if fills else None,
            "last_ts": fills[-1].ts if fills else None,
            "turnover": str(turnover),
            "fee": str(fee),
            "round_trips": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": _ratio_text(win_rate),
            "profit_factor": _ratio_text(profit_factor),
            "total_pnl": str(gross_win - gross_loss),
            "avg_win": str(gross_win / len(wins)) if wins else None,
            "avg_loss": str(-gross_loss / len(losses)) if losses else None,
            "best": str(max(pnls)) if pnls else None,
            "worst": str(min(pnls)) if pnls else None,
            "degraded": [],
        }

    async def today_pnl(self) -> dict[str, object]:
        """M1-8 今日盈亏汇总：按品种拆分已实现盈亏 + 未实现盈亏 + 手续费。

        已实现盈亏：今日成交的 FIFO 回合盈亏（含手续费）
        未实现盈亏：当前持仓按标记价估算的浮动盈亏
        手续费：今日成交的总手续费
        """
        now = int(time.time() * 1000)
        day_start = day_start_ms_utc8(now)

        # 三路读取互不依赖，并发执行（原先串行三次 await，纯浪费延迟）
        degraded: list[str] = []

        async def _read_by_inst() -> dict | None:
            try:
                return await asyncio.to_thread(
                    self._store.get_today_pnl_by_inst, day_start
                )
            except Exception as e:  # noqa: BLE001
                log.warning(f"today_pnl sqlite read failed: {e}")
                degraded.append("sqlite")
                return None

        async def _read_positions() -> list:
            try:
                return await asyncio.to_thread(self._store.get_all_positions)
            except Exception as e:  # noqa: BLE001
                log.warning(f"today_pnl positions read failed: {e}")
                # 静默返回 [] 会把浮动盈亏显示成 0、看起来没有风险，必须降级
                degraded.append("sqlite:positions")
                return []

        async def _read_snapshot() -> dict | None:
            try:
                raw = await self._redis.get(
                    self._settings.monitor.dashboard_snapshot_key
                )
                return _loads(raw)
            except Exception as e:  # noqa: BLE001
                log.warning(f"today_pnl snapshot read failed: {e}")
                return None

        by_inst, positions, snapshot = await asyncio.gather(
            _read_by_inst(),
            _read_positions(),
            _read_snapshot(),
        )
        if by_inst is None:
            return {
                "ts": now,
                "day_start": day_start,
                "instruments": [],
                "total_realized": "0",
                "total_unrealized": "0",
                "total_fee": "0",
                "total_turnover": "0",
                "total_pnl": "0",
                "degraded": list(dict.fromkeys(degraded)),
            }

        # 4. 构建标记价映射
        mark_prices: dict[str, Decimal] = {}
        if snapshot and "positions" in snapshot:
            for pos in snapshot["positions"]:
                inst_id = pos.get("inst_id")
                mark_price = _as_decimal_or_none(pos.get("mark_price"))
                if inst_id and mark_price is not None:
                    mark_prices[inst_id] = mark_price

        # 5. 合并已实现 + 未实现
        inst_data: dict[str, dict] = {}
        for inst_id, data in by_inst.items():
            inst_data[inst_id] = {
                "inst_id": inst_id,
                "realized": data["realized"],
                "unrealized": Decimal("0"),
                "fee": data["fee"],
                "turnover": data["turnover"],
            }

        # 6. 加入持仓的未实现盈亏
        for pos in positions:
            if pos.qty == 0:
                continue
            inst_id = pos.inst_id
            mark_price = mark_prices.get(inst_id)
            if mark_price is None:
                # 没有标记价，用均价估算（不精确但至少有值）
                mark_price = pos.avg_price
            unrealized = pos.unrealized_pnl(mark_price)

            if inst_id in inst_data:
                inst_data[inst_id]["unrealized"] = unrealized
            else:
                inst_data[inst_id] = {
                    "inst_id": inst_id,
                    "realized": Decimal("0"),
                    "unrealized": unrealized,
                    "fee": Decimal("0"),
                    "turnover": Decimal("0"),
                }

        # 7. 汇总
        instruments = []
        total_realized = Decimal("0")
        total_unrealized = Decimal("0")
        total_fee = Decimal("0")
        total_turnover = Decimal("0")

        for data in inst_data.values():
            total_realized += data["realized"]
            total_unrealized += data["unrealized"]
            total_fee += data["fee"]
            total_turnover += data["turnover"]
            instruments.append({
                "inst_id": data["inst_id"],
                "realized": str(data["realized"]),
                "unrealized": str(data["unrealized"]),
                "fee": str(data["fee"]),
                "turnover": str(data["turnover"]),
                "pnl": str(data["realized"] + data["unrealized"]),
            })

        # 按总盈亏排序（降序）
        instruments.sort(
            key=lambda x: Decimal(x["pnl"]),
            reverse=True,
        )

        return {
            "ts": now,
            "day_start": day_start,
            "instruments": instruments,
            "total_realized": str(total_realized),
            "total_unrealized": str(total_unrealized),
            "total_fee": str(total_fee),
            "total_turnover": str(total_turnover),
            "total_pnl": str(total_realized + total_unrealized),
            "degraded": [],
        }

    async def set_kill_switch(
        self, active: bool, reason: str = ""
    ) -> dict[str, object]:
        """切换紧急停止标志。

        只写 Redis 标志，不直接撤挂单、不停进程：worker 消费信号前、
        engine 每轮评估前都会重新读取该键，所以改完立即生效。
        看板路由在激活后会另行调用 `ManualTrader.cancel_open_orders`；
        CLI 用 `quant kill-switch --on` 走同样的交易所批量撤单。
        """
        if active:
            await self._kill_switch.enable(reason)
        else:
            await self._kill_switch.disable()
        return {
            "active": active,
            "reason": reason or None,
            "auth_required": bool(self._settings.dashboard.token),
            "degraded": [],
        }

    async def quality(
        self, inst_id: str | None = None, bar_size: str | None = None
    ) -> dict[str, object]:
        """数据质量报告（MS4-C §9.4）。

        传 `inst_id`+`bar_size` 返回单份完整报告（含 gaps/invalid_ohlc）；
        不传参返回全部 `latest.json` 的摘要列表。
        参数只传一个由路由层直接 422：这里的响应形状必须与
        QualityListResponse / QualityDetailResponse 之一严格匹配，
        否则前端按错误形状解析会 TypeError（P1）。
        """
        now = int(time.time() * 1000)
        reporter = QualityReporter(self._settings.storage.data_dir)
        try:
            if inst_id and bar_size:
                report = await asyncio.to_thread(
                    reporter.load, inst_id, bar_size
                )
                return {
                    "ts": now,
                    "inst_id": inst_id,
                    "bar_size": bar_size,
                    "report": report,
                    "degraded": [],
                }
            reports = await asyncio.to_thread(reporter.summaries)
        except Exception as e:  # noqa: BLE001
            log.warning(f"quality read failed: {e}")
            # 形状必须与调用模式一致：detail 模式也要带 report 键（null），
            # 否则前端 QualityDetail 对 undefined 解引用直接崩（P1）
            if inst_id and bar_size:
                return {
                    "ts": now,
                    "inst_id": inst_id,
                    "bar_size": bar_size,
                    "report": None,
                    "degraded": ["quality"],
                }
            return {"ts": now, "reports": [], "degraded": ["quality"]}
        return {"ts": now, "reports": reports, "degraded": []}

    async def dead_letters(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        stage: str | None = None,
        since: int | None = None,
        only_pending: bool = False,
    ) -> dict[str, object]:
        """死信列表（MS4-B §12）。payload 只给预览，避免整页被原始 body 撑爆。"""
        now = int(time.time() * 1000)
        try:
            items, total = await asyncio.to_thread(
                self._store.list_dead_letters,
                limit,
                offset,
                stage=stage,
                since=since,
                only_pending=only_pending,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"dead letters read failed: {e}")
            return {
                "ts": now,
                "items": [],
                "total": None,
                "degraded": ["sqlite"],
            }
        return {
            "ts": now,
            "items": [_dead_letter_row(row) for row in items],
            "total": total,
            "degraded": [],
        }

    async def replay_dead_letter(self, dead_id: str) -> dict[str, object]:
        """单条重放：requeued / already_processed / already_replayed / ..."""
        return await self._dead.replay(dead_id)

    async def replay_dead_letters(self, ids: list[str]) -> dict[str, object]:
        """批量重放，逐条独立返回。"""
        return {"results": await self._dead.replay_many(ids)}

    async def discard_dead_letter(self, dead_id: str) -> dict[str, object]:
        return await self._dead.discard(dead_id)

    async def reconciliations(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        kind: str | None = None,
        only_open: bool = False,
    ) -> dict[str, object]:
        """三项对账差异列表（M8-18）。"""
        now = int(time.time() * 1000)
        try:
            items, total = await asyncio.to_thread(
                self._store.list_reconciliations,
                limit,
                offset,
                kind=kind,
                only_open=only_open,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"reconciliations read failed: {e}")
            return {
                "ts": now,
                "items": [],
                "total": None,
                "degraded": ["sqlite"],
            }
        return {
            "ts": now,
            "items": [_reconciliation_row(row) for row in items],
            "total": total,
            "degraded": [],
        }

    async def resolve_reconciliation(
        self, rec_id: str, *, by: str = "dashboard"
    ) -> dict[str, object]:
        """标记一条对账差异已处理（幂等）。"""
        changed = await asyncio.to_thread(
            self._store.resolve_reconciliation,
            rec_id,
            by=by,
            now_ms=int(time.time() * 1000),
        )
        return {
            "rec_id": rec_id,
            "result": "resolved" if changed else "already_resolved",
        }

    async def reports(self, limit: int = 20) -> dict[str, object]:
        """日报列表：读取 monitor 落盘的偏离报告 CSV。

        文件名格式 `{date}-{strategy}-{inst}.csv`，内容为
        metric/paper/backtest/deviation 四列。这里只做解析与判定，
        不重新计算，保证展示与当时邮件内容一致。
        """
        now = int(time.time() * 1000)
        cfg = self._settings.report
        out_dir = Path(cfg.out_dir)
        try:
            items = await asyncio.to_thread(
                _read_reports, out_dir, limit, cfg.threshold
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"reports read failed: {e}")
            return {
                "ts": now,
                "out_dir": str(out_dir),
                "threshold": cfg.threshold,
                "reports": [],
                "degraded": ["reports"],
            }
        return {
            "ts": now,
            "out_dir": str(out_dir),
            "threshold": cfg.threshold,
            "reports": items,
            "degraded": [],
        }

    async def alerts(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        level: str | None = None,
        rule: str | None = None,
        state: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> dict[str, object]:
        """告警列表：以 SQLite 为准，读失败回落 Redis 实时缓存。

        回落时 `total=None`、`degraded=["sqlite"]`——让 UI 明示「只有最近
        200 条」，而不是把不完整结果当成全量。
        """
        now = int(time.time() * 1000)
        silences: list[dict] = []
        rows: list[dict] = []
        total: int | None = None
        degraded: list[str] = []

        if self._settings.monitor.alerts_persist:
            try:
                rows, total = await asyncio.to_thread(
                    self._alerts.list,
                    limit,
                    level=level,
                    rule=rule,
                    state=state,
                    since_ms=since_ms,
                    until_ms=until_ms,
                    offset=offset,
                )
                silences = await asyncio.to_thread(
                    self._alerts.active_silences, now
                )
            except Exception as e:  # noqa: BLE001
                log.warning(f"alerts sqlite read failed: {e}")
                degraded.append("sqlite")
                rows, total = [], None

        if total is None:
            try:
                raw_items = await self._redis.lrange(
                    self._settings.monitor.dashboard_alerts_key,
                    0,
                    limit - 1,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(f"alerts redis read failed: {e}")
                degraded.append("redis")
                raw_items = []
            rows = [
                parsed
                for raw in raw_items
                if (parsed := _loads(raw)) is not None
            ]

        return {
            "ts": now,
            "total": total,
            "alerts": rows,
            "silences": silences,
            # 规则级启停（M9-2）：让页面能明示哪些规则被有意关掉，
            # 而不是让人以为「这条规则从不触发」
            "disabled_rules": list(self._settings.monitor.disabled_rules),
            "degraded": degraded,
        }

    async def alert_stats(self, days: int = 7) -> dict[str, object]:
        """近 N 天各规则触发次数（M9-14）。

        `days` 只接受 1~90，超出按边界收口——前端传错不该让整张卡报错。
        """
        days = max(1, min(int(days), 90))
        now = int(time.time() * 1000)
        since = now - days * 86_400_000
        # 规则计数（带时间窗）与未读汇总（全量）是两条独立查询，并发执行
        try:
            rules, summary = await asyncio.gather(
                asyncio.to_thread(self._store.alert_rule_counts, since),
                asyncio.to_thread(self._store.alert_summary_counts),
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"alert stats read failed: {e}")
            return {
                "ts": now,
                "days": days,
                "since": since,
                "total": 0,
                "rules": [],
                "unread": 0,
                "by_level": {"ERROR": 0, "WARNING": 0, "INFO": 0},
                "total_all": 0,
                "degraded": ["sqlite"],
            }
        return {
            "ts": now,
            "days": days,
            "since": since,
            "total": sum(int(item["total"]) for item in rules),
            "rules": rules,
            # 未读/级别计数不带时间窗：未读是「还欠处理」的动作项，历史遗留
            # 的 open 也要计入；看板摘要据此前端不再每 5s 数 200 条（P2-20）
            "unread": int(summary["unread"]),
            "by_level": {
                "ERROR": int(summary["errors"]),
                "WARNING": int(summary["warnings"]),
                "INFO": int(summary["infos"]),
            },
            "total_all": int(summary["total_all"]),
            "degraded": [],
        }

    async def ack_alert(self, alert_id: str, by: str) -> dict[str, object]:
        ok = await self._alerts.ack(alert_id, by)
        return {"alert_id": alert_id, "acked": ok}

    async def create_silence(
        self,
        rule: str,
        *,
        inst_id: str | None,
        hours: float,
        reason: str,
        by: str,
    ) -> dict[str, object]:
        silence_id = await self._alerts.silence(
            rule, inst_id=inst_id, hours=hours, reason=reason, by=by
        )
        return {"silence_id": silence_id}

    async def revoke_silence(self, silence_id: str) -> dict[str, object]:
        revoked = await self._alerts.revoke_silence(silence_id)
        return {"silence_id": silence_id, "revoked": revoked}

    # ------------------------------------------------------------------
    # 今日事件流（M1-6）
    # ------------------------------------------------------------------

    #: 事件类型 → 跳转目标（MS4-A §8.2；前端再映射到自己的 tab key）
    _TIMELINE_MODULES = {
        "alert": "alerts",
        "signal": "strategies",
        "order": "execution",
        "fill": "execution",
    }

    async def timeline(
        self,
        kinds: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        """当日信号/下单/成交/告警合并流（MS4-A §8）。

        `since` 取当日 UTC+8 00:00，与风控日切口径一致；读失败只让这张卡
        降级为 `degraded=["sqlite"]`，不影响首页其余卡片。
        """
        now = int(time.time() * 1000)
        since = day_start_ms_utc8(now)
        try:
            rows = await asyncio.to_thread(
                self._store.daily_timeline, since, limit, offset, kinds
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"timeline read failed: {e}")
            return {
                "ts": now,
                "since": since,
                "items": [],
                "degraded": ["sqlite"],
            }
        return {
            "ts": now,
            "since": since,
            "items": [
                {
                    **row,
                    "module": self._TIMELINE_MODULES.get(
                        str(row.get("kind")), "overview"
                    ),
                }
                for row in rows
            ],
            "degraded": [],
        }

    # ------------------------------------------------------------------
    # 进程健康
    # ------------------------------------------------------------------

    async def processes(self) -> dict[str, object]:
        """7 个常驻进程的判活视图。

        `online` 用**该进程自己的 TTL**（不是统一 2×interval）：monitor 的
        写方是 300s，按 120s 判会得到「键还在但页面说离线」。
        """
        now = int(time.time() * 1000)
        degraded: list[str] = []

        supervisor_raw: object = None
        try:
            supervisor_raw = await self._redis.get(SUPERVISOR_KEY)
        except Exception as e:  # noqa: BLE001
            log.warning(f"processes redis read failed: {e}")
            degraded.append("redis")

        table = _loads(supervisor_raw)
        children: dict = {}
        if isinstance(table, dict) and isinstance(table.get("children"), dict):
            children = table["children"]

        items: list[dict[str, object]] = []
        for spec in PROCESS_SPECS:
            ttl = self._process_ttl(spec.name, spec.ttl_seconds)
            interval = self._process_interval(spec.name, spec.interval_seconds)
            if spec.name == "supervisor":
                sup_ts, sup_pid = parse_heartbeat(supervisor_raw)
                items.append(
                    _process_item(
                        spec.name,
                        raw_present=supervisor_raw is not None,
                        ts=sup_ts,
                        pid=sup_pid,
                        ttl=ttl,
                        now_ms=now,
                        interval_seconds=interval,
                        restarts=None,
                        last_exit=None,
                    )
                )
                continue

            if table is not None and spec.name not in children:
                # supervisor 在线但没托管这个进程（如 --no-dashboard）
                items.append(
                    _process_item(
                        spec.name,
                        raw_present=False,
                        ts=None,
                        pid=None,
                        ttl=ttl,
                        now_ms=now,
                        interval_seconds=interval,
                        restarts=None,
                        last_exit=None,
                        reason="not_managed",
                    )
                )
                continue

            try:
                raw_key = await self._redis.get(
                    f"{HEARTBEAT_PREFIX}:{spec.name}"
                )
            except Exception as e:  # noqa: BLE001
                log.warning(f"process heartbeat read failed {spec.name}: {e}")
                raw_key = None
            raw_ts, pid = parse_heartbeat(raw_key)
            child = children.get(spec.name) or {}
            items.append(
                _process_item(
                    spec.name,
                    raw_present=raw_key is not None,
                    ts=raw_ts,
                    pid=pid,
                    ttl=ttl,
                    now_ms=now,
                    interval_seconds=interval,
                    restarts=_child_int(child, "restarts"),
                    last_exit=_child_int(child, "last_exit"),
                    child_pid=_child_int(child, "pid"),
                )
            )

        # M2-2：worker 用 --ws 启动时会把连接状态随心跳写进来；未启用则无此键
        try:
            ws_raw = await self._redis.get(ws_status_key())
        except Exception as e:  # noqa: BLE001
            log.warning(f"ws status read failed: {e}")
            ws_raw = None
        ws_status = _ws_status(_loads(ws_raw))
        if ws_status is not None:
            worker_item = next((i for i in items if i["name"] == "worker"), None)
            if worker_item is not None:
                worker_item["ws"] = ws_status

        online = sum(1 for i in items if i["online"])
        return {
            "ts": now,
            "processes": items,
            "online": online,
            "total": len(items),
            "offline": [i["name"] for i in items if not i["online"]],
            "degraded": degraded,
        }
    def _process_ttl(self, name: str, fallback: int) -> int:
        """优先用配置里的实际周期派生，避免与写入方的 TTL 不一致。"""
        monitor = self._settings.monitor
        if name == "worker":
            return monitor.worker_heartbeat_timeout_seconds
        if name == "monitor":
            return monitor.monitor_heartbeat_timeout_seconds
        if name == "candles":
            return default_ttl(self._settings.candles.interval_seconds)
        if name == "engine":
            return default_ttl(self._settings.engine.interval_seconds)
        return fallback

    def _process_interval(self, name: str, fallback: int) -> int:
        if name == "candles":
            return self._settings.candles.interval_seconds
        if name == "engine":
            return self._settings.engine.interval_seconds
        if name == "monitor":
            return self._settings.monitor.interval_seconds
        return fallback

    async def equity(self, hours: int = 24) -> dict[str, object]:
        """权益时序（monitor 每轮落库的一条快照）。"""
        now = int(time.time() * 1000)
        since = now - hours * 3600 * 1000
        try:
            points = await asyncio.to_thread(
                self._store.get_equity_history, since
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"equity sqlite read failed: {e}")
            return {"ts": now, "hours": hours, "points": [], "degraded": ["sqlite"]}

        return {
            "ts": now,
            "hours": hours,
            "points": points,
            "degraded": [],
        }

    async def candles(
        self,
        inst: str | None = None,
        bar: str | None = None,
        limit: int = 300,
    ) -> dict[str, object]:
        """K 线面板数据：本地 parquet K 线 + 该品种成交 + 本地持仓均价。

        不做任何计算，只把已有数据拼成一个响应，供前端一次拉取。
        本地无 K 线数据时返回空数组并标记 degraded，页面据此提示先同步。
        """
        now = int(time.time() * 1000)
        inst = inst or self._settings.engine.inst
        bar = bar or self._settings.engine.bar
        degraded: list[str] = []

        try:
            available = await asyncio.to_thread(self._bars.list_bar_sizes, inst)
        except Exception as e:  # noqa: BLE001
            log.warning(f"candles list_bar_sizes failed: {e}")
            available = []
        available.sort(key=_bar_size_key)

        try:
            bar_ms = DataValidator._bar_size_ms(bar)  # type: ignore[attr-defined]
        except ValueError:
            return {
                "ts": now,
                "inst": inst,
                "bar": bar,
                "available_bars": available,
                "bars": [],
                "fills": [],
                "position": None,
                "degraded": ["bar"],
            }

        since = now - (limit + 1) * bar_ms

        try:
            bars = await asyncio.to_thread(
                self._bars.load_bars, inst, bar, since, now
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"candles read failed: {e}")
            bars = []
            degraded.append("candles")
        if not bars and "candles" not in degraded:
            degraded.append("candles")

        fills: list[dict[str, object]] = []
        position: dict[str, str] | None = None
        try:
            recent = await asyncio.to_thread(self._store.get_recent_trades, 500)
            fills = [
                {
                    "ts": f.ts,
                    "side": f.side,
                    "qty": str(f.qty),
                    "price": str(f.price),
                }
                for f in reversed(recent)
                if f.inst_id == inst and f.ts >= since
            ]
            pos = await asyncio.to_thread(self._store.get_position, inst)
            if pos.qty != 0:
                position = {"qty": str(pos.qty), "avg_price": str(pos.avg_price)}
        except Exception as e:  # noqa: BLE001
            log.warning(f"candles sqlite read failed: {e}")
            degraded.append("sqlite")

        return {
            "ts": now,
            "inst": inst,
            "bar": bar,
            "available_bars": available,
            "bars": [
                {
                    "ts": b.ts,
                    "open": str(b.open),
                    "high": str(b.high),
                    "low": str(b.low),
                    "close": str(b.close),
                    "vol": str(b.vol),
                    # 0=未收盘（最后一根还在变）、1=已收盘；前端据此标注（P1 契约）
                    "confirm": b.confirm,
                }
                for b in bars[-limit:]
            ],
            "fills": fills,
            "position": position,
            "degraded": degraded,
        }

    # ------------------------------------------------------------------
    # 参数回滚（M3-14）
    # ------------------------------------------------------------------

    async def rollback_param(
        self,
        strategy_id: str,
        change_id: int,
        *,
        reason: str = "",
        changed_by: str = "owner",
    ) -> dict[str, object]:
        """从历史记录恢复参数值，产生一条新的变更记录（不删历史）。"""
        now_ms = int(time.time() * 1000)

        def _validate(param: str, value: str) -> None:
            # PARAM_SPEC 范围可能事后收紧：恢复出的旧值若已非法会被
            # resolve_strategy 静默忽略 → sources 显示 db 实际用 toml（P2）
            strategy_cls, _ = load_strategy_class(
                strategy_id, Path(self._settings.engine.config_dir)
            )
            spec = strategy_cls.PARAM_SPEC.get(param)
            if spec is None:
                raise ParamError(f"{param} 未在 PARAM_SPEC 声明，无法安全恢复")
            coerce_value(spec, value)

        try:
            ok, message = await asyncio.to_thread(
                self._store.rollback_param,
                strategy_id,
                change_id,
                now_ms,
                reason=reason,
                changed_by=changed_by,
                validate=_validate,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"rollback_param failed: {e}")
            return {
                "ok": False,
                "error": str(e),
                # 路由用 message 返回 400 文案，异常分支原先缺这个键（P2）
                "message": str(e),
                "ts": now_ms,
            }
        return {
            "ok": ok,
            "message": message,
            "ts": now_ms,
        }


#: 死信列表里的 payload 预览上限。完整 body 可能很长，截断后加标记，
#: 让人知道滚动条到头不是数据尽头而是展示上限。
_DEAD_PAYLOAD_PREVIEW = 4000


def _dead_letter_row(row: dict) -> dict[str, object]:
    """把 SQLite 行转成前端用的死信结构（payload 截断）。"""
    payload = str(row.get("payload") or "")
    return {
        "dead_id": row["dead_id"],
        "signal_id": row.get("signal_id"),
        "stage": row["stage"],
        "reason": row["reason"],
        "payload": payload[:_DEAD_PAYLOAD_PREVIEW],
        "payload_truncated": len(payload) > _DEAD_PAYLOAD_PREVIEW,
        "created_at": row["created_at"],
        "replayed_at": row.get("replayed_at"),
        "replay_result": row.get("replay_result"),
    }


def _reconciliation_row(row: dict) -> dict[str, object]:
    """对账差异行：`resolved` 转布尔，便于前端判处置按钮。"""
    return {
        "rec_id": row["rec_id"],
        "ts": row["ts"],
        "kind": row["kind"],
        "key": row["key"],
        "local_value": row.get("local_value"),
        "remote_value": row.get("remote_value"),
        "diff": row.get("diff"),
        "severity": row["severity"],
        "resolved": bool(row.get("resolved")),
        "resolved_at": row.get("resolved_at"),
        "resolved_by": row.get("resolved_by"),
        "note": row.get("note"),
    }


def _bar_size_key(bar: str) -> int:
    """周期按毫秒升序排列；无法解析的排到最后。"""
    try:
        return DataValidator._bar_size_ms(bar)  # type: ignore[attr-defined]
    except ValueError:
        return 1 << 62


def _loads(raw: object) -> dict | None:
    """宽松解析 Redis 中的 JSON；非法内容返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        value = json.loads(raw)  # type: ignore[arg-type]
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


__all__ = ["DashboardService"]
