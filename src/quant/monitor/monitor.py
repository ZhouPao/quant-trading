"""监控主循环（F5.3/F5.4/F5.6）。

每分钟一轮：
1. 采集账户/持仓快照（OKX REST）
2. 评估告警规则（worker/engine 心跳丢失 / Redis 队列堆积 / 死信新增 /
   保证金率过低 / 余额异常波动 / 撤单率 / 连续亏损 / API 错误率 / kill switch）
3. 命中规则 → 记日志 + 邮件（SMTP 未配置则仅日志）
4. 持续型规则从命中恢复时补发一条 INFO「已恢复」
5. 写监控自身心跳；每 alive_email_hours 发一封存活邮件

监控进程自身健康：心跳键带 TTL，超过 monitor_heartbeat_timeout_seconds
未刷新即视为监控已死（下次启动时由外部检测并告警）。
"""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal, InvalidOperation

from redis.asyncio import Redis

from quant.execution.kill_switch import KillSwitch
from quant.foundation.config import MonitorSettings, SmtpSettings
from quant.foundation.logger import get_logger
from quant.monitor.alert_service import AlertEvent, AlertService
from quant.monitor.alerter import Alert, EmailAlerter
from quant.monitor.snapshot import (
    AccountSnapshot,
    PositionSnapshot,
    RiskMetrics,
    liq_distance,
)
from quant.okx.client import OKXClient
from quant.report.daily import DailyReporter
from quant.risk.pnl import round_trip_pnls
from quant.state.sqlite_store import SQLiteStore
from quant.storage.quality import QualityReporter
from quant.storage.retention import RetentionPurger

log = get_logger("monitor")

# 持续型规则：状态消失即视为恢复。事件型规则（死信新增、余额突变、连亏）
# 没有「恢复」概念，不在此列。
_STATEFUL_RULES = frozenset(
    {
        "worker_heartbeat",
        "engine_heartbeat",
        "queue_backlog",
        "margin_ratio",
        "cancel_rate",
        "api_error_rate",
        "kill_switch",
        "position_drift",
        "data_quality",
    }
)

# 这些规则不走看板冷却：每次激活都必须留痕。kill switch 只可能由人工或
# 风控触发，10 分钟内二次激活（如先 CLI 停、再网页停）不能被吞掉。
_NO_COOLDOWN_RULES = frozenset({"kill_switch"})

# 这些规则在持续命中期间只发布一次，消息变化或解除后再次命中才新增。
# kill switch 既然跳过了冷却，就必须靠这里防刷屏：一次手动停止会持续
# 几十轮，每轮都写一条会把列表占满并挤掉其它规则的告警。
_DEDUPE_RULES = frozenset({"kill_switch"})

# 心跳类规则：宿主机休眠时所有进程一起被冻结，醒来后互相看着都「陈旧」，
# 这是误报而不是真实故障，需要在挂起恢复的那一轮跳过。
_HEARTBEAT_RULES = frozenset({"worker_heartbeat", "engine_heartbeat"})

# 本轮与上一轮的间隔超过 N 个轮询周期，判定为进程被系统挂起。
# 正常抖动不会超过 3 倍间隔，取 3 兼顾灵敏度和误判。
_SUSPEND_GAP_FACTOR = 3


def _fmt_rate(value: Decimal | None) -> str:
    """撤单率格式化：None → '-'，否则百分比。"""
    if value is None:
        return "-"
    return f"{value:.2%}"


def _spot_qty(item: dict) -> Decimal:
    """现货余额数量：优先 cashBal，其次 eq；缺失或非法按 0。"""
    for field in ("cashBal", "eq"):
        raw = item.get(field)
        if raw in (None, ""):
            continue
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
    return Decimal("0")


