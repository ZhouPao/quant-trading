"""下单前预检（M7-7）。

风控 checker 回答「该不该下这么多」（策略/仓位/日亏损），预检回答
「这一单交易所收不收」：产品配置是否存在、数量是否符合 lotSz 与 minSz、
名义金额是否过小、账户余额是否够。

两者职责分开的原因：风控参数是可调的、人为的；预检是交易所的硬约束，
不随策略变化。预检失败直接拒单，不进入 broker——避免把必然被拒的单
打到交易所（既浪费限频额度，也把噪音写进订单流水）。

预检**不触达下单接口**，只读产品配置与账户余额；余额读取失败时放行并
记 WARNING（预检是保护不是闸门，网络抖动不该阻断全部交易）。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from quant.backtest.signal import Signal
from quant.foundation.logger import get_logger
from quant.models.instrument import Instrument

log = get_logger("risk.preflight")

#: 余额缓存秒数：每个信号都查一次余额会吃满限频，且余额变化远慢于信号
DEFAULT_BALANCE_CACHE_SECONDS = 15.0
#: 余额安全垫：留出 2% 应对手续费与滑点，避免卡在「刚好够」被交易所拒
DEFAULT_BALANCE_BUFFER = Decimal("0.02")

RULE_INSTRUMENT = "preflight_instrument"
RULE_LOT_SIZE = "preflight_lot_size"
RULE_MIN_SIZE = "preflight_min_size"
RULE_MIN_NOTIONAL = "preflight_min_notional"
RULE_BALANCE = "preflight_balance"


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    """按步长向下取整（OKX 的 lotSz 要求）。"""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True, slots=True)
class PreflightDecision:
    """预检结果。

    `qty` 是按 lotSz 归一化后的**基础币**数量，调用方应使用它下单，
    而不是原始信号数量——否则 broker 会静默取整，本地记录与实际成交不符。
    """

    ok: bool
    qty: Decimal
    reason: str = ""
    rule: str | None = None
    limit_value: Decimal | None = None
    actual_value: Decimal | None = None
    balance_checked: bool = False


class PreflightChecker:
    """下单前预检器。

    Args:
        client: 需提供 `get_instrument(inst_id)` 与 `get_balance()` 的客户端
        min_notional: 额外的单笔最小名义金额（USDT）；0 表示只按交易所
            的 minSz 判定，不做账户级下限
        balance_buffer: 余额安全垫比例
        balance_cache_seconds: 余额缓存时长；0 表示每次现查（仅测试用）
    """

    def __init__(
        self,
        client,
        *,
        min_notional: Decimal = Decimal("0"),
        balance_buffer: Decimal = DEFAULT_BALANCE_BUFFER,
        balance_cache_seconds: float = DEFAULT_BALANCE_CACHE_SECONDS,
    ) -> None:
        if min_notional < 0:
            raise ValueError("min_notional must be non-negative")
        if balance_buffer < 0:
            raise ValueError("balance_buffer must be non-negative")
        self._client = client
        self._min_notional = min_notional
        self._buffer = balance_buffer
        self._cache_seconds = balance_cache_seconds
        self._balance: dict | None = None
        self._balance_ts: float = 0.0

    async def check(
        self,
        signal: Signal,
        qty: Decimal,
        market_price: Decimal | None = None,
    ) -> PreflightDecision:
        """校验一笔待提交订单。

        Args:
            signal: 原始信号（取 inst_id / side / order_type / price）
            qty: 风控调整后的基础币数量
            market_price: 当前市价；None 时限价单回落 signal.price
        """
        if qty <= 0:
            return _reject(
                Decimal("0"),
                RULE_LOT_SIZE,
                f"下单数量 {qty} 无效，拒绝提交",
                actual_value=qty,
            )

        instrument = await asyncio.to_thread(
            self._client.get_instrument, signal.inst_id
        )
        if instrument is None:
            return _reject(
                qty,
                RULE_INSTRUMENT,
                f"查不到产品配置 {signal.inst_id}，无法确认精度与最小量",
            )

        normalized, error = _normalize_qty(instrument, qty)
        if error is not None:
            return error

        price = _resolve_price(signal, market_price)
        if price is not None and price > 0:
            notional = normalized * price
            if self._min_notional > 0 and notional < self._min_notional:
                return _reject(
                    normalized,
                    RULE_MIN_NOTIONAL,
                    f"名义金额 {notional} 低于下限 {self._min_notional}",
                    limit_value=self._min_notional,
                    actual_value=notional,
                )

        balance_checked = False
        if instrument.inst_type == "SPOT" and price is not None and price > 0:
            verdict, checked = await self._check_spot_balance(
                instrument, signal.side, normalized, price
            )
            if verdict is not None:
                return verdict
            balance_checked = checked

        return PreflightDecision(
            ok=True, qty=normalized, balance_checked=balance_checked
        )

    async def _check_spot_balance(
        self,
        instrument: Instrument,
        side: str,
        qty: Decimal,
        price: Decimal,
    ) -> tuple[PreflightDecision | None, bool]:
        """现货买卖余额校验。

        返回 `(拒绝结果, 是否真正做了校验)`：余额不可用或缺少该币种明细
        时返回 `(None, False)` 放行，调用方据此把 `balance_checked`
        标成 False，避免把「没查」显示成「查过且通过」。

        合约不在此校验：可用保证金取决于杠杆与保证金模式，只看
        `availBal` 会误杀带杠杆的正常订单，因此交给交易所 51008 报错，
        该错误已由死信 + 告警路径覆盖。
        """
        balance = await self._balance_snapshot()
        if balance is None:
            return None, False

        if side == "buy":
            ccy = instrument.quote_ccy
            required = qty * price * (Decimal("1") + self._buffer)
            hint = f"买入 {qty} {instrument.base_ccy} 需要 {required} {ccy}"
        else:
            ccy = instrument.base_ccy
            required = qty * (Decimal("1") + self._buffer)
            hint = f"卖出 {qty} {instrument.base_ccy} 需要可用 {required} {ccy}"

        available = _available(balance, ccy)
        if available is None:
            log.warning(
                f"preflight balance detail missing ccy={ccy} "
                f"inst={instrument.inst_id}; skip balance check"
            )
            return None, False

        if available < required:
            return (
                _reject(
                    qty,
                    RULE_BALANCE,
                    f"{hint}，可用仅 {available}",
                    limit_value=required,
                    actual_value=available,
                ),
                True,
            )
        return None, True

    async def _balance_snapshot(self) -> dict | None:
        """带缓存的账户余额；读失败不缓存，下一单会重试。"""
        now = time.monotonic()
        if (
            self._balance is not None
            and self._cache_seconds > 0
            and now - self._balance_ts < self._cache_seconds
        ):
            return self._balance
        try:
            self._balance = await asyncio.to_thread(self._client.get_balance)
            self._balance_ts = now
        except Exception as e:  # noqa: BLE001
            log.warning(f"preflight balance unavailable, skip check: {e}")
            self._balance = None
            return None
        return self._balance


def _normalize_qty(
    instrument: Instrument, qty: Decimal
) -> tuple[Decimal, PreflightDecision | None]:
    """按交易所精度归一化数量，返回 (基础币数量, 拒绝结果)。"""
    if instrument.inst_type == "SPOT":
        normalized = floor_to(qty, instrument.lot_sz)
        if normalized <= 0:
            return normalized, _reject(
                normalized,
                RULE_LOT_SIZE,
                f"{instrument.inst_id} 数量 {qty} 按 lotSz={instrument.lot_sz} "
                f"取整后为 0，拒绝提交",
                limit_value=instrument.lot_sz,
                actual_value=qty,
            )
        if normalized < instrument.min_sz:
            return normalized, _reject(
                normalized,
                RULE_MIN_SIZE,
                f"{instrument.inst_id} 数量 {normalized} 低于最小下单量 "
                f"{instrument.min_sz}",
                limit_value=instrument.min_sz,
                actual_value=normalized,
            )
        return normalized, None

    # 合约：sz 的单位是张，minSz 也是张
    if instrument.ct_val <= 0:
        return qty, _reject(
            qty,
            RULE_INSTRUMENT,
            f"{instrument.inst_id} 面值 ctVal={instrument.ct_val} 非法，拒绝提交",
        )
    contracts = floor_to(qty / instrument.ct_val, instrument.lot_sz)
    if contracts <= 0:
        return Decimal("0"), _reject(
            Decimal("0"),
            RULE_LOT_SIZE,
            f"{instrument.inst_id} 数量 {qty} 折合张数为 0"
            f"（ctVal={instrument.ct_val}，lotSz={instrument.lot_sz}），拒绝提交",
            limit_value=instrument.ct_val * instrument.lot_sz,
            actual_value=qty,
        )
    if contracts < instrument.min_sz:
        return contracts * instrument.ct_val, _reject(
            contracts * instrument.ct_val,
            RULE_MIN_SIZE,
            f"{instrument.inst_id} 折合 {contracts} 张低于最小下单量 "
            f"{instrument.min_sz} 张",
            limit_value=instrument.min_sz,
            actual_value=contracts,
        )
    return contracts * instrument.ct_val, None


def _resolve_price(
    signal: Signal, market_price: Decimal | None
) -> Decimal | None:
    if signal.order_type == "limit" and signal.price is not None:
        return signal.price
    return market_price


def _available(balance: dict, ccy: str) -> Decimal | None:
    """从 OKX 余额明细里取某币种可用数量。

    `availBal` 是可直接用于下单的余额；`availEq` 在部分账户类型下才有，
    作为兜底。取不到该币种明细返回 None（表示「不知道」，不是「0」）。
    """
    details = balance.get("details") or []
    for item in details:
        if str(item.get("ccy", "")).upper() != ccy.upper():
            continue
        for field in ("availBal", "availEq", "cashBal"):
            raw = item.get(field)
            if raw in (None, ""):
                continue
            try:
                return max(Decimal(str(raw)), Decimal("0"))
            except (TypeError, ValueError):
                continue
        return None
    return None


def _reject(
    qty: Decimal,
    rule: str,
    reason: str,
    *,
    limit_value: Decimal | None = None,
    actual_value: Decimal | None = None,
) -> PreflightDecision:
    return PreflightDecision(
        ok=False,
        qty=qty,
        reason=reason,
        rule=rule,
        limit_value=limit_value,
        actual_value=actual_value,
    )


__all__ = [
    "DEFAULT_BALANCE_BUFFER",
    "DEFAULT_BALANCE_CACHE_SECONDS",
    "PreflightChecker",
    "PreflightDecision",
    "floor_to",
]
