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

from quant.execution.kill_switch import KillSwitch
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger
from quant.foundation.schedule import day_start_ms_utc8
from quant.risk.config import RiskConfig
from quant.risk.pnl import round_trip_pnls
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage
from quant.storage.validator import DataValidator

log = get_logger("dashboard.service")


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
    try:
        last = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return offline
    age = _age_seconds(last, now_ms)
    return {
        "online": age is not None and age <= timeout_seconds,
        "last_ts": last,
        "age_seconds": age,
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

    @property
    def settings(self) -> Settings:
        """供同进程的其他只读模块（如回测面板）复用同一份配置。"""
        return self._settings

    # ------------------------------------------------------------------
    # 概览
    # ------------------------------------------------------------------

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
        kill: dict[str, object] = {"active": False, "reason": None, "known": False}

        try:
            raw = await self._redis.get(
                self._settings.monitor.dashboard_snapshot_key
            )
            snapshot = _loads(raw)
            worker = _heartbeat(
                await self._redis.get(
                    self._settings.monitor.worker_heartbeat_key
                ),
                self._settings.monitor.worker_heartbeat_timeout_seconds,
                now,
            )
            monitor = _heartbeat(
                await self._redis.get(
                    self._settings.monitor.monitor_heartbeat_key
                ),
                self._settings.monitor.monitor_heartbeat_timeout_seconds,
                now,
            )
            queue = {
                "pending": int(await self._redis.llen(self._settings.redis.queue_key)),
                "inflight": int(
                    await self._redis.llen(self._settings.redis.inflight_key)
                ),
                "dead": int(await self._redis.llen(self._settings.redis.dead_key)),
            }
            kill = {
                "active": await self._kill_switch.is_active(),
                "reason": await self._kill_switch.reason(),
                "auth_required": bool(self._settings.dashboard.token),
                "known": True,
            }
        except Exception as e:  # noqa: BLE001
            degraded.append("redis")
            log.warning(f"overview redis read failed: {e}")

        order_counts: dict[str, int] = {}
        try:
            order_counts = await asyncio.to_thread(
                self._store.get_order_status_counts
            )
        except Exception as e:  # noqa: BLE001
            degraded.append("sqlite")
            log.warning(f"overview sqlite read failed: {e}")

        snapshot_ts = None
        if snapshot is not None:
            try:
                snapshot_ts = int(snapshot.get("ts") or 0) or None
            except (TypeError, ValueError):
                snapshot_ts = None
        if snapshot is None and "redis" not in degraded:
            # 键缺失或已过 TTL（monitor 每轮刷新，停机后自然过期）：账户列会
            # 静默变成 —，必须和「读取失败」一样显式降级，否则界面看起来一切正常。
            degraded.append("snapshot")

        return {
            "ts": now,
            "snapshot_ts": snapshot_ts,
            "snapshot_age_seconds": _age_seconds(snapshot_ts, now),
            "account": snapshot.get("account") if snapshot else None,
            "metrics": snapshot.get("metrics") if snapshot else None,
            "positions_count": await self._position_count(snapshot),
            "kill_switch": kill,
            "worker": worker,
            "monitor": monitor,
            "queue": queue,
            "orders": order_counts,
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
            ),
            _limit_rule(
                "consecutive_loss",
                "连续亏损",
                Decimal(streak),
                Decimal(consecutive_loss_limit),
                f"最近连续亏损 {streak} 笔",
            ),
            _limit_rule(
                "order_notional",
                "单笔上限",
                None,
                max_order_qty,
                f"单笔最大名义 {max_order_qty} USDT",
            ),
            _limit_rule(
                "inst_position",
                "单品种仓位",
                inst_used,
                equity * max_position_pct,
                f"{inst_name or '暂无持仓'} 名义 {inst_used}",
            ),
            _limit_rule(
                "total_position",
                "总仓位",
                total_used,
                equity * max_total_pct,
                f"全部持仓名义 {total_used}",
            ),
        ]

        return {
            "ts": now,
            "known": True,
            "strategy": payload.get("strategy"),
            "paper_mode": bool(payload.get("paper_mode")),
            "equity": str(equity),
            "equity_source": equity_source,
            "day_start_ts": day_start,
            "rules": rules,
            "degraded": degraded,
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

    async def set_kill_switch(
        self, active: bool, reason: str = ""
    ) -> dict[str, object]:
        """切换紧急停止标志（唯一的写操作）。

        只写 Redis 标志，不撤挂单、不停进程：worker 消费信号前、
        engine 每轮评估前都会重新读取该键，所以改完立即生效。
        撤单仍需 `quant kill-switch --on`（会调交易所批量撤单接口）。
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

    async def alerts(self, limit: int = 50) -> dict[str, object]:
        now = int(time.time() * 1000)
        key = self._settings.monitor.dashboard_alerts_key
        try:
            raw_items = await self._redis.lrange(key, 0, limit - 1)
        except Exception as e:  # noqa: BLE001
            log.warning(f"alerts redis read failed: {e}")
            return {"ts": now, "alerts": [], "degraded": ["redis"]}

        items: list[dict[str, object]] = []
        for raw in raw_items:
            parsed = _loads(raw)
            if parsed is None:
                continue
            items.append(parsed)
        return {"ts": now, "alerts": items, "degraded": []}

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
                }
                for b in bars[-limit:]
            ],
            "fills": fills,
            "position": position,
            "degraded": degraded,
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