class Monitor:
    """账户/持仓/队列/心跳监控器。"""

    def __init__(
        self,
        client: OKXClient,
        redis: Redis,
        settings: MonitorSettings,
        queue_key: str = "quant:signals",
        dead_key: str = "quant:signals:dead",
        alerter: EmailAlerter | None = None,
        store: SQLiteStore | None = None,
        daily: DailyReporter | None = None,
        retention: RetentionPurger | None = None,
        alert_service: AlertService | None = None,
        quality: QualityReporter | None = None,
        quality_cooldown_seconds: int = 3600,
    ) -> None:
        self._client = client
        self._redis = redis
        self._settings = settings
        self._queue_key = queue_key
        self._dead_key = dead_key
        self._alerter = alerter or EmailAlerter(SmtpSettings())
        self._store = store
        self._daily = daily
        self._retention = retention
        self._alert_service = alert_service
        self._quality = quality
        self._quality_cooldown = quality_cooldown_seconds
        self._kill_switch = KillSwitch(redis)
        self._last_equity: Decimal | None = None
        self._last_dead_length: int | None = None
        self._last_alive_ts = 0
        self._peak_equity: Decimal | None = None
        self._peak_loaded = False
        # 上一轮仍在命中的持续型规则（rule -> 最近一条告警），用于识别恢复。
        # 存整条告警而非消息，是为了恢复时能带上 inst_id 去 resolve
        # 对应品种的记录（多品种下否则只会关掉 inst_id IS NULL 的行）。
        self._active_rules: dict[str, Alert] = {}
        # 已提示过「成本基数未知」的品种，避免每轮重复刷日志
        self._cost_warned: set[str] = set()
        # 上一轮开始的墙钟时间，用于识别宿主机休眠/进程挂起
        self._last_cycle_ts: int | None = None
        self._running = False
        # 上一轮结束时间戳（无 TTL 键）：重启时据此判断上次停机多久（M9-4）
        self._last_beat_ms: int | None = None
        self.metrics: RiskMetrics = RiskMetrics.empty(0)
        self.positions: list[PositionSnapshot] = []

    async def run(self) -> None:
        """主循环，直到 stop()。"""
        self._running = True
        if self._settings.disabled_rules:
            log.info(
                f"规则级启停：{'/'.join(self._settings.disabled_rules)} 已停用（M9-2）"
            )
        # 上一次心跳中断检测（M9-4）：只在启动时判一次，否则每轮都会重报
        gap_alerts = await self.check_previous_heartbeat(int(time.time() * 1000))
        if gap_alerts:
            for alert in gap_alerts:
                log.warning(f"[{alert.rule}] {alert.message}")
            await self._publish_alerts(gap_alerts)
        while self._running:
            try:
                await self.run_once()
            except Exception as e:  # noqa: BLE001
                log.exception(f"monitor cycle failed: {e}")
            await self._sleep_or_stop(self._settings.interval_seconds)
        log.info("monitor stopped")

    def stop(self) -> None:
        self._running = False

    async def run_once(self) -> list[Alert]:
        """采集 + 评估一轮，返回命中的告警。"""
        ts = int(time.time() * 1000)
        suspended_ms = self._take_suspend_gap(ts)
        balance_raw, snapshot = await self._collect(ts)
        self.positions = await self._collect_positions(balance_raw)
        self.metrics = self._compute_metrics(snapshot, self.positions, ts)
        await asyncio.to_thread(self._persist_snapshot, snapshot, ts)
        alerts = await self._evaluate(snapshot, ts, suspended=suspended_ms > 0)
        if suspended_ms > 0:
            # 挂起期间 worker/engine 和 monitor 一起被冻结，之前那条心跳
            # ERROR 是误报，直接丢弃状态而不补发「已恢复」
            log.warning(
                f"检测到系统挂起 {suspended_ms / 1000:.0f}s："
                "本轮跳心跳类告警"
            )
            for rule in _HEARTBEAT_RULES:
                self._active_rules.pop(rule, None)
        alerts = self._with_recovery(alerts, ts)

        log.info(
            f"equity={snapshot.total_equity} "
            f"drawdown={self.metrics.max_drawdown:.2%} "
            f"cancel_rate={_fmt_rate(self.metrics.cancel_rate)} "
            f"min_liq_distance={_fmt_rate(self.metrics.min_liq_distance)}"
        )

        # 记日志（无论 SMTP 是否可用，本地日志是主渠道）
        for alert in alerts:
            if alert.level == "ERROR":
                log.error(f"[{alert.rule}] {alert.message}")
            elif alert.level == "INFO":
                log.info(f"[{alert.rule}] {alert.message}")
            else:
                log.warning(f"[{alert.rule}] {alert.message}")

        # 邮件：有 AlertService 时逐条发送（静音可精确到单条）；
        # 没有 service 的旧路径（未接库的调用方/单测）保留一轮一封的聚合。
        if self._alert_service is None and alerts and self._alerter.enabled():
            body = "\n".join(
                f"[{a.level}] [{a.rule}] {a.message}" for a in alerts
            )
            self._alerter.send(
                f"quant-trading 监控告警 x{len(alerts)}", body
            )

        # 存活邮件（每 alive_email_hours 一封）
        await self._maybe_send_alive(ts)

        # 发布给只读看板（失败不影响监控主循环）
        await self._publish_snapshot(snapshot, ts)
        await self._publish_alerts(alerts)

        # 每日偏离日报（到点才生成，内部自行去重）
        await self._run_daily_report(ts)

        # 每日数据保留清理（同上：到点才跑，Redis 去重）
        await self._run_retention(ts)

        # 写监控心跳（TTL 保证超时即视为死亡）
        await self._redis.set(
            self._settings.monitor_heartbeat_key,
            ts,
            ex=self._settings.monitor_heartbeat_timeout_seconds,
        )
        await self._record_last_beat(ts)
        return alerts

    async def _record_last_beat(self, ts: int) -> None:
        """记录无 TTL 的最后心跳时间，供下次重启判断停机时长。

        写失败只记日志：这是辅助信息，不能影响监控主循环。
        """
        key = self._settings.monitor_last_beat_key
        if not key:
            return
        self._last_beat_ms = ts
        try:
            await self._redis.set(key, ts)
        except Exception as e:  # noqa: BLE001
            log.warning(f"write last heartbeat failed: {e}")

    async def check_previous_heartbeat(self, now_ms: int) -> list[Alert]:
        """启动时核对上次心跳，停机超过阈值即补一条告警（M9-4）。

        带 TTL 的心跳键在长时间停机后已过期，读不到任何线索，所以另存
        一个无 TTL 的时间戳。首次部署（键不存在）不告警——没有可比对的基线。
        """
        key = self._settings.monitor_last_beat_key
        if not key:
            return []
        try:
            raw = await self._redis.get(key)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read last heartbeat failed: {e}")
            return []
        if raw is None:
            return []
        try:
            last = int(raw)
        except (TypeError, ValueError):
            log.warning(f"last heartbeat key has non-numeric value: {raw!r}")
            return []
        gap_ms = now_ms - last
        timeout_ms = self._settings.monitor_heartbeat_timeout_seconds * 1000
        if gap_ms <= timeout_ms:
            return []
        return [
            Alert(
                rule="monitor_restart",
                level="WARNING",
                message=(
                    f"上次心跳中断约 {gap_ms / 60_000:.0f} 分钟，"
                    "期间监控未运行（持仓/告警可能漏采）"
                ),
                ts=now_ms,
            )
        ]

    async def _publish_snapshot(self, snap: AccountSnapshot, ts: int) -> None:
        """把本轮快照写到 Redis，供只读看板消费。

        TTL 取 3 个采集周期，monitor 停机后键自然过期，看板据此显示离线。
        """
        payload = {
            "ts": ts,
            "account": {
                "total_equity": str(snap.total_equity),
                "avail_usdt": str(snap.avail_usdt),
                "frozen_usdt": str(snap.frozen_usdt),
                "unrealized_pnl": str(snap.unrealized_pnl),
                "margin_ratio": (
                    str(snap.margin_ratio)
                    if snap.margin_ratio is not None
                    else None
                ),
            },
            "metrics": {
                "max_drawdown": str(self.metrics.max_drawdown),
                "cancel_rate": (
                    str(self.metrics.cancel_rate)
                    if self.metrics.cancel_rate is not None
                    else None
                ),
                "min_liq_distance": (
                    str(self.metrics.min_liq_distance)
                    if self.metrics.min_liq_distance is not None
                    else None
                ),
            },
            "positions": [
                {
                    "inst_id": p.inst_id,
                    "qty": str(p.qty),
                    "avg_price": str(p.avg_price),
                    "unrealized_pnl": str(p.unrealized_pnl),
                    "leverage": str(p.leverage),
                    "liq_price": str(p.liq_price) if p.liq_price else None,
                    "mark_price": str(p.mark_price),
                    "liq_distance": (
                        str(d) if (d := liq_distance(p)) is not None else None
                    ),
                }
                for p in self.positions
            ],
        }
        ttl = max(self._settings.interval_seconds * 3, 30)
        try:
            await self._redis.set(
                self._settings.dashboard_snapshot_key,
                json.dumps(payload, ensure_ascii=False),
                ex=ttl,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"publish dashboard snapshot failed: {e}")

    def _cooldown_window(self, rule: str) -> int:
        """各规则的冷却窗口（秒）：数据缺口不像心跳那样分钟级自愈。"""
        if rule == "data_quality":
            return self._quality_cooldown
        return self._settings.dashboard_alert_cooldown_seconds

    async def _in_cooldown(self, rule: str, inst_id: str | None, ts: int) -> bool:
        """同一规则（+品种）在冷却窗口内只产生一次告警。

        状态放 Redis 而非进程内存：monitor 重启后冷却不会清零，避免重启
        立刻重发一轮。键带 `inst_id` 维度——多品种下 BTC 的告警不该把
        ETH 的同类告警吞掉。
        """
        if rule in _NO_COOLDOWN_RULES:
            return False
        window = self._cooldown_window(rule)
        if window <= 0:
            return False
        key = f"quant:dashboard:alert-cooldown:{rule}:{inst_id or '-'}"
        try:
            if await self._redis.exists(key):
                return True
            await self._redis.set(key, ts, ex=window)
        except Exception as e:  # noqa: BLE001
            # Redis 不可用时不能反过来吞掉告警，宁可多发
            log.warning(f"alert cooldown check failed: {e}")
            return False
        return False

    async def _publish_alerts(self, alerts: list[Alert]) -> None:
        """先冷却过滤，再逐条发布（落库 + Redis 缓存 + 通知）。

        恢复通知（INFO）与 kill_switch 不受冷却约束：前者是状态跃迁，
        后者每次激活都必须留痕。
        """
        if not alerts:
            return

        for alert in alerts:
            if alert.level != "INFO" and alert.rule not in _NO_COOLDOWN_RULES:
                if await self._in_cooldown(
                    alert.rule, alert.cooldown_key or alert.inst_id, alert.ts
                ):
                    continue

            if self._alert_service is None:
                await self._push_alert_cache(alert)
                continue

            await self._alert_service.publish(
                AlertEvent(
                    rule=alert.rule,
                    level=alert.level,
                    message=alert.message,
                    ts=alert.ts,
                    inst_id=alert.inst_id,
                )
            )
            # 恢复通知同时把该规则下未关闭的记录置为 resolved
            if alert.level == "INFO":
                try:
                    await self._alert_service.resolve(
                        alert.rule, inst_id=alert.inst_id
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(f"resolve alerts failed rule={alert.rule}: {e}")

    async def _push_alert_cache(self, alert: Alert) -> None:
        """未接 AlertService 时退回只写 Redis 列表的旧行为。"""
        try:
            await self._redis.lpush(
                self._settings.dashboard_alerts_key,
                json.dumps(
                    {
                        "rule": alert.rule,
                        "level": alert.level,
                        "message": alert.message,
                        "ts": alert.ts,
                        "inst_id": alert.inst_id,
                    },
                    ensure_ascii=False,
                ),
            )
            await self._redis.ltrim(
                self._settings.dashboard_alerts_key,
                0,
                self._settings.dashboard_alert_keep - 1,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"publish dashboard alerts failed: {e}")

    async def _run_daily_report(self, ts: int) -> None:
        """到点生成模拟盘日报；异常只记日志，不影响监控主循环。"""
        if self._daily is None:
            return
        try:
            await self._daily.maybe_run(ts)
        except Exception as e:  # noqa: BLE001
            log.warning(f"daily report failed: {e}")

    async def _run_retention(self, ts: int) -> None:
        """到点清理历史数据；异常只记日志，不影响监控主循环。"""
        if self._retention is None:
            return
        try:
            await self._retention.maybe_run(ts)
        except Exception as e:  # noqa: BLE001
            log.warning(f"retention purge failed: {e}")

    def _compute_metrics(
        self, snap: AccountSnapshot, positions: list[PositionSnapshot], ts: int
    ) -> RiskMetrics:
        """计算风险指标（F5.3）：实时最大回撤 + 撤单率 + 最小强平价距离。"""
        drawdown = self._update_drawdown(snap.total_equity)
        cancel_rate = self._compute_cancel_rate()
        min_liq = self._min_liq_distance(positions)
        return RiskMetrics(
            ts=ts,
            max_drawdown=drawdown,
            cancel_rate=cancel_rate,
            min_liq_distance=min_liq,
        )

    def _min_liq_distance(
        self, positions: list[PositionSnapshot]
    ) -> Decimal | None:
        """所有持仓中最小的强平价距离（最危险）；无强平价返回 None。"""
        distances = [
            d for p in positions if (d := liq_distance(p)) is not None
        ]
        if not distances:
            return None
        return min(distances)

    def _update_drawdown(self, equity: Decimal) -> Decimal:
        """维护历史峰值并返回当前回撤（0~1）。

        峰值优先从 SQLite 历史快照恢复：monitor 重启后回撤不会归零。
        """
        if equity <= 0:
            return Decimal("0")
        self._ensure_peak_loaded()
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity
        return (self._peak_equity - equity) / self._peak_equity

    def _ensure_peak_loaded(self) -> None:
        """首次计算回撤时从库读取历史峰值（仅一次）。"""
        if self._peak_loaded:
            return
        self._peak_loaded = True
        if self._store is None:
            return
        try:
            peak = self._store.get_peak_equity()
        except Exception as e:  # noqa: BLE001
            log.warning(f"load peak equity failed: {e}")
            return
        if peak is not None:
            self._peak_equity = peak

    def _persist_snapshot(self, snap: AccountSnapshot, ts: int) -> None:
        """把本轮权益写入 SQLite，供回撤恢复与看板曲线使用。"""
        if self._store is None:
            return
        try:
            self._store.save_equity_snapshot(
                ts=ts,
                total_equity=snap.total_equity,
                avail_usdt=snap.avail_usdt,
                frozen_usdt=snap.frozen_usdt,
                unrealized_pnl=snap.unrealized_pnl,
                margin_ratio=snap.margin_ratio,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"persist equity snapshot failed: {e}")

    def _compute_cancel_rate(self) -> Decimal | None:
        """撤单率 = 已撤销 / (已成交 + 已撤销)；无 store 或分母 0 返回 None。"""
        if self._store is None:
            return None
        try:
            counts = self._store.get_order_status_counts()
        except Exception as e:  # noqa: BLE001
            log.warning(f"compute cancel rate failed: {e}")
            return None
        filled = counts.get("filled", 0)
        canceled = counts.get("canceled", 0)
        denom = filled + canceled
        if denom == 0:
            return None
        return Decimal(canceled) / Decimal(denom)

    async def _collect(self, ts: int) -> tuple[dict, AccountSnapshot]:
        """采集账户快照（同步 client 用 to_thread 规避阻塞）。

        同时返回原始 balance 响应，供现货持仓复用同一份数据（少一次请求）。
        """
        raw = await asyncio.to_thread(self._client.get_balance)
        return raw, AccountSnapshot.from_okx(raw, ts)

    async def _collect_positions(self, balance: dict) -> list[PositionSnapshot]:
        """采集持仓：合约走 /account/positions，现货走余额明细。

        单个类型失败只记告警，不拖垮整轮（某些账户/模拟盘可能不支持某类型）。
        """
        positions: list[PositionSnapshot] = []
        for inst_type in self._settings.position_inst_types:
            try:
                raw_list = await asyncio.to_thread(
                    self._client.get_positions, inst_type
                )
                for r in raw_list:
                    snap = await self._contract_position(r)
                    if snap is not None:
                        positions.append(snap)
            except Exception as e:  # noqa: BLE001
                log.warning(f"collect positions {inst_type} failed: {e}")
        positions.extend(await self._spot_positions(balance))
        return positions

    async def _contract_position(
        self, raw: dict
    ) -> PositionSnapshot | None:
        """合约持仓：`pos` 是张数，按 `ctVal` 换算成基础币再入快照。

        查不到面值时跳过该行并告警——张数直接当基础币会让数量相差 ctVal 倍，
        比缺一行更容易误导。
        """
        inst_id = str(raw.get("instId") or "")
        ct_val = Decimal("1")
        if inst_id.count("-") >= 2:
            try:
                instrument = await asyncio.to_thread(
                    self._client.get_instrument, inst_id
                )
            except Exception as e:  # noqa: BLE001
                log.warning(f"lookup instrument {inst_id} failed: {e}")
                return None
            if instrument is None or instrument.ct_val <= 0:
                log.warning(f"instrument {inst_id} has no ctVal, skip position")
                return None
            ct_val = instrument.ct_val
        return PositionSnapshot.from_okx(raw, ct_val)

    async def _spot_positions(self, balance: dict) -> list[PositionSnapshot]:
        """从余额明细采集现货持仓（补齐 F5.2 的现货盲区）。

        OKX /account/positions 不返回 SPOT，现货持仓只体现在
        /account/balance 的 details 里：
        - 标记价取公开 ticker（`/market/ticker` 的 last）
        - 成本基数优先用交易所给的 `openAvgPx`（当前持仓均价），
          交易所没给时回退本地 SQLite 记账
        - 没有对应 {ccy}-{quote} 交易对的余额（如理财份额）跳过
        """
        quote = self._settings.spot_quote_ccy.strip().upper()
        if not quote:
            return []

        positions: list[PositionSnapshot] = []
        for item in balance.get("details") or []:
            ccy = str(item.get("ccy") or "").upper()
            if not ccy or ccy == quote:
                continue
            qty = _spot_qty(item)
            if qty <= 0:
                continue
            inst_id = f"{ccy}-{quote}"
            mark = await asyncio.to_thread(self._spot_price, inst_id)
            if mark is None:
                continue
            avg = await asyncio.to_thread(
                self._spot_avg_price, item, inst_id, qty
            )
            positions.append(
                PositionSnapshot.from_spot(inst_id, qty, mark, avg)
            )
        return positions

    def _spot_avg_price(
        self, item: dict, inst_id: str, qty: Decimal
    ) -> Decimal:
        """现货成本基数：优先交易所的持仓均价 `openAvgPx`。

        `openAvgPx` 是交易所核算的「当前持仓均价」，乘全量余额即得账户口径的
        未实现盈亏；`accAvgPx`（累计均价）含已卖出部分的历史成本，不是当前
        持仓成本，故不使用。
        交易所没给该字段（如空投、理财转入）时回退本地记账，且要求本地数量
        能覆盖全部余额，否则返回 0 表示未知。
        """
        try:
            avg = Decimal(str(item.get("openAvgPx") or ""))
        except (InvalidOperation, ValueError):
            avg = Decimal("0")
        if avg > 0:
            return avg
        return self._local_avg_price(inst_id, qty)

    def _spot_price(self, inst_id: str) -> Decimal | None:
        """现货标记价：公开 ticker 的最新成交价；查不到返回 None。"""
        try:
            rows = self._client.get(
                "/api/v5/market/ticker", {"instId": inst_id}
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"spot ticker failed inst={inst_id}: {e}")
            return None
        if not rows:
            return None
        try:
            price = Decimal(str(rows[0].get("last") or ""))
        except (InvalidOperation, ValueError):
            return None
        return price if price > 0 else None

    def _local_avg_price(self, inst_id: str, qty: Decimal) -> Decimal:
        """本地记账的成本基数，仅在能覆盖交易所余额 `qty` 时有效。

        交易余额大于本地数量时，多出的是外部转入、没有买入记录，成本基数
        无从得知。此时返回 0（调用方据此留空盈亏），不用「本地均价 × 交易所
        全量余额」这种口径混算 —— 那会把未记账的币按策略成本价虚增盈亏。
        """
        if self._store is None:
            return Decimal("0")
        try:
            position = self._store.get_position(inst_id)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read local position failed inst={inst_id}: {e}")
            return Decimal("0")
        if position.qty <= 0:
            return Decimal("0")
        if position.qty < qty:
            if inst_id not in self._cost_warned:
                self._cost_warned.add(inst_id)
                log.warning(
                    f"spot cost basis unknown inst={inst_id} "
                    f"exchange_qty={qty} local_qty={position.qty}"
                )
            return Decimal("0")
        self._cost_warned.discard(inst_id)
        return position.avg_price

    async def _evaluate(
        self, snap: AccountSnapshot, ts: int, suspended: bool = False
    ) -> list[Alert]:
        """评估告警规则；`suspended` 为真时跳过受整机挂起影响的心跳类规则。"""
        alerts: list[Alert] = []

        if not suspended:
            alerts.extend(await self._check_worker_heartbeat(ts))
            alerts.extend(await self._check_engine_heartbeat(ts))
        alerts.extend(await self._check_queue(ts))
        alerts.extend(await self._check_dead_letter(ts))
        alerts.extend(self._check_margin_ratio(snap, ts))
        alerts.extend(self._check_balance_change(snap, ts))
        alerts.extend(self._check_position_drift(ts))
        alerts.extend(self._check_cancel_rate(ts))
        alerts.extend(self._check_consecutive_losses(ts))
        alerts.extend(self._check_api_error_rate(ts))
        alerts.extend(await self._check_kill_switch(ts))
        alerts.extend(await self._check_quality(ts))
        if self._settings.disabled_rules:
            # 规则级启停（M9-2）：整条规则不产生告警，不参与恢复跃迁
            disabled = set(self._settings.disabled_rules)
            alerts = [a for a in alerts if a.rule not in disabled]
        return alerts

    def _with_recovery(self, alerts: list[Alert], ts: int) -> list[Alert]:
        """对持续型规则做「命中 → 恢复」的跃迁检测。

        只比较本轮命中的规则集合与上一轮，恢复时补一条 INFO，
        让邮件/看板能看出故障已自愈，而不是一直停在上一条告警。
        """
        current = {
            a.rule: a for a in alerts if a.rule in _STATEFUL_RULES
        }
        # 持续命中且消息没变 → 这条已经在上一轮报过了，不再重复发布。
        # 解除后 `_active_rules` 会被清空，所以再次激活仍会新增一条。
        fresh = [
            a
            for a in alerts
            if not (
                a.rule in _DEDUPE_RULES
                and (
                    prev := self._active_rules.get(a.rule)
                ) is not None
                and prev.message == a.message
            )
        ]
        recovered = [
            Alert(
                rule=rule,
                level="INFO",
                message=f"已恢复（此前：{alert.message}）",
                ts=ts,
                inst_id=alert.inst_id,
            )
            for rule, alert in self._active_rules.items()
            if rule not in current
        ]
        self._active_rules = current
        return fresh + recovered

    def _take_suspend_gap(self, ts: int) -> int:
        """记录本轮墙钟时间，返回判定为系统挂起的时间（毫秒，0=正常）。

        宿主机休眠 / 进程被 App Nap 冻结时，monitor 自身的循环间隔会远大于
        `interval_seconds`。以这个偏差作为「整机被冻结」的证据：此时 worker
        和 engine 的心跳必然陈旧，属于误报。
        """
        last = self._last_cycle_ts
        self._last_cycle_ts = ts
        interval_ms = self._settings.interval_seconds * 1000
        if last is None or interval_ms <= 0:
            return 0
        elapsed = ts - last
        if elapsed <= interval_ms * _SUSPEND_GAP_FACTOR:
            return 0
        return elapsed - interval_ms

    async def _check_engine_heartbeat(self, ts: int) -> list[Alert]:
        """engine 状态键超过阈值未更新即告警（engine 是本地信号源）。"""
        key = self._settings.engine_status_key
        if not key:
            return []
        raw = await self._redis.get(key)
        if raw is None:
            return [
                Alert(
                    rule="engine_heartbeat",
                    level="ERROR",
                    message="engine 从未上报状态（engine 可能未启动）",
                    ts=ts,
                )
            ]
        try:
            last = int(json.loads(raw)["ts"])
        except (ValueError, TypeError, KeyError):
            return [
                Alert(
                    rule="engine_heartbeat",
                    level="ERROR",
                    message="engine 状态无法解析",
                    ts=ts,
                )
            ]
        timeout_ms = self._settings.engine_timeout_seconds * 1000
        if ts - last > timeout_ms:
            age_s = (ts - last) / 1000
            return [
                Alert(
                    rule="engine_heartbeat",
                    level="ERROR",
                    message=f"engine 状态陈旧 {age_s:.0f}s",
                    ts=ts,
                )
            ]
        return []

    async def _check_worker_heartbeat(self, ts: int) -> list[Alert]:
        raw = await self._redis.get(self._settings.worker_heartbeat_key)
        if raw is None:
            return [
                Alert(
                    rule="worker_heartbeat",
                    level="ERROR",
                    message="worker 心跳丢失（键不存在）",
                    ts=ts,
                )
            ]
        last = int(raw)
        timeout_ms = self._settings.worker_heartbeat_timeout_seconds * 1000
        if ts - last > timeout_ms:
            age_s = (ts - last) / 1000
            return [
                Alert(
                    rule="worker_heartbeat",
                    level="ERROR",
                    message=f"worker 心跳丢失 {age_s:.0f}s",
                    ts=ts,
                )
            ]
        return []

    async def _check_queue(self, ts: int) -> list[Alert]:
        pending = await self._redis.llen(self._queue_key)
        threshold = self._settings.queue_backlog_threshold
        if pending > threshold:
            return [
                Alert(
                    rule="queue_backlog",
                    level="WARNING",
                    message=f"pending 队列堆积 {pending} > {threshold}",
                    ts=ts,
                )
            ]
        return []

    async def _check_dead_letter(self, ts: int) -> list[Alert]:
        dead = await self._redis.llen(self._dead_key)
        if self._last_dead_length is None:
            self._last_dead_length = dead
            return []
        if dead > self._last_dead_length:
            self._last_dead_length = dead
            return [
                Alert(
                    rule="dead_letter",
                    level="ERROR",
                    message=f"死信队列新增条目（当前 {dead} 条）",
                    ts=ts,
                )
            ]
        self._last_dead_length = dead
        return []

    def _check_margin_ratio(
        self, snap: AccountSnapshot, ts: int
    ) -> list[Alert]:
        if snap.margin_ratio is None:
            return []
        if snap.margin_ratio < self._settings.margin_ratio_threshold:
            return [
                Alert(
                    rule="margin_ratio",
                    level="ERROR",
                    message=(
                        f"保证金率 {snap.margin_ratio} 低于阈值 "
                        f"{self._settings.margin_ratio_threshold}"
                    ),
                    ts=ts,
                )
            ]
        return []

    def _check_balance_change(
        self, snap: AccountSnapshot, ts: int
    ) -> list[Alert]:
        alerts: list[Alert] = []
        if self._last_equity is None:
            self._last_equity = snap.total_equity
            return alerts

        prev = self._last_equity
        self._last_equity = snap.total_equity
        if prev <= 0:
            return alerts
        change = abs(snap.total_equity - prev) / prev
        if change > self._settings.balance_change_threshold:
            alerts.append(
                Alert(
                    rule="balance_change",
                    level="WARNING",
                    message=(
                        f"账户权益异常波动 {change:.2%}："
                        f"{prev} -> {snap.total_equity}"
                    ),
                    ts=ts,
                )
            )
        return alerts

    def _check_position_drift(self, ts: int) -> list[Alert]:
        """策略记账数量超过交易所余额时告警（P1 对账盲区）。

        快照与本地记账都是**基础币**口径（合约的张数在
        `_contract_position` 里已按 ctVal 折算），故现货与合约一并比对。
        本地少于交易所是正常的（外部转入、空投），只有反过来——本地多出
        交易所没有的仓位——才会让策略卖出因可用量不足被拒，属于必须人工
        介入的记账漂移。
        """
        if self._store is None:
            return []
        try:
            local = self._store.get_all_positions()
        except Exception as e:  # noqa: BLE001
            log.warning(f"read local positions failed: {e}")
            return []

        held = {p.inst_id: p.qty for p in self.positions}
        tolerance = Decimal(str(self._settings.position_drift_tolerance))
        drifted: list[str] = []
        for pos in local:
            if pos.qty <= 0:
                continue
            exchange = held.get(pos.inst_id, Decimal("0"))
            excess = pos.qty - exchange
            if excess <= 0:
                continue
            if excess / pos.qty <= tolerance:
                continue
            drifted.append(
                f"{pos.inst_id} 本地 {pos.qty} > 交易所 {exchange}"
            )
        if not drifted:
            return []
        return [
            Alert(
                rule="position_drift",
                level="WARNING",
                message="策略记账数量超过交易所余额：" + "；".join(drifted),
                ts=ts,
            )
        ]

    def _check_cancel_rate(self, ts: int) -> list[Alert]:
        """撤单率超过阈值时告警。"""
        rate = self.metrics.cancel_rate
        if rate is None:
            return []
        threshold = Decimal(str(self._settings.cancel_rate_threshold))
        if rate > threshold:
            return [
                Alert(
                    rule="cancel_rate",
                    level="WARNING",
                    message=(
                        f"撤单率 {rate:.2%} 超过阈值 {threshold:.2%}"
                    ),
                    ts=ts,
                )
            ]
        return []

    def _check_consecutive_losses(self, ts: int) -> list[Alert]:
        """连续亏损回合数达到阈值时告警。"""
        if self._store is None:
            return []
        limit = self._settings.consecutive_loss_threshold
        try:
            fills = self._store.get_recent_trades(limit * 4)
        except Exception as e:  # noqa: BLE001
            log.warning(f"check consecutive losses failed: {e}")
            return []
        pnls = round_trip_pnls(fills)
        count = 0
        for pnl in reversed(pnls):
            if pnl < 0:
                count += 1
            else:
                break
        if count >= limit:
            return [
                Alert(
                    rule="consecutive_losses",
                    level="ERROR",
                    message=f"连续亏损 {count} 个回合（阈值 {limit}）",
                    ts=ts,
                )
            ]
        return []

    def _check_api_error_rate(self, ts: int) -> list[Alert]:
        """API 请求错误率超过阈值时告警。"""
        rate = self._client.get_error_rate()
        if rate is None:
            return []
        threshold = Decimal(str(self._settings.api_error_rate_threshold))
        if rate > threshold:
            return [
                Alert(
                    rule="api_error_rate",
                    level="ERROR",
                    message=(
                        f"API 错误率 {rate:.2%} 超过阈值 {threshold:.2%}"
                    ),
                    ts=ts,
                )
            ]
        return []

    async def _check_kill_switch(self, ts: int) -> list[Alert]:
        """紧急停止激活时告警。"""
        if await self._kill_switch.is_active():
            reason = (await self._kill_switch.reason()) or "(none)"
            return [
                Alert(
                    rule="kill_switch",
                    level="ERROR",
                    message=f"紧急停止已激活 reason={reason}",
                    ts=ts,
                )
            ]
        return []

    async def _check_quality(self, ts: int) -> list[Alert]:
        """数据质量报告里有未修复缺口时告警（MS4-C §9.3）。

        读取 `data/quality/**/latest.json`，缺口语义由 candles 同步侧维护；
        这里只负责「要不要发」——冷却按 (inst, bar) 各算，见 `_in_cooldown`。
        """
        if self._quality is None:
            return []
        try:
            reports = await asyncio.to_thread(self._quality.summaries)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read quality reports failed: {e}")
            return []

        alerts: list[Alert] = []
        for report in reports:
            summary = report.get("summary") or {}
            open_n = int(summary.get("gaps_open") or 0)
            if open_n <= 0:
                continue
            inst_id = str(report.get("inst_id") or "")
            bar_size = str(report.get("bar_size") or "")
            expected = summary.get("expected")
            present = summary.get("present")
            missing = (
                int(expected) - int(present)
                if expected is not None and present is not None
                else None
            )
            detail = f"，缺失约 {missing} 根" if missing else ""
            alerts.append(
                Alert(
                    rule="data_quality",
                    level="WARNING",
                    message=(
                        f"{inst_id} {bar_size} 有 {open_n} 处未修复缺口{detail}，"
                        "回测与日报结果可能失真"
                    ),
                    ts=ts,
                    inst_id=inst_id,
                    cooldown_key=f"{inst_id}:{bar_size}",
                )
            )
        return alerts

    async def _maybe_send_alive(self, ts: int) -> None:
        """每 alive_email_hours 发一封存活邮件。

        上次发送时间存 Redis：monitor 重启不会立刻补发，避免崩溃循环刷邮箱。
        """
        if not self._alerter.enabled():
            return
        interval_ms = self._settings.alive_email_hours * 3_600_000
        last = self._last_alive_ts
        try:
            raw = await self._redis.get(self._settings.alive_email_key)
            if raw is not None:
                last = max(last, int(raw))
        except Exception as e:  # noqa: BLE001
            log.warning(f"read alive email marker failed: {e}")

        if ts - last < interval_ms:
            return
        self._last_alive_ts = ts
        try:
            await self._redis.set(self._settings.alive_email_key, ts)
        except Exception as e:  # noqa: BLE001
            log.warning(f"write alive email marker failed: {e}")
        self._alerter.send("quant-trading 监控存活", "monitor alive")

    async def _sleep_or_stop(self, seconds: int) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise


__all__ = ["Monitor"]
