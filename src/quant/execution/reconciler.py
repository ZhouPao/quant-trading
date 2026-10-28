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
                mismatches += 1
                await self._apply_fill(fill)
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

    async def _apply_fill(self, fill) -> None:
        """以远端成交为准，补写订单成交与持仓。"""
        await asyncio.to_thread(
            self.store.update_order_fill, fill.order_id, fill
        )
        await asyncio.to_thread(self.store.save_fill, fill)
        position = await asyncio.to_thread(
            self.store.get_position, fill.inst_id
        )
        position.apply_fill(fill)
        await asyncio.to_thread(self.store.update_position, position)
        log.warning(
            f"reconciled {fill.order_id}: pending -> filled "
            f"qty={fill.qty} px={fill.price}"
        )


__all__ = ["Reconciler"]
