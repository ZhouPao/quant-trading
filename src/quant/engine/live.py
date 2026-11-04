"""本地策略实时信号引擎（不依赖 TradingView）。

每根 K 线收盘后，用本地 parquet 的历史 K 线回放策略，把产生的信号写入
Redis 队列，交给 worker 在 OKX 模拟盘执行。

支持多绑定（MS4-C §4.2）：每轮从 Redis 缓存读出启停矩阵，对每个启用的
「策略 × 品种」组合各跑一次。每个组合持有**独立的策略实例与 Context**，
K 线窗口按各自的 bar / window_size 裁剪；风控参数仍按 strategy_id 解析。

与回测的差别：
- 持仓与现金取真实（模拟盘）状态，而不是从初始资金推导
- 水位之后漏掉的 bar 会逐根补评估（受 `max_catchup_bars` 限制），
  避免休眠/停机期间的交叉信号永久丢失
- 每根 bar 只处理一次，水位存 Redis，重启不重复下单
- kill switch 生效或有未完成挂单时跳过，避免重复/越权下单

策略的选择与参数复用 `config/strategies/{id}.toml`，与回测、日报完全一致。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from redis.asyncio import Redis

from quant.backtest.context import Context
from quant.backtest.order import Fill
from quant.backtest.signal import Signal
from quant.execution.account import read_account_field
from quant.execution.kill_switch import KillSwitch
from quant.foundation.config import Settings
from quant.foundation.heartbeat import Heartbeat
from quant.foundation.logger import get_logger
from quant.models import Bar
from quant.monitor.alert_service import AlertEvent, AlertService
from quant.monitor.alerter import EmailAlerter
from quant.queue.signal_queue import Deduper, SignalQueue
from quant.risk.circuit_breaker import (
    STATUS_AWAITING_RESUME,
    CircuitBreaker,
    CircuitState,
)
from quant.risk.events import build_event_id
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage
from quant.storage.validator import DataValidator
from quant.strategies.bindings import Binding, BindingsUnavailable, read_bindings
from quant.strategies.loader import StrategyConfigError
from quant.strategies.params import ParamError, resolve_strategy

log = get_logger("engine.live")

TickStatus = Literal[
    "signal",        # 产生了信号并已入队
    "no_signal",     # 策略判断无信号
    "duplicate",     # 该 bar 已处理过（或信号重复）
    "waiting_data",  # 本地 K 线还没追到最近已收盘 bar
    "skipped",       # kill switch / 未完成挂单等原因跳过
    "error",         # 策略配置或计算失败
]

#: 评估历史保留轮数（M3-15）。看板只显示 10 个「状态变化点」，但原始数据
#: 要留够：本地 K 线落后时每轮都是 waiting_data，只留 10 轮会把有信号的
#: 轮次全挤掉。看板端折叠连续重复状态后再取最近 10 条。
EVAL_HISTORY_LIMIT = 50


def _eval_history_key(status_key: str, strategy_id: str, inst_id: str) -> str:
    """评估历史在 Redis 中的键；与看板读取端保持同一格式。"""
    return f"{status_key}:history:{strategy_id}:{inst_id}"


@dataclass(frozen=True, slots=True)
class EngineTick:
    """一轮评估结果，便于日志与测试断言。"""

    status: TickStatus
    bar_ts: int | None = None
    signal: Signal | None = None
    reason: str = ""
    signal_count: int = 0
    catch_up: int = 0  # 本轮除最新一根外额外补评估的 bar 数


class LiveSignalEngine:
    """逐根 K 线驱动本地策略的常驻引擎。"""

    def __init__(
        self,
        settings: Settings,
        store: SQLiteStore,
        redis: Redis,
        queue: SignalQueue | None = None,
        deduper: Deduper | None = None,
        bars: BarStorage | None = None,
    ) -> None:
        self._settings = settings
        self._cfg = settings.engine
        self._store = store
        self._redis = redis
        self._queue = queue or SignalQueue(
            redis,
            pending_key=settings.redis.queue_key,
            inflight_key=settings.redis.inflight_key,
            dead_key=settings.redis.dead_key,
        )
        self._deduper = deduper or Deduper(
            redis,
            prefix=settings.redis.dedup_prefix,
            ttl_seconds=settings.redis.dedup_ttl_seconds,
        )
        self._bars = bars or BarStorage(settings.storage.data_dir)
        self._kill = KillSwitch(redis)
        # 熔断只作用单个品种，与 kill switch 的全局状态键完全分开（M7-13）
        self._circuit = (
            CircuitBreaker(redis) if settings.risk.circuit_enabled else None
        )
        # 熔断是引擎侧事件，直接落库 + 推看板 + 邮件，不必等 monitor 轮询
        self._alerts = AlertService(
            store, redis, EmailAlerter(settings.smtp), settings.monitor
        )
        self._heartbeat = Heartbeat(
            redis, "engine", interval=self._cfg.interval_seconds
        )
        self._running = False
        self._started_ts = int(time.time() * 1000)
        # 进程内累计计数，按绑定分桶（随状态 JSON 发布给看板，重启归零）
        self._counters: dict[tuple[str, str], dict[str, int]] = {}
        self._bar_ms_cache: dict[str, int] = {}
        try:
            self._bar_ms = DataValidator._bar_size_ms(self._cfg.bar)  # type: ignore[attr-defined]
        except ValueError as e:
            raise ValueError(f"engine.bar invalid: {self._cfg.bar!r}") from e

    async def run(self) -> None:
        """主循环，直到 stop()。"""
        self._running = True
        log.info(
            f"strategy engine started interval={self._cfg.interval_seconds}s "
            f"multi_binding={self._cfg.multi_binding} "
            f"fallback={self._cfg.strategy}/{self._cfg.inst}/{self._cfg.bar}"
        )
        while self._running:
            started = time.monotonic()
            try:
                results = await self.run_all()
                for binding, tick in results:
                    await self.publish_status(tick, binding)
                await self._heartbeat.beat(
                    {
                        "strategy": self._cfg.strategy,
                        "inst": self._cfg.inst,
                        "bindings": len(results),
                    }
                )
            except Exception as e:  # noqa: BLE001
                log.exception(f"engine cycle failed: {e}")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self._cfg.interval_seconds - elapsed))
        log.info("strategy engine stopped")

    def stop(self) -> None:
        self._running = False

    async def run_once(self, now_ms: int | None = None) -> EngineTick:
        """评估所有启用绑定，返回「主组合」的结果（兼容单绑定调用）。

        主组合是 `[engine] strategy/inst` 指定的那一对；它不在启用列表里时
        返回第一个组合的结果，便于日志与测试仍能拿到一个明确的状态。
        """
        results = await self.run_all(now_ms)
        if not results:
            return EngineTick("skipped", reason="no enabled bindings")
        primary = (self._cfg.strategy, self._cfg.inst)
        for binding, tick in results:
            if binding.key == primary:
                return tick
        return results[0][1]

    async def run_all(
        self, now_ms: int | None = None
    ) -> list[tuple[Binding, EngineTick]]:
        """跑本轮所有启用绑定；读不到矩阵时激活 kill switch 并跳过。"""
        now = int(time.time() * 1000) if now_ms is None else now_ms
        try:
            bindings = await read_bindings(self._redis, self._store, self._settings)
        except BindingsUnavailable as e:
            await self._halt(e)
            return []

        enabled = [item for item in bindings if item.enabled]
        if not enabled:
            log.debug("no enabled bindings, skip cycle")
            return []

        # 账户可用资金是账户级快照，一轮读一次即可
        cash = await self._available_cash()
        results: list[tuple[Binding, EngineTick]] = []
        for binding in enabled:
            try:
                tick = await self._run_binding(binding, now, cash)
            except ValueError as e:
                # 单个绑定的周期/配置非法不应拖垮其它组合
                tick = EngineTick("error", reason=str(e))
            self._log_tick(tick, binding)
            results.append((binding, tick))
        return results

    async def _run_binding(
        self, binding: Binding, now: int, cash: Decimal
    ) -> EngineTick:
        """评估单个绑定最近一根已收盘 bar。"""
        bar_ms = self._bar_ms_for(binding.bar)
        last_closed = ((now - bar_ms) // bar_ms) * bar_ms

        window = max(self._cfg.warmup_bars, self._settings.backtest.window_size)
        bars = await asyncio.to_thread(
            self._bars.load_bars,
            binding.inst_id,
            binding.bar,
            last_closed - window * bar_ms,
            last_closed,
        )
        # 历史遗留的未收盘 bar（ts > last_closed）不参与评估
        bars = [b for b in bars if b.ts <= last_closed]
        if not bars:
            return EngineTick("waiting_data", reason="no local bars")
        if bars[-1].ts < last_closed:
            return EngineTick(
                "waiting_data",
                bars[-1].ts,
                reason=f"local bars behind (last_closed={last_closed})",
            )

        newest = bars[-1]
        done_ts = await self._last_processed(binding)
        if done_ts is not None and newest.ts <= done_ts:
            return EngineTick("duplicate", newest.ts, reason="bar already processed")

        pending = self._pending_bars(bars, done_ts, newest)
        # 正常节奏下只有最新一根待评估；多出来的就是漏掉后补评估的
        catch_up = max(len(pending) - 1, 0) if done_ts is not None else 0

        if await self._kill.is_active():
            await self._mark(binding, newest.ts)
            # 跳过的 bar 没有真正评估，不计入补评估数
            return EngineTick("skipped", newest.ts, reason="kill switch active")

        circuit_tick = await self._check_circuit(binding, pending, now)
        if circuit_tick is not None:
            await self._mark(binding, newest.ts)
            return circuit_tick

        if await self._has_open_orders(binding):
            await self._mark(binding, newest.ts)
            return EngineTick("skipped", newest.ts, reason="pending orders")

        try:
            # 与回测共用同一真相源：toml → DB 覆盖 → 显式传参
            strategy_cls, params, _sources = await asyncio.to_thread(
                resolve_strategy,
                binding.strategy_id,
                self._cfg.config_dir,
                self._store,
                resolve_from_db=self._settings.params.resolve_from_db,
            )
        except (StrategyConfigError, ParamError) as e:
            return EngineTick("error", newest.ts, reason=str(e))
        # 每个绑定一个独立实例：策略可能持有内部状态，不能跨组合共享
        strategy = strategy_cls()

        position = await asyncio.to_thread(self._store.get_position, binding.inst_id)
        ctx = Context(position=position, params=params, cash=cash)
        strategy.on_init(ctx)

        # 整个窗口都要回放（让指标就绪），但只在 pending 范围内收集信号
        pending_ts = {bar.ts for bar in pending}
        signals: list[Signal] = []
        for bar in bars:
            ctx.timestamp = bar.ts
            ctx.bars_window.append(bar)
            if len(ctx.bars_window) > window:
                del ctx.bars_window[: len(ctx.bars_window) - window]

            # 保护性出场：与回测同口径，bar 内触及止损/止盈即出场。
            # 实盘只能转成市价卖单入队，成交价由 worker 按当时盘口决定。
            exit_price = (
                strategy.protective_exit(bar, ctx) if ctx.position.is_long else None
            )
            if exit_price is not None:
                out = Signal(
                    strategy_id=strategy.strategy_id,
                    side="sell",
                    order_type="market",
                    inst_id=bar.inst_id,
                    qty=ctx.position.qty,
                    ts=bar.ts,
                )
                if bar.ts in pending_ts:
                    signals.append(out)
                # 虚拟成交按触发价记，保证后续 bar 的持仓判断与回测一致
                self._apply_virtual_fill(ctx, out, bar, price=exit_price)
                log.info(
                    f"protective exit triggered inst={bar.inst_id} "
                    f"price={exit_price} bar_ts={bar.ts}"
                )
                continue

            out = strategy.on_bar(bar, ctx)
            if out is None or bar.ts not in pending_ts:
                continue
            signals.append(out)
            # 同步虚拟持仓：否则金叉买入后，后续死叉仍认为空仓而漏掉平仓信号
            self._apply_virtual_fill(ctx, out, bar)

        if not signals:
            await self._mark(binding, newest.ts)
            return EngineTick("no_signal", newest.ts, catch_up=catch_up)

        queued: list[Signal] = []
        for signal in signals:
            signal = replace(signal, signal_id=uuid.uuid4().hex)
            if await self._deduper.is_duplicate(signal):
                continue
            await self._queue.push(signal)
            queued.append(signal)

        await self._mark(binding, newest.ts)
        if not queued:
            return EngineTick(
                "duplicate", newest.ts, reason="dedup hit", catch_up=catch_up
            )
        return EngineTick(
            "signal",
            newest.ts,
            signal=queued[-1],
            signal_count=len(queued),
            catch_up=catch_up,
        )

    # internal

    async def _check_circuit(
        self, binding: Binding, pending: list[Bar], now: int
    ) -> EngineTick | None:
        """极端行情熔断（M7-6）。

        已有熔断状态时整轮跳过该品种；冷却到期后状态变为 `awaiting_resume`，
        仍继续跳过，直到人工在看板确认（M7-12）。否则逐根检查待评估 bar，
        任一超阈值立即熔断并返回 skipped。熔断期间的 bar 直接推进水位，
        恢复后不补评估——那段行情已经过去，补发信号只会用过期价格下单。
        """
        if self._circuit is None:
            return None
        cfg = self._settings.risk
        state = await self._circuit.state(binding.inst_id, now=now)
        if state is not None:
            if state.status == STATUS_AWAITING_RESUME:
                reason = (
                    f"circuit breaker awaiting manual resume "
                    f"(cooldown ended {_clock(state.resume_at)})"
                )
            else:
                reason = (
                    f"circuit breaker active until {_clock(state.resume_at)}"
                )
            return EngineTick("skipped", pending[-1].ts, reason=reason)
        for bar in pending:
            tripped = await self._circuit.evaluate(
                bar,
                threshold=cfg.circuit_amplitude_pct,
                cooldown_seconds=cfg.circuit_cooldown_seconds,
                now=now,
                requires_confirm=cfg.circuit_resume_requires_confirm,
            )
            if tripped is None:
                continue
            await self._audit_circuit(binding, bar, tripped)
            return EngineTick(
                "skipped", bar.ts, reason=f"circuit breaker: {tripped.reason}"
            )
        return None

    async def _audit_circuit(
        self, binding: Binding, bar: Bar, state: CircuitState
    ) -> None:
        """熔断落 `risk_events` + 发告警（M7-17 熔断分支）。失败只记日志。"""
        cfg = self._settings.risk
        try:
            await asyncio.to_thread(
                self._store.insert_risk_event,
                event_id=build_event_id(
                    f"circuit:{binding.inst_id}", bar.ts, "circuit_breaker"
                ),
                ts=state.triggered_ts,
                inst_id=binding.inst_id,
                signal_id=f"circuit:{bar.ts}",
                decision="halted",
                strategy_id=binding.strategy_id,
                rule="circuit_breaker",
                limit_name="circuit_amplitude_pct",
                limit_value=str(cfg.circuit_amplitude_pct),
                actual_value=str(CircuitBreaker.amplitude(bar)),
                reason=state.reason,
            )
        except Exception as e:  # noqa: BLE001
            log.error(f"record circuit risk event failed: {e}")
        try:
            await self._alerts.publish(
                AlertEvent(
                    rule="circuit_breaker",
                    level="ERROR",
                    message=(
                        f"{binding.inst_id} {binding.bar} 触发熔断："
                        f"{state.reason}；暂停至 {_clock(state.resume_at)}"
                    ),
                    ts=state.triggered_ts,
                    inst_id=binding.inst_id,
                    strategy_id=binding.strategy_id,
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"publish circuit alert failed: {e}")

    def _pending_bars(
        self, bars: list[Bar], done_ts: int | None, newest: Bar
    ) -> list[Bar]:
        """水位之后待评估的 bar。

        首次运行（无水位）只评估最新一根，否则会把整个回放窗口的历史信号
        全部补发；有水位时逐根补评估，避免休眠期间的交叉被跳过。
        单轮补评估根数受 `max_catchup_bars` 限制，超限时丢弃更早的部分并告警
        ——停机太久时早期信号已失去意义。
        """
        if done_ts is None:
            return [newest]

        pending = [bar for bar in bars if bar.ts > done_ts]
        if not pending:
            return [newest]

        limit = max(1, self._cfg.max_catchup_bars)
        if len(pending) > limit:
            log.warning(
                f"catch-up truncated: {len(pending) - limit} older bar(s) dropped, "
                f"only last {limit} evaluated between {done_ts} and {newest.ts}"
            )
            return pending[-limit:]

        if len(pending) > 1:
            log.warning(
                f"engine catching up {len(pending)} bar(s) "
                f"between {done_ts} and {newest.ts}"
            )
        return pending

    def _apply_virtual_fill(
        self,
        ctx: Context,
        signal: Signal,
        bar: Bar,
        price: Decimal | None = None,
    ) -> None:
        """补评估时把信号记到虚拟持仓与现金上，让后续 bar 的判断与回测一致。

        实际成交由 worker 按市价执行；这里只用本根收盘价（保护性出场用触发价）
        维护策略可见的状态。
        """
        fill_price = bar.close if price is None else price
        ctx.position.apply_fill(
            Fill(
                order_id=f"catchup-{bar.ts}",
                inst_id=signal.inst_id,
                side=signal.side,
                qty=signal.qty,
                price=fill_price,
                fee=Decimal("0"),
                ts=bar.ts,
            )
        )
        notional = signal.qty * fill_price
        if signal.side == "buy":
            ctx.cash = max(ctx.cash - notional, Decimal("0"))
        else:
            ctx.cash += notional

    def _log_tick(self, tick: EngineTick, binding: Binding | None = None) -> None:
        tag = ""
        if binding is not None:
            tag = f" binding={binding.strategy_id}:{binding.inst_id}"
        if tick.status == "signal" and tick.signal is not None:
            s = tick.signal
            catch_up = (
                f" catch_up={tick.signal_count}" if tick.signal_count > 1 else ""
            )
            log.info(
                f"signal enqueued strategy={s.strategy_id} side={s.side} "
                f"inst={s.inst_id} qty={s.qty} bar_ts={tick.bar_ts}{catch_up}"
            )
        elif tick.status == "skipped":
            log.warning(f"engine skipped{tag} bar_ts={tick.bar_ts}: {tick.reason}")
        elif tick.status == "error":
            log.error(f"engine error{tag} bar_ts={tick.bar_ts}: {tick.reason}")
        elif tick.status == "no_signal":
            log.info(f"no signal{tag} bar_ts={tick.bar_ts}")
        else:
            log.debug(
                f"engine tick {tick.status}{tag} "
                f"bar_ts={tick.bar_ts} {tick.reason}"
            )

    @property
    def status_key(self) -> str:
        """主组合的状态键（单绑定看板与测试沿用）。"""
        cfg = self._cfg
        return f"{cfg.status_key}:{cfg.strategy}:{cfg.inst}"

    def _status_key_for(self, binding: Binding) -> str:
        return f"{self._cfg.status_key}:{binding.strategy_id}:{binding.inst_id}"

    async def publish_status(
        self, tick: EngineTick, binding: Binding | None = None
    ) -> None:
        """把本轮结果与累计计数写入 Redis，供只读看板展示健康度。

        多绑定时按绑定分别计数、分别写键；不传 binding 则视为主组合。
        """
        target = binding or Binding(
            self._cfg.strategy, self._cfg.inst, self._cfg.bar, True
        )
        counters = self._counters.setdefault(
            target.key, {"ticks": 0, "signals": 0, "skipped": 0, "catch_up": 0}
        )
        counters["ticks"] += 1
        if tick.status == "signal":
            counters["signals"] += tick.signal_count
        elif tick.status == "skipped":
            counters["skipped"] += 1
        counters["catch_up"] += tick.catch_up

        payload = {
            "ts": int(time.time() * 1000),
            "started_ts": self._started_ts,
            "strategy": target.strategy_id,
            "inst": target.inst_id,
            "bar": target.bar,
            "status": tick.status,
            "bar_ts": tick.bar_ts,
            "reason": tick.reason,
            "signal_count": tick.signal_count,
            "catch_up": tick.catch_up,
            "ticks": counters["ticks"],
            "signals_total": counters["signals"],
            "skipped_total": counters["skipped"],
            "catch_up_total": counters["catch_up"],
        }
        try:
            await self._redis.set(self._status_key_for(target), json.dumps(payload))
        except Exception as e:  # noqa: BLE001
            log.warning(f"write engine status failed: {e}")

        # M3-15：保留最近 10 次评估，供策略详情页展示（不含信号的轮次也记录）
        history_key = _eval_history_key(
            self._cfg.status_key, target.strategy_id, target.inst_id
        )
        try:
            await self._redis.lpush(history_key, json.dumps(payload))
            await self._redis.ltrim(history_key, 0, EVAL_HISTORY_LIMIT - 1)
        except Exception as e:  # noqa: BLE001
            log.warning(f"write engine evaluation history failed: {e}")

    async def _available_cash(self) -> Decimal:
        """优先用 monitor 发布的账户可用 USDT，读不到则用配置兜底。"""
        value = await read_account_field(
            self._redis,
            self._settings.monitor.dashboard_snapshot_key,
            "avail_usdt",
        )
        if value is not None:
            return value
        fallback = Decimal(str(self._cfg.cash_fallback))
        log.warning(f"account snapshot unavailable, use cash fallback {fallback}")
        return fallback

    async def _has_open_orders(self, binding: Binding) -> bool:
        try:
            orders = await asyncio.to_thread(self._store.get_open_orders)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read open orders failed: {e}")
            return True  # 读不到就保守跳过，避免重复下单
        return any(o.get("inst_id") == binding.inst_id for o in orders)

    async def _halt(self, error: Exception) -> None:
        """绑定读不到时停止新开仓：置 kill switch 并记录原因。

        宁可少交易也不能猜一个默认组合继续下单；运维处理完缓存/库后
        需要用 `quant kill-switch --off` 显式恢复。
        """
        log.error(f"bindings unavailable, halting new entries: {error}")
        try:
            await self._kill.enable(f"bindings unavailable: {error}")
        except Exception as e:  # noqa: BLE001
            log.error(f"activate kill switch failed: {e}")

    def _bar_ms_for(self, bar: str) -> int:
        cached = self._bar_ms_cache.get(bar)
        if cached is not None:
            return cached
        try:
            value = DataValidator._bar_size_ms(bar)  # type: ignore[attr-defined]
        except ValueError as e:
            raise ValueError(f"binding bar invalid: {bar!r}") from e
        self._bar_ms_cache[bar] = value
        return value

    @property
    def _marker_key(self) -> str:
        return f"{self._cfg.last_bar_key}:{self._cfg.strategy}:{self._cfg.inst}"

    def _marker_key_for(self, binding: Binding) -> str:
        return (
            f"{self._cfg.last_bar_key}:{binding.strategy_id}:{binding.inst_id}"
        )

    async def _last_processed(self, binding: Binding | None = None) -> int | None:
        key = self._marker_key if binding is None else self._marker_key_for(binding)
        try:
            raw = await self._redis.get(key)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read engine marker failed: {e}")
            return None
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            log.warning(f"engine marker malformed: {raw!r}")
            return None

    async def _mark(self, binding: Binding | None, bar_ts: int) -> None:
        key = self._marker_key if binding is None else self._marker_key_for(binding)
        try:
            await self._redis.set(key, str(bar_ts))
        except Exception as e:  # noqa: BLE001
            log.warning(f"write engine marker failed: {e}")


def _clock(ts_ms: int) -> str:
    """毫秒时间戳 → 本地 `MM-DD HH:MM`，用于日志与告警文案。"""
    return time.strftime("%m-%d %H:%M", time.localtime(ts_ms / 1000))


__all__ = ["EngineTick", "LiveSignalEngine", "TickStatus"]
