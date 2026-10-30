"""OKX 模拟盘/实盘执行器（M3）。

通过 REST 下单并回查订单状态：
1. POST /api/v5/trade/order
2. GET  /api/v5/trade/order?instId=&ordId=  ← 拿成交价/成交量/手续费

模拟盘与实盘唯一区别是 OKXClient 的 `demo` 标志
（设置 `x-simulated-trading: 1` 头），本类不需要感知。
"""
from __future__ import annotations

import asyncio
import re
import time
from decimal import ROUND_DOWN, Decimal

from quant.backtest.order import Fill, Order
from quant.backtest.signal import Signal
from quant.execution.broker import ExecutionResult
from quant.foundation.errors import NetworkError, OrderError
from quant.foundation.logger import get_logger
from quant.okx.client import OKXClient

log = get_logger("execution.okx_broker")

# 已成交状态
_FILLED_STATES = ("filled",)
# 有成交量的非终结状态：需要落增量成交（MS4-B §7.3）
_PARTIAL_STATES = ("partially_filled",)
# 未终结状态（可轮询）
_OPEN_STATES = ("live", "partially_filled")
# OKX 批量撤单单次上限
_BATCH_CANCEL_LIMIT = 20
# 挂单列表接口单页上限
_PENDING_PAGE_LIMIT = 100
# 重复 clOrdId（交易所侧幂等命中）
_DUPLICATED_CL_ORD_ID = "51016"
# 下单提交重试上限（MS4-B §9.2）
_SUBMIT_MAX_ATTEMPTS = 3


def _fmt(value: Decimal) -> str:
    """Decimal → 非科学计数法字符串（OKX 要求）。"""
    return format(value.normalize(), "f")


def _is_contract(inst_id: str) -> bool:
    """是否合约品种：BTC-USDT-SWAP / BTC-USD-251226 至少两个连字符。"""
    return inst_id.count("-") >= 2


def _floor_to(value: Decimal, step: Decimal) -> Decimal:
    """按步长向下取整（OKX 的 lotSz 要求）。"""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _cl_ord_id(signal: Signal) -> str:
    """OKX clOrdId：≤32 位字母数字。"""
    raw = signal.signal_id or ""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw)
    if cleaned:
        return cleaned[:32]
    return f"q{int(time.time() * 1000)}"


def _normalize_spot_fee(
    inst_id: str,
    side: str,
    qty: Decimal,
    price: Decimal,
    fee: Decimal,
    fee_ccy: str,
) -> tuple[Decimal, Decimal]:
    """现货手续费换算成「净成交数量 + 计价币费用」。

    OKX 现货买入手续费按**收到的币**（基础币）扣除，例如买 0.001 BTC 收
    0.000001 BTC。此时实际入账是 `accFillSz - fee`，费用要按成交价折成
    计价币记入盈亏，否则本地持仓会永久多于交易所余额，全平卖出会因可用
    余额不足被拒。
    卖出手续费通常已是计价币，只做币种校验；万一以基础币收取，同样折成
    计价币记费用（数量不再调整，feeCcy 为基础币的卖单属极少数情况）。
    合约的 sz 是张数、feeCcy 是结算币，不做换算。
    """
    if inst_id.count("-") != 1:
        return qty, fee
    base_ccy = inst_id.split("-")[0].upper()
    if fee <= 0 or fee_ccy.upper() != base_ccy:
        return qty, fee
    if side == "buy":
        net = qty - fee
        if net <= 0:
            return qty, fee
        return net, fee * price
    return qty, fee * price


