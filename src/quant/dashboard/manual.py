"""看板手动交易：减仓/平仓，以及市价/限价开仓、撤单、改单。

与 `service.py` 的只读职责分开：本模块会真实下单并写 SQLite。

执行路径是直连 OKX（不经信号队列），因此这里自己兜住三件事：
1. 下单前向交易所查可用量，实际下单量 = min(请求量, 可用量)；
2. 合约减仓单必须带 `reduceOnly`，避免方向算错时开出反向新仓；
3. 同品种用 Redis 短锁串行化，防止连点重复下单。

开仓单（M8-17）与自动单共用同一套约束：kill switch、品种熔断、
`RiskChecker` 五条规则、`risk_events` 审计、`orders` 状态机。
风控参数取 `[risk]` 全局层（手动单没有策略归属），权益必须来自
monitor 快照且未过期——打不开新敞口时宁可拒单，不做兜底。

数量口径：对外一律用「基础币数量」；合约在内部按 `ctVal` 换算成张数，
回执里同时给出 `contracts`，便于对照交易所界面。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from redis.asyncio import Redis

from quant.backtest.order import Fill
from quant.backtest.signal import OrderType, Side, Signal
from quant.execution.account import read_account_field
from quant.execution.kill_switch import KillSwitch
from quant.execution.okx_broker import OkxBroker
from quant.execution.price import RestPriceProvider
from quant.foundation.config import Settings
from quant.foundation.errors import QuantError
from quant.foundation.logger import get_logger
from quant.foundation.schedule import day_start_ms_utc8
from quant.models.instrument import Instrument
from quant.okx.client import OKXClient
from quant.risk.checker import RiskChecker
from quant.risk.circuit_breaker import CircuitBreaker
from quant.risk.config import RiskConfig
from quant.risk.limits import LIMIT_FIELDS, resolve_limits
from quant.state.sqlite_store import SQLiteStore

log = get_logger("dashboard.manual")

#: 手动单的策略标识：看板据此区分来源（执行页筛选与审计）
MANUAL_STRATEGY_ID = "manual"
# 秒级互斥，够覆盖一次市价单的往返
LOCK_TTL_SECONDS = 5
# 单次平仓最多处理的品种数，防御异常数据导致的超长循环
MAX_FLATTEN_ITEMS = 20


class ManualTradeError(Exception):
    """手动交易失败；`status` 直接映射 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _dec(raw: object) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    """按步长向下取整（OKX 的 lotSz 要求）。"""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _is_contract(inst_id: str) -> bool:
    """是否合约品种：SWAP/FUTURES 形如 BTC-USDT-SWAP、BTC-USD-251226，
    至少两个连字符；现货只有 BASE-QUOTE 一个连字符。"""
    return inst_id.count("-") >= 2


