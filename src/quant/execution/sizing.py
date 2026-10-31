"""服务端仓位计算（M8-14）。

信号不带 `qty` 时由 worker 按「权益 × 单笔比例 ÷ 价格」算量，再按交易所
精度向下取整。比例来源三级回落（任一命中即止）：

1. `Signal.qty` 给了（且 > 0）→ 直接用，不进入本模块；
2. `Signal.size_pct` 给了 → 按它算；
3. 都没给 → 用 `[execution] default_size_pct`（默认 0.1）。

算量只负责「该下多少」，不负责「能不能下」：单笔名义上限、单品种与总
仓位比例仍由风控 checker 判定，预检再校验交易所硬约束。因此本模块的
输出必须**向下取整到 lotSz**，否则 broker 静默取整会让本地记录与成交不符。

合约的 `sz` 单位是张：基础币先除 `ctVal` 再按 `lotSz` 取整，`minSz` 也是
比张数，不是比基础币。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from quant.backtest.signal import Signal
from quant.risk.limits import RiskLimits

#: 模拟撮合没有交易所精度，用 1e-8 作为「不截断」的等价步长
SIM_LOT_SZ = Decimal("0.00000001")

CAPPED_BY_MAX_ORDER = "max_order_qty"
CAPPED_BY_LOT_ROUND = "lot_round"


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """算量需要的交易规则子集（与 `Instrument` 解耦，便于测试）。"""

    lot_sz: Decimal
    min_sz: Decimal = Decimal("0")
    ct_val: Decimal = Decimal("1")
    inst_type: str = "SPOT"

    def __post_init__(self) -> None:
        if self.lot_sz <= 0:
            raise ValueError(f"lot_sz must be positive, got {self.lot_sz}")
        if self.min_sz < 0:
            raise ValueError(f"min_sz must be non-negative, got {self.min_sz}")
        if self.ct_val <= 0:
            raise ValueError(f"ct_val must be positive, got {self.ct_val}")

    @property
    def is_contract(self) -> bool:
        return self.inst_type in ("SWAP", "FUTURES", "OPTION")

    @classmethod
    def from_instrument(cls, instrument) -> InstrumentSpec:
        """由 `models.instrument.Instrument` 构造。"""
        return cls(
            lot_sz=instrument.lot_sz,
            min_sz=instrument.min_sz,
            ct_val=instrument.ct_val,
            inst_type=instrument.inst_type,
        )


#: 无交易所产品配置时（模拟撮合）使用的宽松规格
SIM_SPEC = InstrumentSpec(lot_sz=SIM_LOT_SZ)


@dataclass(frozen=True, slots=True)
class SizingResult:
    """一次服务端算量的完整结果，供审计与日志使用。"""

    qty: Decimal          # 取整后的可下单量（基础币，合约已折回基础币）
    raw_qty: Decimal      # 取整前
    notional: Decimal     # qty × price
    equity: Decimal
    price: Decimal
    size_pct: Decimal
    capped_by: str | None = None


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    """按步长向下取整（OKX 的 lotSz 要求）。"""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


class PositionSizer:
    """服务端仓位计算器。

    Args:
        default_size_pct: 信号既没给 qty 也没给 size_pct 时的单笔目标占比
    """

    def __init__(self, default_size_pct: Decimal = Decimal("0.1")) -> None:
        if default_size_pct <= 0:
            raise ValueError("default_size_pct must be positive")
        self.default_size_pct = default_size_pct

    def resolve_size_pct(self, signal: Signal) -> Decimal:
        """单笔目标占比：信号显式给了就用，否则回落配置默认值。"""
        if signal.size_pct is not None:
            return signal.size_pct
        return self.default_size_pct

    def size(
        self,
        signal: Signal,
        equity: Decimal | None,
        price: Decimal | None,
        limits: RiskLimits | None = None,
        *,
        spec: InstrumentSpec | None = None,
    ) -> SizingResult | None:
        """算出可下单量；无法算量（缺价格/权益、低于最小量）返回 None。

        Args:
            signal: 待算量信号（只取 side / size_pct，qty 由调用方另行处理）
            equity: 账户总权益；None 或非正时无法算量
            price: 定价依据（市价或限价单价格）；None 或非正时无法算量
            limits: 生效限额；提供时按 `max_order_qty` 截断
            spec: 交易规则；None 时按模拟撮合的宽松精度处理
        """
        if equity is None or not equity.is_finite() or equity <= 0:
            return None
        if price is None or not price.is_finite() or price <= 0:
            return None

        size_pct = self.resolve_size_pct(signal)
        if size_pct <= 0:
            return None

        spec = spec or SIM_SPEC
        raw_qty = equity * size_pct / price
        if raw_qty <= 0:
            return None

        target = raw_qty
        capped_by: str | None = None
        if limits is not None:
            max_qty = Decimal(limits.max_order_qty) / price
            if target > max_qty:
                target = max_qty
                capped_by = CAPPED_BY_MAX_ORDER

        if spec.is_contract:
            contracts = floor_to(target / spec.ct_val, spec.lot_sz)
            if contracts <= 0 or contracts < spec.min_sz:
                return None
            qty = contracts * spec.ct_val
        else:
            qty = floor_to(target, spec.lot_sz)
            if qty <= 0:
                return None
            if spec.min_sz > 0 and qty < spec.min_sz:
                return None

        if capped_by is None and qty != raw_qty:
            capped_by = CAPPED_BY_LOT_ROUND

        return SizingResult(
            qty=qty,
            raw_qty=raw_qty,
            notional=qty * price,
            equity=equity,
            price=price,
            size_pct=size_pct,
            capped_by=capped_by,
        )


__all__ = [
    "CAPPED_BY_LOT_ROUND",
    "CAPPED_BY_MAX_ORDER",
    "SIM_SPEC",
    "InstrumentSpec",
    "PositionSizer",
    "SizingResult",
    "floor_to",
]
