"""交易所三项对账（F3.5/F3.7，M8-18）。

worker 启动时与运行期定时执行，比对三类对象：

- 订单：遍历本地未终结订单，向交易所查询真实状态。远端已 filled 的补写
  成交与持仓；已 canceled 的本地标记 canceled；仍 live 的超时撤单。
- 持仓：本地 SQLite 记账 vs 交易所持仓（合约按 `ctVal` 折成基础币，
  现货取余额明细）。本地多于交易所会因可用量不足卖不出去，**自动下调**
  到交易所实际量；本地少于交易所多为外部转入，成本基数未知，**只记录**
  交人工确认。
- 余额：本地最近一次权益快照 vs 交易所实时 `totalEq`。快照过旧说明本地
  视图不可信，跳过本轮而不是报一个假差异；差异一律交人工确认（现金变动
  无法从成交流水反推，自动改账只会掩盖问题）。

差异写 `reconciliations` 表，`severity=auto` 表示本地已被校正，
`manual` 表示需要人工确认；持续存在的差异原地刷新，不每轮插新行。
新增的 `manual` 差异通过 `AlertService` 发一条 `reconcile_mismatch` 告警
（同一 `kind:key` 只报一次，直到它消失后再次出现）。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from quant.backtest.position import Position
from quant.execution.okx_broker import OkxBroker, detail_to_fill
from quant.foundation.logger import get_logger
from quant.monitor.snapshot import AccountSnapshot
from quant.okx.client import OKXClient
from quant.state.sqlite_store import SQLiteStore

log = get_logger("execution.reconciler")

_CANCELED_STATES = ("canceled", "mmp_canceled")
_OPEN_STATES = ("live", "partially_filled")
#: 持仓对账覆盖的合约类型（现货走余额明细，不在此列）
_POSITION_INST_TYPES = ("SWAP", "FUTURES")
#: 默认持仓容差（基础币）：小于该值的零头视为精度误差，不记差异
POSITION_TOLERANCE = Decimal("0.0001")
#: 默认余额容差（USDT）：手续费/利息的四舍五入不应触发对账
BALANCE_TOLERANCE = Decimal("0.01")


def _is_contract(inst_id: str) -> bool:
    """是否合约品种：BTC-USDT-SWAP 至少两个连字符。"""
    return inst_id.count("-") >= 2


def _dec(value: object) -> Decimal:
    """容错转 Decimal：缺失/非法按 0。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _spot_qty(item: dict) -> Decimal:
    """现货余额数量：优先 cashBal，其次 eq。"""
    for field in ("cashBal", "eq"):
        raw = item.get(field)
        if raw in (None, ""):
            continue
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
    return Decimal("0")


