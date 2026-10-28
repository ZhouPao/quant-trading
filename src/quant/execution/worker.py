"""Worker 主循环（spec §9）。

流程：BRPOP → 风控 → 执行 → 持久化 → 从 inflight 移除。
- 启动时把 inflight 回 pending（崩溃恢复）
- 处理前查 SQLite 状态，已 executed 的信号直接丢弃（幂等）
- 每分钟写心跳到 Redis
- 单条处理异常入死信，主循环不退出
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal

from quant.backtest.signal import Signal
from quant.execution.broker import Broker
from quant.execution.kill_switch import KillSwitch
from quant.foundation.logger import get_logger
from quant.foundation.schedule import day_start_ms_utc8
from quant.queue.signal_queue import SignalQueue
from quant.risk.checker import RiskChecker
from quant.state.sqlite_store import SQLiteStore

log = get_logger("execution.worker")


class Worker:
    """信号消费 worker。"""

    HEARTBEAT_KEY = "quant:heartbeat:worker"
    HEARTBEAT_TTL = 120

    def __init__(
        self,
        queue: SignalQueue,
        risk: RiskChecker,
        executor: Broker,
        store: SQLiteStore,
        redis,
        price_provider: Callable[[str], Decimal | None] | None = None,
        heartbeat_interval: int = 60,
        reconciler=None,
        kill_switch: KillSwitch | None = None,
        reconcile_interval: int = 60,
        equity_provider: Callable[[], Awaitable[Decimal | None]] | None = None,
        risk_limits_key: str | None = None,
        risk_limits: dict[str, object] | None = None,
        risk_limits_ttl: int = 300,
    ) -> None:
        self.queue = queue
        self.risk = risk
        self.executor = executor
        self.store = store
        self.redis = redis
        self.price_provider = price_provider
        self.equity_provider = equity_provider
        self.heartbeat_interval = heartbeat_interval
        self.reconciler = reconciler
        self.kill_switch = kill_switch
        self.reconcile_interval = reconcile_interval
        # 生效风控参数由本进程发布：paper-mode 覆盖只存在于 CLI 参数里，
        # 看板直读 toml 会显示未生效的旧值。
        self.risk_limits_key = risk_limits_key
        self.risk_limits = risk_limits
        self.risk_limits_ttl = risk_limits_ttl
        self._running = False

    async def run(self) -> None:
        """主循环，直到 stop() 被调用。"""
        self._running = True
        recovered = await self.queue.recover_inflight()
        if recovered:
            log.info(f"recovered {len(recovered)} inflight signals")

        if self.reconciler is not None:
            mismatches = await self.reconciler.reconcile()
            if mismatches:
                log.warning(
                    f"reconciliation found {mismatches} mismatched orders"
                )

        heartbeat = asyncio.create_task(self._heartbeat_loop())
        tasks = [heartbeat]
        if self.reconciler is not None and self.reconcile_interval > 0:
            tasks.append(asyncio.create_task(self._reconcile_loop()))
        try:
            while self._running:
                await self.run_once(timeout=1)
        finally:
            self._running = False
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            log.info("worker stopped")

    def stop(self) -> None:
        """请求优雅退出。"""
        self._running = False

    async def run_once(self, timeout: int = 1) -> bool:
        """处理一条信号。返回 True 表示处理了一条。"""
        signal = await self.queue.pop(timeout=timeout)
        if signal is None:
            return False

        # 紧急停止：拒绝新信号，入死信并移除 inflight
        if self.kill_switch is not None and await self.kill_switch.is_active():
            reason = (await self.kill_switch.reason()) or "kill-switch active"
            log.warning(
                f"kill-switch active, reject signal id={signal.signal_id}"
            )
            await self.queue.push_dead(signal, reason=f"kill-switch: {reason}")
            await self.queue.remove_inflight(signal)
            return True

        try:
            await self._process(signal)
        except Exception as e:
            log.exception(f"process failed signal={signal.signal_id}")
            await self.queue.push_dead(signal, reason=str(e))
            await self.queue.remove_inflight(signal)
        return True

    async def _process(self, signal: Signal) -> None:
        """风控 → 执行 → 持久化。"""
        # 幂等：崩溃恢复时避免重复下单
        status = await asyncio.to_thread(
            self.store.get_signal_status, signal.signal_id
        )
        if status in ("executed", "rejected"):
            log.info(
                f"signal already handled ({status}), skip id={signal.signal_id}"
            )
            await self.queue.remove_inflight(signal)
            return

        market_price = (
            self.price_provider(signal.inst_id) if self.price_provider else None
        )
        equity = (
            await self.equity_provider() if self.equity_provider else None
        )

        decision = await asyncio.to_thread(
            self.risk.check,
            signal,
            market_price,
            day_start_ms_utc8(),
            equity=equity,
        )
        if not decision.approved:
            await asyncio.to_thread(
                self.store.save_signal, signal, "rejected"
            )
            log.warning(
                f"signal rejected id={signal.signal_id} reason={decision.reason}"
            )
            # 硬性风控上限（日亏损/连续亏损）：自动触发紧急停止
            if decision.halt and self.kill_switch is not None:
                await self.kill_switch.enable(
                    f"auto: {decision.reason}"
                )
                log.error(
                    f"kill-switch auto-enabled by risk limit: "
                    f"{decision.reason}"
                )
            await self.queue.remove_inflight(signal)
            return

        result = await self.executor.execute(
            signal, decision.adjusted_qty, market_price
        )
        await asyncio.to_thread(self.store.save_signal, signal, "executed")
        await asyncio.to_thread(self.store.save_order, result.order)

        if result.fill is not None:
            fill = result.fill
            await asyncio.to_thread(
                self.store.update_order_fill, result.order.order_id, fill
            )
            await asyncio.to_thread(self.store.save_fill, fill)

            position = await asyncio.to_thread(
                self.store.get_position, fill.inst_id
            )
            position.apply_fill(fill)
            await asyncio.to_thread(self.store.update_position, position)

        await self.queue.remove_inflight(signal)
        log.info(
            f"signal executed id={signal.signal_id} "
            f"order={result.order.order_id} status={result.order.status}"
        )

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await self.redis.set(
                self.HEARTBEAT_KEY,
                int(time.time() * 1000),
                ex=self.HEARTBEAT_TTL,
            )
            await self._publish_risk_limits()
            await asyncio.sleep(self.heartbeat_interval)

    async def _publish_risk_limits(self) -> None:
        """发布本进程实际生效的风控参数（含 paper-mode 覆盖）。

        看板只读该键；TTL 取多个心跳周期，worker 停机后键自然过期，
        卡片据此显示「未知」而不是展示过期的生效值。
        """
        if not self.risk_limits_key or self.risk_limits is None:
            return
        payload = {**self.risk_limits, "ts": int(time.time() * 1000)}
        try:
            await self.redis.set(
                self.risk_limits_key,
                json.dumps(payload, ensure_ascii=False),
                ex=self.risk_limits_ttl,
            )
        except Exception as e:  # noqa: BLE001
            # 发布失败不影响交易主流程
            log.warning(f"publish risk limits failed: {e}")

    async def _reconcile_loop(self) -> None:
        """运行期定时对账：刷新挂单终态并撤销超时挂单。"""
        while self._running:
            await asyncio.sleep(self.reconcile_interval)
            await self._reconcile_once()

    async def _reconcile_once(self) -> None:
        try:
            changed = await self.reconciler.reconcile()
            if changed:
                log.warning(
                    f"periodic reconciliation changed {changed} orders"
                )
        except Exception as e:  # noqa: BLE001
            # 单轮对账失败不能影响主消费循环
            log.exception(f"periodic reconciliation failed: {e}")


__all__ = ["Worker"]