class ManualTrader:
    """手动减仓/平仓执行器。"""

    def __init__(
        self,
        settings: Settings,
        store: SQLiteStore,
        redis: Redis | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._redis = redis
        self._td_mode = settings.execution.td_mode
        self._client: OKXClient | None = None
        self._broker: OkxBroker | None = None
        self._rest_price: RestPriceProvider | None = None

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def _ensure_client(self) -> tuple[OKXClient, OkxBroker]:
        """懒加载 OKX 客户端：看板启动不依赖网络，首次操作才建连。"""
        okx = self._settings.okx
        if not (okx.api_key and okx.secret and okx.passphrase):
            raise ManualTradeError(
                "未配置 OKX 凭据，无法手动下单；请在 config.toml 填写 "
                "[okx] api_key/secret/passphrase",
                status=503,
            )
        if self._client is None:
            self._client = OKXClient(okx)
            self._broker = OkxBroker(self._client, td_mode=self._td_mode)
            # 公开行情无需鉴权；市价开仓的风控名义价取这里
            self._rest_price = RestPriceProvider(self._client)
        assert self._broker is not None
        return self._client, self._broker

    async def _instrument(self, inst_id: str) -> Instrument:
        """按 instId 精确查询产品配置（ctVal/lotSz/minSz）。

        复用 client 的进程内缓存：早期版本在这里自己按 SPOT → SWAP →
        FUTURES 试，类型不对时 OKX 返回 51001 而非空列表，导致合约查配置
        直接抛错。
        """
        client, _ = self._ensure_client()
        instrument = await asyncio.to_thread(client.get_instrument, inst_id)
        if instrument is None:
            raise ManualTradeError(f"交易所查不到产品配置：{inst_id}", status=404)
        return instrument

    async def _available(self, inst_id: str, instrument: Instrument) -> Decimal:
        """查询交易所当前可减仓数量（基础币口径）。"""
        client, _ = self._ensure_client()
        if instrument.inst_type == "SPOT":
            balance = await asyncio.to_thread(client.get_balance)
            details = balance.get("details") or []
            for item in details:
                if str(item.get("ccy", "")).upper() != instrument.base_ccy.upper():
                    continue
                for field in ("availBal", "availEq", "cashBal", "eq"):
                    value = _dec(item.get(field))
                    if value is not None:
                        return max(value, Decimal("0"))
            return Decimal("0")

        rows = await asyncio.to_thread(client.get_positions, instrument.inst_type)
        for row in rows:
            if str(row.get("instId", "")) != inst_id:
                continue
            contracts = _dec(row.get("pos")) or Decimal("0")
            if contracts == 0:
                return Decimal("0")
            return abs(contracts) * instrument.ct_val
        return Decimal("0")

    def _local_qty(self, inst_id: str) -> Decimal:
        try:
            return abs(self._store.get_position(inst_id).qty)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read local position failed inst={inst_id}: {e}")
            return Decimal("0")

    async def _lock(self, inst_id: str) -> bool:
        if self._redis is None:
            return True
        try:
            return bool(
                await self._redis.set(
                    f"quant:manual:lock:{inst_id}",
                    "1",
                    nx=True,
                    ex=LOCK_TTL_SECONDS,
                )
            )
        except Exception as e:  # noqa: BLE001
            # Redis 不可用时不阻断交易，宁可少一层保护也不要操作不了
            log.warning(f"manual lock unavailable inst={inst_id}: {e}")
            return True

    async def _unlock(self, inst_id: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(f"quant:manual:lock:{inst_id}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"manual unlock failed inst={inst_id}: {e}")

    # ------------------------------------------------------------------
    # 对外操作
    # ------------------------------------------------------------------

    async def reduce(
        self,
        inst_id: str,
        *,
        ratio: Decimal | None,
        qty: Decimal | None,
        reason: str,
    ) -> dict[str, object]:
        """按比例或指定数量减仓。

        Args:
            ratio: 0 < ratio <= 1，按「本地持仓 × ratio」下单
            qty: 基础币数量；与 ratio 二选一
        """
        if not inst_id:
            raise ManualTradeError("inst_id 不能为空")
        if not reason.strip():
            raise ManualTradeError("必须填写操作原因")
        if (ratio is None) == (qty is None):
            raise ManualTradeError("ratio 与 qty 必须且只能提供一个")
        if ratio is not None and not (Decimal("0") < ratio <= Decimal("1")):
            raise ManualTradeError("ratio 必须在 (0, 1] 区间")
        if qty is not None and qty <= 0:
            raise ManualTradeError("qty 必须为正数")

        if not await self._lock(inst_id):
            raise ManualTradeError(f"{inst_id} 有正在处理的手动操作，请稍后重试", 409)
        try:
            return await self._reduce_locked(
                inst_id, ratio=ratio, qty=qty, reason=reason
            )
        finally:
            await self._unlock(inst_id)

    async def flatten(self, *, reason: str) -> dict[str, object]:
        """一键全平：逐个品种减到 0，单项失败不影响其它。"""
        if not reason.strip():
            raise ManualTradeError("必须填写操作原因")

        targets = await self._flatten_targets()
        if not targets:
            return {
                "ok": True,
                "ts": int(time.time() * 1000),
                "results": [],
                "total": 0,
                "succeeded": 0,
                "failed": 0,
            }

        results: list[dict[str, object]] = []
        for inst_id in targets[:MAX_FLATTEN_ITEMS]:
            if not await self._lock(inst_id):
                results.append(
                    {
                        "inst_id": inst_id,
                        "ok": False,
                        "error": "该品种有正在处理的操作",
                    }
                )
                continue
            try:
                results.append(
                    await self._reduce_locked(
                        inst_id,
                        ratio=Decimal("1"),
                        qty=None,
                        reason=reason,
                        allow_full=True,
                    )
                )
            except ManualTradeError as e:
                results.append(
                    {"inst_id": inst_id, "ok": False, "error": str(e)}
                )
            except QuantError as e:
                results.append(
                    {"inst_id": inst_id, "ok": False, "error": str(e)}
                )
            finally:
                await self._unlock(inst_id)

        succeeded = sum(1 for r in results if r.get("ok"))
        return {
            "ok": succeeded == len(results),
            "ts": int(time.time() * 1000),
            "results": results,
            "total": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
        }

    async def cancel_open_orders(self) -> dict[str, object]:
        """撤销交易所侧全部挂单，并同步本地未终结订单（M7-4）。

        以**交易所的挂单列表**为准：本地漏记或状态漂移时，只撤本地记录
        会留下真正裸露在交易所的挂单。撤单失败时不动本地状态，避免页面
        显示「已撤」而实际还挂着。
        """
        _client, broker = self._ensure_client()
        try:
            canceled = await broker.cancel_all_open_orders()
        except Exception as e:  # noqa: BLE001
            raise ManualTradeError(f"撤销挂单失败：{e}", status=502) from e

        local_marked = 0
        for order in await asyncio.to_thread(self._store.get_open_orders):
            order_id = str(order.get("order_id") or "")
            if not order_id:
                continue
            await asyncio.to_thread(
                self._store.update_order_status, order_id, "canceled"
            )
            local_marked += 1
        log.warning(
            f"kill-switch canceled exchange={canceled} local_marked={local_marked}"
        )
        return {
            "exchange_canceled": canceled,
            "local_marked": local_marked,
        }

    async def place_order(
        self,
        inst_id: str,
        *,
        side: Side,
        order_type: OrderType,
        qty: Decimal,
        price: Decimal | None,
        reason: str,
    ) -> dict[str, object]:
        """手工开仓（市价/限价），M8-17。

        与自动单共用同一套风控：kill switch、品种熔断、`RiskChecker`
        五条规则；审计写 `risk_events` 与 signals/orders；状态推进走
        `save_order` / `update_order_fill` 的单向状态机。

        Args:
            qty: 基础币数量。合约在内部按 `ctVal` 折张并向下取整
            price: 限价单必填；市价单忽略
        """
        if not inst_id:
            raise ManualTradeError("inst_id 不能为空")
        if side not in ("buy", "sell"):
            raise ManualTradeError("side 必须是 buy 或 sell")
        if order_type not in ("market", "limit"):
            raise ManualTradeError("order_type 必须是 market 或 limit")
        if qty <= 0:
            raise ManualTradeError("qty 必须为正数")
        if not reason.strip():
            raise ManualTradeError("必须填写操作原因")
        limit_price: Decimal | None = None
        if order_type == "limit":
            if price is None or price <= 0:
                raise ManualTradeError("限价单必须提供正数价格")
            limit_price = price

        if not await self._lock(inst_id):
            raise ManualTradeError(f"{inst_id} 有正在处理的手动操作，请稍后重试", 409)
        try:
            return await self._place_locked(
                inst_id,
                side=side,
                order_type=order_type,
                qty=qty,
                price=limit_price,
                reason=reason,
            )
        finally:
            await self._unlock(inst_id)

    async def cancel_order(self, order_id: str, *, reason: str) -> dict[str, object]:
        """撤单（M8-17）。

        已终结的订单直接返回、不再打交易所：连点与重复提交天然幂等。
        撤单失败时先查一次远端状态——若已被别人撤掉/成交，按实际状态回报，
        不把「本来就已经撤了」当成失败。
        """
        if not order_id:
            raise ManualTradeError("order_id 不能为空")
        if not reason.strip():
            raise ManualTradeError("必须填写操作原因")

        order = await asyncio.to_thread(self._store.get_order, order_id)
        if order is None:
            raise ManualTradeError(f"订单不存在：{order_id}", 404)
        status = str(order.get("status") or "")
        if status not in self._store.OPEN_ORDER_STATUSES:
            return {
                "ok": True,
                "order_id": order_id,
                "status": status,
                "already": True,
                "ts": int(time.time() * 1000),
            }

        inst_id = str(order.get("inst_id") or "")
        if not inst_id:
            raise ManualTradeError(f"订单缺少品种信息：{order_id}", 409)
        if not await self._lock(inst_id):
            raise ManualTradeError(f"{inst_id} 有正在处理的手动操作，请稍后重试", 409)
        try:
            _, broker = self._ensure_client()
            try:
                await broker.cancel_order(inst_id, order_id)
            except Exception as e:  # noqa: BLE001
                remote = await self._remote_state(broker, inst_id, order_id)
                if remote != "canceled":
                    raise ManualTradeError(f"撤单失败：{e}", 502) from e
            await asyncio.to_thread(
                self._store.update_order_status, order_id, "canceled"
            )
            log.warning(
                f"manual cancel inst={inst_id} order={order_id} reason={reason!r}"
            )
            return {
                "ok": True,
                "order_id": order_id,
                "status": "canceled",
                "already": False,
                "ts": int(time.time() * 1000),
            }
        finally:
            await self._unlock(inst_id)

    async def amend_order(
        self,
        order_id: str,
        *,
        qty: Decimal | None,
        price: Decimal | None,
        reason: str,
    ) -> dict[str, object]:
        """改价/改量（M8-17，仅限未终结的限价单）。

        改单会改变敞口，因此与下单走同一道风控闸门；`qty` 缺省表示只改价。
        成功后回写 signals 的 qty/price——orders 表不存这两列，看板靠 JOIN。
        """
        if not order_id:
            raise ManualTradeError("order_id 不能为空")
        if not reason.strip():
            raise ManualTradeError("必须填写操作原因")
        if qty is None and price is None:
            raise ManualTradeError("qty 与 price 至少提供一个")
        if qty is not None and qty <= 0:
            raise ManualTradeError("qty 必须为正数")
        if price is not None and price <= 0:
            raise ManualTradeError("price 必须为正数")

        order = await asyncio.to_thread(self._store.get_order, order_id)
        if order is None:
            raise ManualTradeError(f"订单不存在：{order_id}", 404)
        status = str(order.get("status") or "")
        if status not in self._store.OPEN_ORDER_STATUSES:
            raise ManualTradeError(f"订单已终结（{status}），不能改单", 409)
        if str(order.get("order_type") or "") != "limit":
            raise ManualTradeError("只有限价单可以改价改量", 409)

        inst_id = str(order.get("inst_id") or "")
        if not inst_id:
            raise ManualTradeError(f"订单缺少品种信息：{order_id}", 409)
        current_qty = _dec(order.get("qty"))
        current_price = _dec(order.get("price"))
        new_qty = qty if qty is not None else current_qty
        new_price = price if price is not None else current_price
        if new_qty is None or new_qty <= 0 or new_price is None or new_price <= 0:
            raise ManualTradeError("订单缺少数量或价格，无法改单", 409)

        if not await self._lock(inst_id):
            raise ManualTradeError(f"{inst_id} 有正在处理的手动操作，请稍后重试", 409)
        try:
            instrument = await self._instrument(inst_id)
            await self._guard_new_exposure(inst_id)

            signal = Signal(
                strategy_id=str(
                    order.get("strategy_id") or MANUAL_STRATEGY_ID
                ),
                side="buy" if str(order.get("side")) == "buy" else "sell",
                order_type="limit",
                inst_id=inst_id,
                qty=new_qty,
                price=new_price,
                ts=int(order.get("created_ts") or time.time() * 1000),
                signal_id=str(order.get("signal_id") or ""),
            )
            allowed = await self._risk_gate(signal, new_price)
            if allowed < new_qty:
                new_qty = allowed
                signal = replace(signal, qty=allowed)

            submitted, contracts = await self._normalize_qty(
                inst_id, instrument, new_qty
            )
            _, broker = self._ensure_client()
            await broker.amend_order(
                inst_id,
                order_id,
                # qty 缺省时只改价，不能顺手把 size 也发过去
                new_size=submitted if qty is not None else None,
                new_price=price,
            )
            signal_id = str(order.get("signal_id") or "")
            if signal_id:
                await asyncio.to_thread(
                    self._store.update_signal_order,
                    signal_id,
                    submitted,
                    new_price,
                )
            log.warning(
                f"manual amend inst={inst_id} order={order_id} "
                f"qty={submitted} price={new_price} reason={reason!r}"
            )
            return {
                "ok": True,
                "order_id": order_id,
                "inst_id": inst_id,
                "status": status,
                "submitted_qty": str(submitted),
                "contracts": str(contracts) if contracts is not None else None,
                "price": str(new_price),
                "adjusted": new_qty != (qty if qty is not None else new_qty),
                "ts": int(time.time() * 1000),
            }
        finally:
            await self._unlock(inst_id)

    async def _flatten_targets(self) -> list[str]:
        """待平品种：本地持仓 ∪ 快照合约多头。

        现货只认本地记账：快照里的现货是账户全部余额（可能含 ETH/OKB 等
        与策略无关的资产），直接平会连带卖掉它们。
        """
        targets: list[str] = []
        try:
            for pos in await asyncio.to_thread(self._store.get_all_positions):
                if pos.qty > 0:
                    targets.append(pos.inst_id)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read local positions failed: {e}")

        for inst_id in await self._snapshot_longs():
            if not _is_contract(inst_id):
                continue
            if inst_id not in targets:
                targets.append(inst_id)
        return targets

    async def _snapshot_longs(self) -> list[str]:
        """monitor 快照里的多头品种。

        本地持仓可能落后于交易所（例如策略刚成交、或 worker 重启前没落库），
        所以合约全平还要参考快照，避免漏平。现货由调用方过滤。
        """
        if self._redis is None:
            return []
        try:
            raw = await self._redis.get(self._settings.monitor.dashboard_snapshot_key)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read snapshot failed: {e}")
            return []
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        longs: list[str] = []
        for item in payload.get("positions") or []:
            if not isinstance(item, dict):
                continue
            inst_id = str(item.get("inst_id") or "")
            qty = _dec(item.get("qty"))
            if inst_id and qty is not None and qty > 0:
                longs.append(inst_id)
        return longs

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _place_locked(
        self,
        inst_id: str,
        *,
        side: Side,
        order_type: OrderType,
        qty: Decimal,
        price: Decimal | None,
        reason: str,
    ) -> dict[str, object]:
        instrument = await self._instrument(inst_id)
        await self._guard_new_exposure(inst_id)

        signal = Signal(
            strategy_id=MANUAL_STRATEGY_ID,
            side=side,
            order_type=order_type,
            inst_id=inst_id,
            qty=qty,
            price=price,
            ts=int(time.time() * 1000),
            signal_id=f"manual-{uuid.uuid4().hex[:20]}",
        )
        # 名义价值按「限价单用委托价、市价单用最新成交价」计算；
        # 取不到价就不允许开仓，否则 RiskChecker 会跳过全部名义类规则
        risk_price = (
            price if price is not None else await self._market_price(inst_id)
        )
        allowed = await self._risk_gate(signal, risk_price)
        if allowed != qty:
            signal = replace(signal, qty=allowed)

        submitted, contracts = await self._normalize_qty(
            inst_id, instrument, allowed
        )
        _, broker = self._ensure_client()
        result = await broker.execute(signal, submitted)
        await self._persist(signal, result)

        log.warning(
            f"manual order inst={inst_id} side={side} type={order_type} "
            f"qty={submitted} price={price} reason={reason!r} "
            f"status={result.order.status} order={result.order.order_id}"
        )
        fill = result.fill
        return {
            "inst_id": inst_id,
            "inst_type": instrument.inst_type,
            "side": side,
            "order_type": order_type,
            "ok": True,
            "ts": int(time.time() * 1000),
            "requested_qty": str(qty),
            "submitted_qty": str(submitted),
            "contracts": str(contracts) if contracts is not None else None,
            "price": str(price) if price is not None else None,
            "adjusted": allowed != qty,
            "order_id": result.order.order_id,
            "status": result.order.status,
            "fill": (
                {
                    "qty": str(fill.qty),
                    "price": str(fill.price),
                    "fee": str(fill.fee),
                    "ts": fill.ts,
                }
                if fill is not None
                else None
            ),
            "error": None,
        }

    async def _guard_new_exposure(self, inst_id: str) -> None:
        """开新敞口前的全局闸门：kill switch + 品种熔断。

        Redis 读不出去时**拒绝**：无法确认是否已紧急停止/open，就不该
        打开新仓位。减仓/平仓不走这道闸门（它们只会降低敞口）。
        """
        if self._redis is None:
            return
        try:
            halted = await KillSwitch(self._redis).is_active()
        except Exception as e:  # noqa: BLE001
            raise ManualTradeError(
                f"无法确认紧急停止状态：{e}", status=503
            ) from e
        if halted:
            raise ManualTradeError(
                "紧急停止（kill switch）已激活，不能手工开仓", status=409
            )
        try:
            state = await CircuitBreaker(self._redis).state(inst_id)
        except Exception as e:  # noqa: BLE001
            raise ManualTradeError(f"无法确认熔断状态：{e}", status=503) from e
        if state is not None:
            raise ManualTradeError(
                f"{inst_id} 熔断中：{state.reason}", status=409
            )

    async def _risk_gate(
        self, signal: Signal, market_price: Decimal | None
    ) -> Decimal:
        """跑一遍与自动单相同的风控规则，返回允许的下单量。

        Raises:
            ManualTradeError: 拒单（409）或取不到风控基数（503）。
        """
        if market_price is None or market_price <= 0:
            raise ManualTradeError(
                f"{signal.inst_id} 取不到行情价，无法做名义价值风控，已拒绝",
                status=503,
            )
        equity = await self._equity()

        # 手动单没有策略归属，只取 [risk] 全局层；策略级覆盖不参与
        limits = resolve_limits(MANUAL_STRATEGY_ID, self._settings.risk)
        checker = RiskChecker(
            RiskConfig(
                strategy_id=MANUAL_STRATEGY_ID,
                **{name: limits.value(name) for name in LIMIT_FIELDS},
            ),
            self._store,
            allow_initial_equity_fallback=False,
            event_log=self._settings.risk.event_log,
        )
        now = int(time.time() * 1000)
        decision = await asyncio.to_thread(
            checker.check,
            signal,
            market_price,
            day_start_ms_utc8(now),
            equity=equity,
        )
        await asyncio.to_thread(
            checker.record_event, decision, signal, now_ms=now, equity=equity
        )
        if not decision.approved:
            log.warning(
                f"manual order rejected inst={signal.inst_id} "
                f"rule={decision.rule} reason={decision.reason}"
            )
            raise ManualTradeError(f"风控拒单：{decision.reason}", status=409)
        if decision.halt:
            # 硬性上限只拒这一笔；停自动交易是 worker 的职责，
            # 手动操作不应顺带把全局开关打开
            log.error(
                f"manual order hit hard risk limit: {decision.reason}"
            )
        return decision.adjusted_qty

    async def _equity(self) -> Decimal:
        """风控基数：monitor 快照的总权益，缺失或超龄直接拒单。"""
        if self._redis is None:
            raise ManualTradeError(
                "Redis 不可用，读不到账户快照，拒绝手工开仓", status=503
            )
        value = await read_account_field(
            self._redis,
            self._settings.monitor.dashboard_snapshot_key,
            "total_equity",
            now_ms=int(time.time() * 1000),
            max_age_ms=self._settings.risk.snapshot_max_age_ms,
        )
        if value is None:
            raise ManualTradeError(
                "账户快照不可用或已过期，无法校验风控；请确认 monitor 在线",
                status=503,
            )
        return value

    async def _market_price(self, inst_id: str) -> Decimal | None:
        client, _ = self._ensure_client()
        provider = self._rest_price or RestPriceProvider(client)
        return await asyncio.to_thread(provider, inst_id)

    async def _normalize_qty(
        self, inst_id: str, instrument: Instrument, qty: Decimal
    ) -> tuple[Decimal, Decimal | None]:
        """按 `lotSz` 向下取整并校验 `minSz`，返回 (基础币量, 张数)。"""
        if instrument.inst_type == "SPOT":
            submitted = floor_to(qty, instrument.lot_sz)
            ok_size = submitted > 0 and submitted >= instrument.min_sz
            return_contracts: Decimal | None = None
        else:
            raw_contracts = (
                qty / instrument.ct_val if instrument.ct_val > 0 else qty
            )
            return_contracts = floor_to(raw_contracts, instrument.lot_sz)
            submitted = return_contracts * instrument.ct_val
            # minSz 的单位是张，不是基础币
            ok_size = (
                return_contracts > 0
                and return_contracts >= instrument.min_sz
            )
        if not ok_size:
            raise ManualTradeError(
                f"{inst_id} 可下单量 "
                f"{return_contracts if return_contracts is not None else submitted} "
                f"低于最小下单量 {instrument.min_sz}（lotSz={instrument.lot_sz}）"
            )
        return submitted, return_contracts

    async def _persist(self, signal: Signal, result) -> None:
        """落库：signal → order → 成交累加 → 持仓。

        顺序与 worker 一致（先 signal 再 order，orders 靠 JOIN 取上下文）；
        成交量按增量累加、状态走单向状态机，重复落库不会把已成交打回 pending。
        """
        await asyncio.to_thread(self._store.save_signal, signal, "executed")
        await asyncio.to_thread(self._store.save_order, result.order)
        fill = result.fill
        if fill is None:
            return
        await asyncio.to_thread(
            self._store.update_order_fill,
            result.order.order_id,
            fill,
            result.order.status,
        )
        await asyncio.to_thread(self._store.save_fill, fill)
        position = await asyncio.to_thread(
            self._store.get_position, fill.inst_id
        )
        position.apply_fill(fill)
        await asyncio.to_thread(self._store.update_position, position)

    async def _remote_state(
        self, broker: OkxBroker, inst_id: str, order_id: str
    ) -> str:
        try:
            detail = await broker.get_order(inst_id, order_id)
        except Exception as e:  # noqa: BLE001
            log.warning(
                f"query order state failed inst={inst_id} order={order_id}: {e}"
            )
            return ""
        return str((detail or {}).get("state") or "")

    async def _reduce_locked(
        self,
        inst_id: str,
        *,
        ratio: Decimal | None,
        qty: Decimal | None,
        reason: str,
        allow_full: bool = False,
    ) -> dict[str, object]:
        instrument = await self._instrument(inst_id)
        available = await self._available(inst_id, instrument)
        if available <= 0:
            raise ManualTradeError(f"{inst_id} 交易所可用持仓为 0，无需减仓")

        if ratio is not None:
            if allow_full:
                # 全平以交易所可用量为准：本地记录可能落后或偏小，
                # 按本地数量下单会在交易所留下残余仓位
                base_qty = available
            else:
                local = self._local_qty(inst_id)
                if local <= 0:
                    raise ManualTradeError(
                        f"{inst_id} 本地无持仓记录，无法按比例减仓"
                    )
                base_qty = local * ratio
        else:
            assert qty is not None
            base_qty = qty

        target = min(base_qty, available)

        contracts: Decimal | None = None
        if instrument.inst_type == "SPOT":
            submitted = floor_to(target, instrument.lot_sz)
            ok_size = submitted > 0 and submitted >= instrument.min_sz
        else:
            # 合约 sz 是张数：基础币 / 面值 再按 lotSz 取整
            raw_contracts = (
                target / instrument.ct_val
                if instrument.ct_val > 0
                else target
            )
            contracts = floor_to(raw_contracts, instrument.lot_sz)
            submitted = contracts * instrument.ct_val
            # minSz 的单位是张，不是基础币
            ok_size = contracts > 0 and contracts >= instrument.min_sz

        if not ok_size:
            raise ManualTradeError(
                f"{inst_id} 可下单量 {contracts if contracts is not None else submitted} "
                f"低于最小下单量 {instrument.min_sz}（lotSz={instrument.lot_sz}）"
            )

        signal = Signal(
            strategy_id="manual",
            side="sell",
            order_type="market",
            inst_id=inst_id,
            # 全链路基础币口径：合约的 sz/张数换算由 broker 负责
            qty=submitted,
            ts=int(time.time() * 1000),
            signal_id=f"manual-{uuid.uuid4().hex[:20]}",
        )

        _, broker = self._ensure_client()
        result = await broker.execute(
            signal,
            submitted,
            reduce_only=instrument.inst_type != "SPOT",
        )

        # 先落 signal 再落 order：orders 只有 signal_id，看板的品种/方向/
        # 类型/数量都靠 JOIN signals 取回，缺这行就会显示为空
        await asyncio.to_thread(self._store.save_signal, signal, "executed")
        await asyncio.to_thread(self._store.save_order, result.order)
        fill = result.fill
        if fill is not None:
            # broker 已把合约张数折成基础币，这里直接用
            normalized = Fill(
                order_id=fill.order_id,
                inst_id=inst_id,
                side="sell",
                qty=fill.qty,
                price=fill.price,
                fee=fill.fee,
                ts=fill.ts,
            )
            await asyncio.to_thread(
                self._store.update_order_fill, result.order.order_id, normalized
            )
            await asyncio.to_thread(self._store.save_fill, normalized)
            position = await asyncio.to_thread(self._store.get_position, inst_id)
            # 交易所成交量可能大于本地记录（本地落后于交易所）：只结平已知
            # 持仓，否则 apply_fill 会把本地持仓翻成反向空头；
            # 本地完全没有记录时保持空仓，不凭空建仓
            closable = position.qty if position.qty > 0 else Decimal("0")
            if closable > 0:
                applied = (
                    replace(normalized, qty=closable)
                    if normalized.qty > closable
                    else normalized
                )
                position.apply_fill(applied)
                await asyncio.to_thread(self._store.update_position, position)

        log.warning(
            f"manual reduce inst={inst_id} reason={reason!r} "
            f"submitted={submitted} status={result.order.status} "
            f"order={result.order.order_id}"
        )

        return {
            "inst_id": inst_id,
            "inst_type": instrument.inst_type,
            "ok": True,
            "ts": int(time.time() * 1000),
            "available_qty": str(available),
            "submitted_qty": str(submitted),
            "contracts": str(contracts) if contracts is not None else None,
            "order_id": result.order.order_id,
            "status": result.order.status,
            "fill": (
                {
                    "qty": str(fill.qty),
                    "price": str(fill.price),
                    "fee": str(fill.fee),
                    "ts": fill.ts,
                }
                if fill is not None
                else None
            ),
            "error": None,
        }
