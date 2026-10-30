"""交易所订单对账（F3.5/F3.7）。

worker 启动时与运行期定时执行，遍历本地 status='pending' 的订单，
向交易所查询真实状态：

- 远端已 filled：补写成交 + 更新持仓，以远端为准
- 远端已 canceled：本地标记 canceled
- 远端仍 live/partially_filled：保持 pending，下次再对账
- 挂单超过 order_timeout_seconds：批量撤单并把本地标记 canceled

order_timeout_seconds=0 表示关闭超时撤单。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal

from quant.execution.okx_broker import OkxBroker, detail_to_fill
from quant.foundation.logger import get_logger
from quant.state.sqlite_store import SQLiteStore

log = get_logger("execution.reconciler")

_CANCELED_STATES = ("canceled", "mmp_canceled")
_OPEN_STATES = ("live", "partially_filled")


class Reconciler:
    """本地挂单与交易所订单状态对账。"""

    def __init__(
        self,
        store: SQLiteStore,
        broker: OkxBroker,
        order_timeout_seconds: int = 0,
    ) -> None:
        if order_timeout_seconds < 0:
            raise ValueError("order_timeout_seconds must be >= 0")
        self.store = store
        self.broker = broker
        self.order_timeout_seconds = order_timeout_seconds

    async def reconcile(self, now_ms: int | None = None) -> int:
        """对账一轮，返回状态发生变化的订单数。"""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        open_orders = await asyncio.to_thread(self.store.get_open_orders)
        mismatches = 0
        stale: list[dict] = []
        for order in open_orders:
            order_id = order["order_id"]
            inst_id = order.get("inst_id")
            if not inst_id:
                log.warning(f"open order {order_id} missing inst_id, skip")
                continue

            remote = await self.broker.get_order(inst_id, order_id)
            state = str(remote.get("state", ""))
            if not state:
                continue

            if state in _CANCELED_STATES:
                mismatches += 1
                await asyncio.to_thread(
                    self.store.update_order_status, order_id, "canceled"
                )
                log.warning(f"reconciled {order_id}: pending -> canceled")
                continue

            try:
                ct_val = await self.broker.ct_val(inst_id)
            except Exception as e:  # noqa: BLE001
                log.warning(f"reconcile {order_id}: 查不到合约面值，跳过：{e}")
                continue

            fill = detail_to_fill(
                inst_id, order_id, remote, fallback_ts=now, ct_val=ct_val
            )
            if fill is not None:
                mismatches += await self._apply_fill(fill, state, now)
                continue

            if state in _OPEN_STATES and self._is_stale(order, now):
                stale.append(order)

        if stale:
            mismatches += await self._cancel_stale(stale)

        if mismatches:
            log.warning(f"reconciliation found {mismatches} changed orders")
        return mismatches

    def _is_stale(self, order: dict, now_ms: int) -> bool:
        """挂单是否已超过超时阈值；未开启或缺少创建时间视为未超时。"""
        if self.order_timeout_seconds <= 0:
            return False
        created_ts = int(order.get("created_ts") or 0)
        if created_ts <= 0:
            return False
        return now_ms - created_ts >= self.order_timeout_seconds * 1000

    async def _cancel_stale(self, orders: list[dict]) -> int:
        """批量撤销超时挂单，成功撤销的本地标记 canceled。"""
        pairs = [
            (str(o["inst_id"]), str(o["order_id"]))
            for o in orders
            if o.get("inst_id") and o.get("order_id")
        ]
        if not pairs:
            return 0

        results = await self.broker.cancel_orders(pairs)
        canceled = 0
        for inst_id, order_id in pairs:
            code = results.get(order_id, "-1")
            if code == "0":
                canceled += 1
                await asyncio.to_thread(
                    self.store.update_order_status, order_id, "canceled"
                )
                log.warning(
                    f"order {order_id} exceeded "
                    f"{self.order_timeout_seconds}s, canceled "
                    f"(inst={inst_id})"
                )
            else:
                # 撤单失败（可能已成交/已撤销）：保持 pending，下轮对账再判定
                log.warning(
                    f"timeout cancel failed inst={inst_id} "
                    f"ordId={order_id} sCode={code}"
                )
        return canceled

    async def _apply_fill(self, fill, remote_state: str, now_ms: int) -> int:
        """以远端成交为准，按**增量**补写订单成交与持仓。

        `detail_to_fill` 返回的是订单累计成交（OKX 的 `accFillSz` 语义），
        而 `update_order_fill` 负责累加。因此这里必须先与本地已落库的量
        相减，否则每次对账都会把累计量再加一遍，成交量翻倍膨胀。

        增量成交的时间戳用**本次对账时间**：远端 `fillTime` 在多次部分
        成交之间可能相同，用它会让 `fills(order_id, ts)` 主键冲突，后一条
        冲掉前一条，成交明细合计就少于实际成交量。

        Returns:
            1 表示有状态推进（计入 mismatches），0 表示无变化。
        """
        prior = await asyncio.to_thread(
            self.store.get_order_fill_state, fill.order_id
        )
        delta_qty = fill.qty - prior["filled_qty"]
        if delta_qty <= 0:
            # 已记录的成交不再重复入账；但状态可能仍需推进（如部分→全部）
            target = "filled" if remote_state in ("filled",) else "partially_filled"
            changed = await asyncio.to_thread(
                self.store.update_order_status, fill.order_id, target
            )
            return 1 if changed else 0

        delta_fee = max(fill.fee - prior["fee"], Decimal("0"))
        delta = replace(fill, qty=delta_qty, fee=delta_fee, ts=now_ms)
        target = "filled" if remote_state in ("filled",) else "partially_filled"

        await asyncio.to_thread(self.store.save_fill, delta)
        await asyncio.to_thread(
            self.store.update_order_fill, fill.order_id, delta, target
        )
        position = await asyncio.to_thread(
            self.store.get_position, delta.inst_id
        )
        position.apply_fill(delta)
        await asyncio.to_thread(self.store.update_position, position)
        log.warning(
            f"reconciled {fill.order_id}: -> {target} "
            f"delta_qty={delta_qty} px={fill.price}"
        )
        return 1


__all__ = ["Reconciler"]
