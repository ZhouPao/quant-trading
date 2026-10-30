"""本地策略实时信号引擎（不依赖 TradingView）。

每根 K 线收盘后，用本地 parquet 的历史 K 线回放策略，把产生的信号写入
Redis 队列，交给 worker 在 OKX 模拟盘执行。

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
from quant.queue.signal_queue import Deduper, SignalQueue
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage
from quant.storage.validator import DataValidator
from quant.strategies.loader import StrategyConfigError, load_strategy

log = get_logger("engine.live")

TickStatus = Literal[
    "signal",        # 产生了信号并已入队
    "no_signal",     # 策略判断无信号
    "duplicate",     # 该 bar 已处理过（或信号重复）
    "waiting_data",  # 本地 K 线还没追到最近已收盘 bar
    "skipped",       # kill switch / 未完成挂单等原因跳过
    "error",         # 策略配置或计算失败
]


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
        self._heartbeat = Heartbeat(
            redis, "engine", interval=self._cfg.interval_seconds
        )
        self._running = False
        self._started_ts = int(time.time() * 1000)
        # 进程内累计计数，随状态 JSON 发布给看板（重启归零）
        self._counters = {
            "ticks": 0,
            "signals": 0,
            "skipped": 0,
            "catch_up": 0,
        }
        try:
            self._bar_ms = DataValidator._bar_size_ms(self._cfg.bar)  # type: ignore[attr-defined]
        except ValueError as e:
            raise ValueError(f"engine.bar invalid: {self._cfg.bar!r}") from e

    async def run(self) -> None:
        """主循环，直到 stop()。"""
        self._running = True
        log.info(
            f"strategy engine started strategy={self._cfg.strategy} "
            f"inst={self._cfg.inst} bar={self._cfg.bar} "
            f"interval={self._cfg.interval_seconds}s"
        )
        while self._running:
            started = time.monotonic()
            try:
                tick = await self.run_once()
                self._log_tick(tick)
                await self.publish_status(tick)
                await self._heartbeat.beat(
                    {"strategy": self._cfg.strategy, "inst": self._cfg.inst}
                )
            except Exception as e:  # noqa: BLE001
                log.exception(f"engine cycle failed: {e}")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self._cfg.interval_seconds - elapsed))
        log.info("strategy engine stopped")

    def stop(self) -> None:
        self._running = False

    async def run_once(self, now_ms: int | None = None) -> EngineTick:
        """评估最近一根已收盘 bar。"""
        now = int(time.time() * 1000) if now_ms is None else now_ms
        last_closed = ((now - self._bar_ms) // self._bar_ms) * self._bar_ms

        window = max(self._cfg.warmup_bars, self._settings.backtest.window_size)
        bars = await asyncio.to_thread(
            self._bars.load_bars,
            self._cfg.inst,
            self._cfg.bar,
            last_closed - window * self._bar_ms,
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
        done_ts = await self._last_processed()
        if done_ts is not None and newest.ts <= done_ts:
            return EngineTick("duplicate", newest.ts, reason="bar already processed")

        pending = self._pending_bars(bars, done_ts, newest)
        # 正常节奏下只有最新一根待评估；多出来的就是漏掉后补评估的
        catch_up = max(len(pending) - 1, 0) if done_ts is not None else 0

        if await self._kill.is_active():
            await self._mark(newest.ts)
            # 跳过的 bar 没有真正评估，不计入补评估数
            return EngineTick("skipped", newest.ts, reason="kill switch active")

        if await self._has_open_orders():
            await self._mark(newest.ts)
            return EngineTick("skipped", newest.ts, reason="pending orders")

        try:
            strategy, params = await asyncio.to_thread(
                load_strategy, self._cfg.strategy, self._cfg.config_dir
            )
        except StrategyConfigError as e:
            return EngineTick("error", newest.ts, reason=str(e))

        position = await asyncio.to_thread(self._store.get_position, self._cfg.inst)
        cash = await self._available_cash()
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
            await self._mark(newest.ts)
            return EngineTick("no_signal", newest.ts, catch_up=catch_up)

        queued: list[Signal] = []
        for signal in signals:
            signal = replace(signal, signal_id=uuid.uuid4().hex)
            if await self._deduper.is_duplicate(signal):
                continue
            await self._queue.push(signal)
            queued.append(signal)

        await self._mark(newest.ts)
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

    def _log_tick(self, tick: EngineTick) -> None:
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
            log.warning(f"engine skipped bar_ts={tick.bar_ts}: {tick.reason}")
        elif tick.status == "error":
            log.error(f"engine error bar_ts={tick.bar_ts}: {tick.reason}")
        elif tick.status == "no_signal":
            log.info(f"no signal bar_ts={tick.bar_ts}")
        else:
            log.debug(f"engine tick {tick.status} bar_ts={tick.bar_ts} {tick.reason}")

    @property
    def status_key(self) -> str:
        return f"{self._cfg.status_key}:{self._cfg.strategy}:{self._cfg.inst}"

    async def publish_status(self, tick: EngineTick) -> None:
        """把本轮结果与累计计数写入 Redis，供只读看板展示健康度。"""
        self._counters["ticks"] += 1
        if tick.status == "signal":
            self._counters["signals"] += tick.signal_count
        elif tick.status == "skipped":
            self._counters["skipped"] += 1
        self._counters["catch_up"] += tick.catch_up

        payload = {
            "ts": int(time.time() * 1000),
            "started_ts": self._started_ts,
            "status": tick.status,
            "bar_ts": tick.bar_ts,
            "reason": tick.reason,
            "signal_count": tick.signal_count,
            "catch_up": tick.catch_up,
            "ticks": self._counters["ticks"],
            "signals_total": self._counters["signals"],
            "skipped_total": self._counters["skipped"],
            "catch_up_total": self._counters["catch_up"],
        }
        try:
            await self._redis.set(self.status_key, json.dumps(payload))
        except Exception as e:  # noqa: BLE001
            log.warning(f"write engine status failed: {e}")

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

    async def _has_open_orders(self) -> bool:
        try:
            orders = await asyncio.to_thread(self._store.get_open_orders)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read open orders failed: {e}")
            return True  # 读不到就保守跳过，避免重复下单
        return any(o.get("inst_id") == self._cfg.inst for o in orders)

    @property
    def _marker_key(self) -> str:
        return f"{self._cfg.last_bar_key}:{self._cfg.strategy}:{self._cfg.inst}"

    async def _last_processed(self) -> int | None:
        try:
            raw = await self._redis.get(self._marker_key)
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

    async def _mark(self, bar_ts: int) -> None:
        try:
            await self._redis.set(self._marker_key, str(bar_ts))
        except Exception as e:  # noqa: BLE001
            log.warning(f"write engine marker failed: {e}")


__all__ = ["EngineTick", "LiveSignalEngine", "TickStatus"]