def detail_to_fill(
    inst_id: str,
    ord_id: str,
    detail: dict,
    fallback_ts: int = 0,
    ct_val: Decimal = Decimal("1"),
) -> Fill | None:
    """从 OKX 订单详情构造 Fill（有成交量且信息完整时）。

    inst_id/ord_id 由调用方保证（查询即以其为入参）；side 从详情读取。
    合约的 `accFillSz` 是张数，乘 `ct_val` 换成基础币后再落库——本地持仓、
    风控、看板统一用基础币口径。现货手续费若以基础币收取，成交量会扣掉
    手续费、费用折成计价币，保证 Fill.qty 与账户余额变动一致
    （见 `_normalize_spot_fee`）。

    注意：返回的 `qty`/`fee` 是**该订单的累计值**（OKX 的 `accFillSz`
    语义），不是本次增量。部分成交场景下调用方必须自行与已落库的量相减。

    `partially_filled` 也返回 Fill：只认 `filled` 会把部分成交整段丢掉，
    本地成交量与持仓都会偏小。
    """
    state = str(detail.get("state", ""))
    acc_fill_sz = Decimal(str(detail.get("accFillSz") or "0"))
    avg_px = Decimal(str(detail.get("avgPx") or "0"))
    side = str(detail.get("side", ""))
    if state not in _FILLED_STATES + _PARTIAL_STATES:
        return None
    if acc_fill_sz <= 0 or avg_px <= 0:
        return None
    if side not in ("buy", "sell"):
        return None
    qty = acc_fill_sz * ct_val
    fee = abs(Decimal(str(detail.get("fee") or "0")))
    qty, fee = _normalize_spot_fee(
        inst_id,
        side,
        qty,
        avg_px,
        fee,
        str(detail.get("feeCcy") or ""),
    )
    ts = int(detail.get("fillTime") or detail.get("uTime") or fallback_ts)
    return Fill(
        order_id=ord_id,
        inst_id=inst_id,
        side=side,
        qty=qty,
        price=avg_px,
        fee=fee,
        ts=ts,
    )