class Reconciler:
    """本地记账与交易所的订单/持仓/余额对账。"""

    def __init__(
        self,
        store: SQLiteStore,
        broker: OkxBroker,
        order_timeout_seconds: int = 0,
        *,
        client: OKXClient | None = None,
        snapshot_max_age_ms: int = 180_000,
        position_tolerance: Decimal = POSITION_TOLERANCE,
        balance_tolerance: Decimal = BALANCE_TOLERANCE,
        alert_service=None,
    ) -> None:
        if order_timeout_seconds < 0:
            raise ValueError("order_timeout_seconds must be >= 0")
        self.store = store
        self.broker = broker
        self.order_timeout_seconds = order_timeout_seconds
        # client 为空时只做订单对账（历史行为，mock broker 也走这里）
        self.client = client
        self.snapshot_max_age_ms = snapshot_max_age_ms
        self.position_tolerance = position_tolerance
        self.balance_tolerance = balance_tolerance
        self.alert_service = alert_service
        #: 本轮发现的差异（测试与调用方读取）
        self.discrepancies: list[dict] = []
        #: 已报过告警的 manual 差异键，消失后再次出现才会重报
        self._alerted: set[str] = set()
        self._balance_raw: dict | None = None

    async def reconcile(self, now_ms: int | None = None) -> int:
        """对账一轮，返回订单状态发生变化的数量。

        持仓/余额差异不影响返回值（它们是记录+告警，不改变订单状态），
        调用方可从 `self.discrepancies` 读取本轮结果。
        """
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        self.discrepancies = []
        self._balance_raw = None
        changed = await self._reconcile_orders(now)
        if self.client is not None:
            await self._reconcile_positions(now)
            await self._reconcile_balance(now)
        if self.discrepancies:
            log.warning(
                f"reconciliation found {len(self.discrepancies)} "
                f"discrepancies ({changed} order changes)"
            )
        await self._publish_alerts(now)
        return changed

    async def _reconcile_orders(self, now_ms: int) -> int:
        """订单状态对账；状态推进后记一条 auto 差异留痕。"""
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
                self._record_auto(
                    "order",
                    order_id,
                    local=str(order.get("status") or "pending"),
                    remote=state,
                    note="远端已撤销，本地标记 canceled",
                    now_ms=now_ms,
                )
                log.warning(f"reconciled {order_id}: pending -> canceled")
                continue

            try:
                ct_val = await self.broker.ct_val(inst_id)
            except Exception as e:  # noqa: BLE001
                log.warning(f"reconcile {order_id}: 查不到合约面值，跳过：{e}")
                continue

            fill = detail_to_fill(
                inst_id, order_id, remote, fallback_ts=now_ms, ct_val=ct_val
            )
            if fill is not None:
                applied = await self._apply_fill(fill, state, now_ms)
                if applied:
                    mismatches += 1
                    self._record_auto(
                        "order",
                        order_id,
                        local=str(order.get("status") or "pending"),
                        remote=state,
                        note=f"按远端成交补记 {fill.qty} @ {fill.price}",
                        now_ms=now_ms,
                    )
                continue

            if state in _OPEN_STATES and self._is_stale(order, now_ms):
                stale.append(order)

        if stale:
            canceled_ids = await self._cancel_stale(stale)
            mismatches += len(canceled_ids)
            by_id = {str(o["order_id"]): o for o in stale}
            for order_id in canceled_ids:
                order = by_id.get(order_id, {})
                self._record_auto(
                    "order",
                    order_id,
                    local=str(order.get("status") or "pending"),
                    remote="canceled",
                    note=f"挂单超过 {self.order_timeout_seconds}s，已撤单",
                    now_ms=now_ms,
                )

        return mismatches

    async def _reconcile_positions(self, now_ms: int) -> None:
        """持仓数量对账：合约与本地已知品种逐一比对。"""
        remote = await self._remote_positions()
        if remote is None:
            return
        local_rows = await asyncio.to_thread(self.store.get_all_positions)
        local = {p.inst_id: p for p in local_rows}

        # 只比对「本地记账过的品种」与「交易所合约持仓」：现货余额里的
        # 其它币种多半是用户自持（空投/理财），纳入比对只会天天误报。
        keys = set(local) | {
            inst_id for inst_id in remote if _is_contract(inst_id)
        }
        for inst_id in sorted(keys):
            held = local.get(inst_id)
            local_qty = held.qty if held is not None else Decimal("0")
            remote_qty = remote.get(inst_id, Decimal("0"))
            diff = local_qty - remote_qty
            if abs(diff) <= self.position_tolerance:
                continue

            if diff > 0:
                # 本地多记：不校正会因可用量不足卖不出去，按远端下调
                avg = held.avg_price if held is not None else Decimal("0")
                realized = held.realized_pnl if held is not None else Decimal("0")
                await asyncio.to_thread(
                    self.store.update_position,
                    Position(
                        inst_id=inst_id,
                        qty=remote_qty,
                        avg_price=avg if remote_qty > 0 else Decimal("0"),
                        realized_pnl=realized,
                    ),
                )
                self._record_auto(
                    "position",
                    inst_id,
                    local=str(local_qty),
                    remote=str(remote_qty),
                    diff=str(diff),
                    note="本地多于交易所，已按远端下调（防止卖出不存在的仓位）",
                    now_ms=now_ms,
                )
                log.warning(
                    f"reconciled position {inst_id}: {local_qty} -> {remote_qty}"
                )
            else:
                # 本地少记：多为外部转入，成本基数未知，不自动加仓
                self._record_manual(
                    "position",
                    inst_id,
                    local=str(local_qty),
                    remote=str(remote_qty),
                    diff=str(diff),
                    note="交易所多于本地，可能是外部转入/历史数据缺失，请人工确认",
                    now_ms=now_ms,
                )

    async def _reconcile_balance(self, now_ms: int) -> None:
        """余额对账：本地最近权益快照 vs 交易所实时总权益。"""
        raw = await self._balance()
        if raw is None:
            return
        snapshot = await asyncio.to_thread(
            self.store.get_latest_equity_snapshot
        )
        if snapshot is None:
            return
        age = now_ms - int(snapshot["ts"])
        if self.snapshot_max_age_ms > 0 and age > self.snapshot_max_age_ms:
            log.info(
                f"balance reconcile skipped: snapshot age {age}ms "
                f"> {self.snapshot_max_age_ms}ms"
            )
            return
        local_equity = _dec(snapshot["total_equity"])
        remote_equity = AccountSnapshot.from_okx(raw, now_ms).total_equity
        if local_equity <= 0 or remote_equity <= 0:
            return
        diff = local_equity - remote_equity
        if abs(diff) <= self.balance_tolerance:
            return
        self._record_manual(
            "balance",
            "total_equity",
            local=str(local_equity),
            remote=str(remote_equity),
            diff=str(diff),
            note="本地为最近一次权益快照；差异可能来自出入金/手续费/利息，请人工确认",
            now_ms=now_ms,
        )


    def _record_auto(
        self,
        kind: str,
        key: str,
        *,
        local: str | None,
        remote: str | None,
        note: str,
        now_ms: int,
        diff: str | None = None,
    ) -> None:
        """记录一条已自动修复的差异（历史留痕，不再进入待办）。"""
        self.discrepancies.append(
            {
                "kind": kind,
                "key": key,
                "local_value": local,
                "remote_value": remote,
                "diff": diff,
                "severity": SQLiteStore.RECONCILE_AUTO,
                "note": note,
            }
        )
        try:
            self.store.record_reconciliation(
                kind=kind,
                key=key,
                local_value=local,
                remote_value=remote,
                diff=diff,
                severity=SQLiteStore.RECONCILE_AUTO,
                note=note,
                now_ms=now_ms,
                resolved=True,
            )
        except Exception as e:  # noqa: BLE001
            # 记录失败不能阻断对账，否则一次写库异常会让后续订单状态停滞
            log.warning(f"persist reconciliation failed: {e}")

    def _record_manual(
        self,
        kind: str,
        key: str,
        *,
        local: str | None,
        remote: str | None,
        note: str,
        now_ms: int,
        diff: str | None = None,
    ) -> None:
        """记录一条需人工确认的差异（同类只保留一条未处理记录）。"""
        self.discrepancies.append(
            {
                "kind": kind,
                "key": key,
                "local_value": local,
                "remote_value": remote,
                "diff": diff,
                "severity": SQLiteStore.RECONCILE_MANUAL,
                "note": note,
            }
        )
        try:
            self.store.upsert_open_reconciliation(
                kind=kind,
                key=key,
                local_value=local,
                remote_value=remote,
                diff=diff,
                severity=SQLiteStore.RECONCILE_MANUAL,
                note=note,
                now_ms=now_ms,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"persist reconciliation failed: {e}")

    async def _remote_positions(self) -> dict[str, Decimal] | None:
        """交易所持仓（基础币口径）：合约按 ctVal 折算，现货取余额明细。

        整体拉取失败（网络/鉴权）返回 None 表示「本轮无法对账」，避免把
        一次接口失败当成「交易所没有持仓」而误删本地仓位。
        """
        if self.client is None:
            return None
        out: dict[str, Decimal] = {}
        fetched = False
        for inst_type in _POSITION_INST_TYPES:
            try:
                rows = await asyncio.to_thread(
                    self.client.get_positions, inst_type
                )
            except Exception as e:  # noqa: BLE001
                log.warning(f"reconcile positions {inst_type} failed: {e}")
                continue
            fetched = True
            for raw in rows:
                inst_id = str(raw.get("instId") or "")
                qty = _dec(raw.get("pos"))
                if not inst_id or qty == 0:
                    continue
                ct_val = Decimal("1")
                if _is_contract(inst_id):
                    try:
                        ct_val = await self.broker.ct_val(inst_id)
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            f"reconcile position {inst_id}: 查不到面值：{e}"
                        )
                        continue
                    if ct_val <= 0:
                        continue
                out[inst_id] = out.get(inst_id, Decimal("0")) + qty * ct_val

        raw_balance = await self._balance()
        if raw_balance is not None:
            fetched = True
            for item in raw_balance.get("details") or []:
                ccy = str(item.get("ccy") or "").upper()
                if not ccy or ccy == "USDT":
                    continue
                qty = _spot_qty(item)
                if qty == 0:
                    continue
                inst_id = f"{ccy}-USDT"
                out[inst_id] = out.get(inst_id, Decimal("0")) + qty
        return out if fetched else None

    async def _balance(self) -> dict | None:
        """原始余额响应（一轮只拉一次，持仓与余额对账共用）。"""
        if self.client is None:
            return None
        if self._balance_raw is not None:
            return self._balance_raw
        try:
            self._balance_raw = await asyncio.to_thread(
                self.client.get_balance
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"reconcile balance failed: {e}")
            return None
        return self._balance_raw

    async def _publish_alerts(self, now_ms: int) -> None:
        """新增的 manual 差异发一条告警；同类差异只报一次。"""
        manual = {
            f"{d['kind']}:{d['key']}": d
            for d in self.discrepancies
            if d["severity"] == SQLiteStore.RECONCILE_MANUAL
        }
        fresh = [key for key in manual if key not in self._alerted]
        # 消失的差异从「已报」集合移除：再次出现时重新提醒
        self._alerted &= set(manual)
        if self.alert_service is None or not fresh:
            return
        self._alerted.update(fresh)
        parts = [
            f"{d['kind']} {d['key']} 本地 {d['local_value']} / "
            f"交易所 {d['remote_value']}"
            for key, d in manual.items()
            if key in fresh
        ]
        message = f"对账差异 {len(fresh)} 项待确认：" + "；".join(parts)
        inst_id = (
            fresh[0].split(":", 1)[1]
            if len(fresh) == 1 and fresh[0].startswith("position:")
            else None
        )
        try:
            # 延迟导入：alert_service 依赖 monitor 模块，避免无谓的导入环
            from quant.monitor.alert_service import AlertEvent

            await self.alert_service.publish(
                AlertEvent(
                    rule="reconcile_mismatch",
                    level="WARNING",
                    message=message,
                    ts=now_ms,
                    inst_id=inst_id,
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"publish reconcile alert failed: {e}")

    def _is_stale(self, order: dict, now_ms: int) -> bool:
        """挂单是否已超过超时阈值；未开启或缺少创建时间视为未超时。"""
        if self.order_timeout_seconds <= 0:
            return False
        created_ts = int(order.get("created_ts") or 0)
        if created_ts <= 0:
            return False
        return now_ms - created_ts >= self.order_timeout_seconds * 1000

    async def _cancel_stale(self, orders: list[dict]) -> list[str]:
        """批量撤销超时挂单，返回**确实撤销成功**的订单号。"""
        pairs = [
            (str(o["inst_id"]), str(o["order_id"]))
            for o in orders
            if o.get("inst_id") and o.get("order_id")
        ]
        if not pairs:
            return []

        results = await self.broker.cancel_orders(pairs)
        canceled: list[str] = []
        for inst_id, order_id in pairs:
            code = results.get(order_id, "-1")
            if code == "0":
                canceled.append(order_id)
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
