"""监控快照（F5.1/F5.2）。

从 OKX 账户/持仓接口原始响应构造不可变快照：
- AccountSnapshot：账户余额、可用/冻结、未实现盈亏、保证金率
- PositionSnapshot：单品种持仓数量/方向、开仓均价、未实现盈亏、杠杆

现货账户无保证金率（mgnRatio 为空），margin_ratio 记为 None 表示不适用。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _dec(value: object) -> Decimal:
    """容错转 Decimal：None/空串/非法值 → 0。"""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _optional_dec(value: object) -> Decimal | None:
    """容错转可选 Decimal：None/空串/≤0 → None。"""
    if value is None or value == "":
        return None
    v = _dec(value)
    if v <= 0:
        return None
    return v


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """账户快照。"""

    ts: int
    total_equity: Decimal
    avail_usdt: Decimal
    frozen_usdt: Decimal
    unrealized_pnl: Decimal
    margin_ratio: Decimal | None

    @classmethod
    def from_okx(cls, raw: dict, ts: int) -> AccountSnapshot:
        """从 OKX /account/balance 响应项构造。"""
        details = raw.get("details") or []
        usdt = next((d for d in details if d.get("ccy") == "USDT"), {})
        return cls(
            ts=ts,
            total_equity=_dec(raw.get("totalEq")),
            avail_usdt=_dec(usdt.get("availBal")),
            frozen_usdt=_dec(usdt.get("frozenBal")),
            unrealized_pnl=_dec(raw.get("upl")),
            margin_ratio=_margin_ratio(raw.get("mgnRatio")),
        )


def _margin_ratio(value: object) -> Decimal | None:
    """保证金率；空/0 视为不适用（现货账户无此项）。"""
    v = _dec(value)
    if v <= 0:
        return None
    return v


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """持仓快照。

    liq_price: 预估强平价（仅合约持仓有，现货为 None）
    mark_price: 标记价格（合约有 markPx；现货缺省回退 avg_price）
    """

    inst_id: str
    qty: Decimal
    avg_price: Decimal
    unrealized_pnl: Decimal
    leverage: Decimal
    liq_price: Decimal | None
    mark_price: Decimal

    @classmethod
    def from_okx(
        cls, raw: dict, ct_val: Decimal = Decimal("1")
    ) -> PositionSnapshot:
        """从 OKX /account/positions 响应项构造。

        合约的 `pos` 是**张数**，乘 `ct_val`（合约面值）换算成基础币，
        与本地记账、风控、看板保持同一口径；现货 `ct_val` 传 1。
        `upl` 已是结算币金额，不随数量换算。
        """
        avg = _dec(raw.get("avgPx"))
        mark = _optional_dec(raw.get("markPx")) or avg
        return cls(
            inst_id=str(raw.get("instId", "")),
            qty=_dec(raw.get("pos")) * ct_val,
            avg_price=avg,
            unrealized_pnl=_dec(raw.get("upl")),
            leverage=_dec(raw.get("lever") or "1"),
            liq_price=_optional_dec(raw.get("liqPx")),
            mark_price=mark,
        )

    @classmethod
    def from_spot(
        cls,
        inst_id: str,
        qty: Decimal,
        mark_price: Decimal,
        avg_price: Decimal = Decimal("0"),
    ) -> PositionSnapshot:
        """从余额明细 + 公开行情构造现货持仓快照。

        现货没有杠杆与强平价；`avg_price` 由调用方给出（优先交易所的
        `openAvgPx`，其次本地记账）。未知成本基数时传 0，此时未实现盈亏
        记 0 —— 宁可留空，也不凭空编一个成本价。
        """
        upl = (
            (mark_price - avg_price) * qty
            if avg_price > 0
            else Decimal("0")
        )
        return cls(
            inst_id=inst_id,
            qty=qty,
            avg_price=avg_price,
            unrealized_pnl=upl,
            leverage=Decimal("1"),
            liq_price=None,
            mark_price=mark_price,
        )


def liq_distance(pos: PositionSnapshot) -> Decimal | None:
    """强平价距离 = |标记价 - 强平价| / 标记价（0~1）。

    无强平价（现货）返回 None。
    """
    if pos.liq_price is None or pos.mark_price <= 0:
        return None
    return abs(pos.mark_price - pos.liq_price) / pos.mark_price


@dataclass(frozen=True, slots=True)
class RiskMetrics:
    """风险指标（F5.3）。

    max_drawdown: 当前权益相对历史峰值权益的回撤（0~1）
    cancel_rate: 撤单率 = 已撤销 / (已成交 + 已撤销)；分母为 0 时 None
    min_liq_distance: 所有持仓中最小的强平价距离（最危险）；无合约持仓 None
    """

    ts: int
    max_drawdown: Decimal
    cancel_rate: Decimal | None
    min_liq_distance: Decimal | None

    @classmethod
    def empty(cls, ts: int) -> RiskMetrics:
        return cls(
            ts=ts,
            max_drawdown=Decimal("0"),
            cancel_rate=None,
            min_liq_distance=None,
        )


__all__ = [
    "AccountSnapshot",
    "PositionSnapshot",
    "RiskMetrics",
    "liq_distance",
]
