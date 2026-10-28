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
from quant.foundation.errors import OrderError
from quant.foundation.logger import get_logger
from quant.okx.client import OKXClient

log = get_logger("execution.okx_broker")

# 已成交状态
_FILLED_STATES = ("filled",)
# 未终结状态（可轮询）
_OPEN_STATES = ("live", "partially_filled")
# OKX 批量撤单单次上限
_BATCH_CANCEL_LIMIT = 20


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
    """从 OKX 订单详情构造 Fill（仅当已成交且信息完整时）。

    inst_id/ord_id 由调用方保证（查询即以其为入参）；side 从详情读取。
    合约的 `accFillSz` 是张数，乘 `ct_val` 换成基础币后再落库——本地持仓、
    风控、看板统一用基础币口径。现货手续费若以基础币收取，成交量会扣掉
    手续费、费用折成计价币，保证 Fill.qty 与账户余额变动一致
    （见 `_normalize_spot_fee`）。
    """
    state = str(detail.get("state", ""))
    acc_fill_sz = Decimal(str(detail.get("accFillSz") or "0"))
    avg_px = Decimal(str(detail.get("avgPx") or "0"))
    side = str(detail.get("side", ""))
    if state not in _FILLED_STATES or acc_fill_sz <= 0 or avg_px <= 0:
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
        resp = await asyncio.to_thread(
            self._client.post, "/api/v5/trade/order", body
        )
        if not isinstance(resp, list) or not resp:
            raise OrderError("-1", f"empty order response: {resp!r}")

        row = resp[0]
        s_code = str(row.get("sCode", "0"))
        if s_code != "0":
            raise OrderError(s_code, str(row.get("sMsg", "order rejected")))

        ord_id = str(row.get("ordId", ""))
        if not ord_id:
            raise OrderError("-1", f"missing ordId in response: {row!r}")

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
                status="filled",
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
            status="pending",
            created_ts=created_ts,
        )
        return ExecutionResult(order=order, fill=None)

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