class OkxBroker:
    """OKX 下单执行器。"""

    def __init__(
        self,
        client: OKXClient,
        td_mode: str = "cash",
        poll_attempts: int = 5,
        poll_interval: float = 0.2,
    ) -> None:
        if poll_attempts < 1:
            raise ValueError("poll_attempts must be >= 1")
        if poll_interval < 0:
            raise ValueError("poll_interval must be >= 0")
        self._client = client
        self._td_mode = td_mode
        self._poll_attempts = poll_attempts
        self._poll_interval = poll_interval

    async def ct_val(self, inst_id: str) -> Decimal:
        """合约面值（基础币 ↔ 张数换算系数）；现货返回 1。

        合约查不到面值时抛 `OrderError`，不做「按 1 处理」的降级——那会让
        0.01 BTC 被当成 0.01 张下单，数量相差 ctVal 倍。宁可拒单。
        """
        if not _is_contract(inst_id):
            return Decimal("1")
        instrument = await asyncio.to_thread(
            self._client.get_instrument, inst_id
        )
        if instrument is None or instrument.ct_val <= 0:
            raise OrderError(
                "-1", f"查不到合约面值 ctVal：{inst_id}，拒绝按不确定的面值下单"
            )
        return instrument.ct_val

    async def execute(
        self,
        signal: Signal,
        qty: Decimal,
        market_price: Decimal | None = None,
        reduce_only: bool = False,
    ) -> ExecutionResult:
        """提交订单并尽可能返回成交回报。

        Args:
            qty: 下单数量，**基础币**口径。合约在提交前按 ctVal 折成张数，
                成交回报再折回基础币，本地记账全程只见基础币。
            reduce_only: 仅减仓（合约专用）。看板手动平仓必须置 True，
                否则仓位方向算错时会开出反向新仓。
        """
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")

        ct_val = await self.ct_val(signal.inst_id)
        sz = qty
        if ct_val != 1:
            instrument = await asyncio.to_thread(
                self._client.get_instrument, signal.inst_id
            )
            assert instrument is not None  # ct_val() 已校验存在
            sz = _floor_to(qty / ct_val, instrument.lot_sz)
            if sz <= 0:
                raise OrderError(
                    "-1",
                    f"{signal.inst_id} {qty} 折合张数为 0（ctVal={ct_val}，"
                    f"lotSz={instrument.lot_sz}）",
                )

        body: dict[str, str] = {
            "instId": signal.inst_id,
            # 现货只接受 cash；配置的 cross/isolated 只对合约生效
            "tdMode": self._td_mode if _is_contract(signal.inst_id) else "cash",
            "side": signal.side,
            "ordType": signal.order_type,
            "sz": _fmt(sz),
            "clOrdId": _cl_ord_id(signal),
        }
        if reduce_only:
            body["reduceOnly"] = "true"
        if signal.order_type == "limit":
            if signal.price is None:
                raise ValueError("limit order requires price")
            body["px"] = _fmt(signal.price)
        elif signal.order_type == "market" and not _is_contract(signal.inst_id):
            # OKX 现货市价单必须指定 tgtCcy，否则 sz 会被当作计价币（USDT）
            # 解释，导致 51020「最小金额不足」。这里 qty 是基础币数量。
            # 合约的 sz 恒为张数，不接受 tgtCcy。
            body["tgtCcy"] = "base_ccy"

        created_ts = int(time.time() * 1000)
        ord_id = await self._submit_with_idempotency(signal, body)

        log.info(
            f"order submitted inst={signal.inst_id} side={signal.side} "
            f"qty={qty} ordId={ord_id}"
        )

        detail = await self._poll_order(signal.inst_id, ord_id)
        state = str(detail.get("state", ""))

        fill = detail_to_fill(
            signal.inst_id,
            ord_id,
            detail,
            fallback_ts=created_ts,
            ct_val=ct_val,
        )
        if fill is not None:
            order = Order(
                order_id=ord_id,
                signal=signal,
                status="filled" if state in _FILLED_STATES else "partially_filled",
                created_ts=created_ts,
            )
            return ExecutionResult(order=order, fill=fill)

        if state in ("canceled", "mmp_canceled"):
            order = Order(
                order_id=ord_id,
                signal=signal,
                status="canceled",
                created_ts=created_ts,
            )
            return ExecutionResult(order=order, fill=None)

        order = Order(
            order_id=ord_id,
            signal=signal,
            # 交易所已受理进挂单簿时记为 live，未查到状态时保持 pending
            status="live" if state == "live" else "pending",
            created_ts=created_ts,
        )
        return ExecutionResult(order=order, fill=None)

    async def _submit_with_idempotency(self, signal: Signal, body: dict) -> str:
        """提交订单并返回 ordId；超时/重复 clOrdId 时先查后重试（MS4-B §9）。

        三条约束：
        1. 超时后**禁止直接重发**，必须先按 `clOrdId` 查是否已落地；
        2. 重试上限 3 次；
        3. `clOrdId` 由 `signal_id` 稳定派生，重试几次都是同一字符串——
           这是幂等能生效的前提。
        """
        cl_ord_id = str(body.get("clOrdId", ""))
        inst_id = str(body.get("instId", ""))
        last_exc: Exception | None = None

        for attempt in range(_SUBMIT_MAX_ATTEMPTS):
            try:
                resp = await asyncio.to_thread(
                    self._client.post, "/api/v5/trade/order", body
                )
            except NetworkError as e:
                last_exc = e
                existing = await self._find_by_cl_ord_id(inst_id, cl_ord_id)
                if existing:
                    log.warning(
                        f"submit timeout but order exists: "
                        f"{existing.get('ordId')} clOrdId={cl_ord_id}"
                    )
                    return str(existing["ordId"])
                if attempt == _SUBMIT_MAX_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))
                continue

            if not isinstance(resp, list) or not resp:
                raise OrderError("-1", f"empty order response: {resp!r}")

            row = resp[0]
            s_code = str(row.get("sCode", "0"))
            if s_code == _DUPLICATED_CL_ORD_ID:
                # 交易所侧已有同 clOrdId 订单：视为已提交，转为查询
                existing = await self._find_by_cl_ord_id(inst_id, cl_ord_id)
                if existing:
                    log.warning(
                        f"duplicated clOrdId, reuse order "
                        f"{existing.get('ordId')} clOrdId={cl_ord_id}"
                    )
                    return str(existing["ordId"])
                raise OrderError(s_code, "duplicated clOrdId but order not found")
            if s_code != "0":
                raise OrderError(s_code, str(row.get("sMsg", "order rejected")))

            ord_id = str(row.get("ordId", ""))
            if not ord_id:
                raise OrderError("-1", f"missing ordId in response: {row!r}")
            return ord_id

        raise last_exc or OrderError("-1", "order submit failed")

    async def _find_by_cl_ord_id(
        self, inst_id: str, cl_ord_id: str
    ) -> dict | None:
        """按 clOrdId 查订单，查询本身失败时返回 None（不能掩盖原始错误）。"""
        if not inst_id or not cl_ord_id:
            return None
        try:
            found = await self.get_order_by_cl_ord_id(inst_id, cl_ord_id)
        except Exception as e:  # noqa: BLE001
            log.warning(f"query order by clOrdId failed: {e}")
            return None
        return found or None

    async def _poll_order(self, inst_id: str, ord_id: str) -> dict:
        """回查订单；未终结状态按配置轮询。"""
        detail: dict = {}
        for attempt in range(self._poll_attempts):
            data = await asyncio.to_thread(
                self._client.get,
                "/api/v5/trade/order",
                {"instId": inst_id, "ordId": ord_id},
            )
            if isinstance(data, list) and data:
                detail = data[0]
                if str(detail.get("state", "")) not in _OPEN_STATES:
                    return detail
            if attempt < self._poll_attempts - 1:
                await asyncio.sleep(self._poll_interval)
        return detail

    async def get_order(self, inst_id: str, ord_id: str) -> dict:
        """单次查询订单详情（用于对账，不轮询）。"""
        data = await asyncio.to_thread(
            self._client.get,
            "/api/v5/trade/order",
            {"instId": inst_id, "ordId": ord_id},
        )
        if isinstance(data, list) and data:
            return data[0]
        return {}

    async def get_order_by_cl_ord_id(self, inst_id: str, cl_ord_id: str) -> dict:
        """按 clOrdId 查订单（幂等重试与恢复用）。

        注意两点：接口是复数的 `/api/v5/trade/orders`，且**必须显式带
        `state=all`**——该接口默认只返回未完结订单，否则「已成交」会被
        查成空字典，超时重试就会真的再下一次单。
        """
        data = await asyncio.to_thread(
            self._client.get,
            "/api/v5/trade/orders",
            {"instId": inst_id, "clOrdId": cl_ord_id, "state": "all"},
        )
        if isinstance(data, list) and data:
            return data[0]
        return {}

    async def list_pending_orders(self, inst_type: str | None = None) -> list[dict]:
        """列出交易所侧全部未完结订单（`GET /trade/orders-pending`）。

        单页最多 100 条，按 `after`（上一页最后一条 ordId）向前翻页。
        游标不前进或出现重复页时立即退出，避免接口异常导致死循环。
        """
        results: list[dict] = []
        after: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, str] = {"limit": str(_PENDING_PAGE_LIMIT)}
            if inst_type:
                params["instType"] = inst_type
            if after:
                params["after"] = after
            data = await asyncio.to_thread(
                self._client.get, "/api/v5/trade/orders-pending", params
            )
            if not isinstance(data, list) or not data:
                break
            results.extend(data)
            last = str(data[-1].get("ordId", ""))
            if len(data) < _PENDING_PAGE_LIMIT or not last or last in seen:
                break
            seen.add(last)
            after = last
        return results

    async def cancel_all_open_orders(self, inst_type: str | None = None) -> int:
        """撤销交易所侧全部挂单，返回**成功撤掉的条数**（MS4-B §6.4）。

        以交易所的挂单列表为准，而不是本地 `orders` 表：本地漏记或状态
        漂移时，只撤本地记录会留下真正的裸露挂单。撤单复用批量接口
        （每批 20），不逐单撤；批内已成交/已撤的单会返回非 0 码，视为
        已处理。
        """
        pending = await self.list_pending_orders(inst_type)
        pairs = [
            (str(o.get("instId", "")), str(o.get("ordId", "")))
            for o in pending
            if o.get("instId") and o.get("ordId")
        ]
        if not pairs:
            return 0
        results = await self.cancel_orders(pairs)
        canceled = sum(1 for code in results.values() if code == "0")
        log.warning(
            f"cancel-all: pending={len(pairs)} canceled={canceled}"
        )
        return canceled

    async def cancel_order(self, inst_id: str, ord_id: str) -> dict:
        """撤单：POST /api/v5/trade/cancel-order。"""
        body = {"instId": inst_id, "ordId": ord_id}
        resp = await asyncio.to_thread(
            self._client.post, "/api/v5/trade/cancel-order", body
        )
        if not isinstance(resp, list) or not resp:
            raise OrderError("-1", f"empty cancel response: {resp!r}")
        row = resp[0]
        s_code = str(row.get("sCode", "0"))
        if s_code != "0":
            raise OrderError(s_code, str(row.get("sMsg", "cancel rejected")))
        log.info(f"order canceled inst={inst_id} ordId={ord_id}")
        return row

    async def cancel_orders(
        self, orders: list[tuple[str, str]]
    ) -> dict[str, str]:
        """批量撤单：POST /api/v5/trade/cancel-batch-orders（每批最多 20）。

        Args:
            orders: [(inst_id, ord_id), ...]

        Returns:
            {ord_id: sCode}；"0" 表示撤单成功，其余为失败码。
            请求整体失败时该批全部记为 "-1"。
        """
        if not orders:
            return {}

        results: dict[str, str] = {}
        for start in range(0, len(orders), _BATCH_CANCEL_LIMIT):
            chunk = orders[start : start + _BATCH_CANCEL_LIMIT]
            body = [
                {"instId": inst_id, "ordId": ord_id}
                for inst_id, ord_id in chunk
            ]
            resp = await asyncio.to_thread(
                self._client.post, "/api/v5/trade/cancel-batch-orders", body
            )
            if not isinstance(resp, list):
                log.warning(f"batch cancel empty response: {resp!r}")
                for _inst_id, ord_id in chunk:
                    results[ord_id] = "-1"
                continue

            for row in resp:
                ord_id = str(row.get("ordId", ""))
                if not ord_id:
                    continue
                results[ord_id] = str(row.get("sCode", "-1"))
            # 响应缺项（异常情况）按失败处理，避免本地误判已撤
            for _inst_id, ord_id in chunk:
                results.setdefault(ord_id, "-1")

        canceled = sum(1 for code in results.values() if code == "0")
        log.info(f"batch cancel requested={len(orders)} canceled={canceled}")
        return results

    async def amend_order(
        self,
        inst_id: str,
        ord_id: str,
        new_size: Decimal | None = None,
        new_price: Decimal | None = None,
    ) -> dict:
        """改单：POST /api/v5/trade/amend-order。

        至少提供一个 new_size（**基础币**数量）或 new_price（限价价）。
        """
        if new_size is None and new_price is None:
            raise ValueError("amend order requires new_size or new_price")
        body: dict[str, str] = {"instId": inst_id, "ordId": ord_id}
        if new_size is not None:
            ct_val = await self.ct_val(inst_id)
            sz = new_size
            if ct_val != 1:
                instrument = await asyncio.to_thread(
                    self._client.get_instrument, inst_id
                )
                assert instrument is not None
                sz = _floor_to(new_size / ct_val, instrument.lot_sz)
                if sz <= 0:
                    raise OrderError(
                        "-1", f"{inst_id} {new_size} 折合张数为 0"
                    )
            body["newSz"] = _fmt(sz)
        if new_price is not None:
            body["newPx"] = _fmt(new_price)
        resp = await asyncio.to_thread(
            self._client.post, "/api/v5/trade/amend-order", body
        )
        if not isinstance(resp, list) or not resp:
            raise OrderError("-1", f"empty amend response: {resp!r}")
        row = resp[0]
        s_code = str(row.get("sCode", "0"))
        if s_code != "0":
            raise OrderError(s_code, str(row.get("sMsg", "amend rejected")))
        log.info(f"order amended inst={inst_id} ordId={ord_id}")
        return row


__all__ = ["OkxBroker", "detail_to_fill"]
