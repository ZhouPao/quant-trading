"""风控规则检查（spec §10）。

5 条规则：
1. 单笔最大下单量（名义价值）
2. 单品种最大持仓比例
3. 总仓位上限
4. 日亏损上限
5. 连续亏损保护

超限时可下调下单量（adjusted_qty）；无法下调到正数则拒绝。
账户权益由调用方按需传入（worker 取 monitor 快照的 total_equity）；
未传入时退回构造参数 initial_equity，便于不接交易所的单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant.backtest.order import Fill
from quant.backtest.signal import Signal
from quant.foundation.logger import get_logger
from quant.risk.config import RiskConfig
from quant.risk.pnl import round_trip_pnls
from quant.state.sqlite_store import SQLiteStore

log = get_logger("risk.checker")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """风控决策。

    halt=True 表示触发了不可恢复的硬性风控上限（日亏损/连续亏损），
    调用方应据此触发紧急停止（kill switch）。
    """

    approved: bool
    adjusted_qty: Decimal
    reason: str = ""
    halt: bool = False


class RiskChecker:
    """信号风控检查器。"""

    def __init__(
        self,
        config: RiskConfig,
        store: SQLiteStore,
        initial_equity: Decimal = Decimal("10000"),
    ) -> None:
        self.config = config
        self.store = store
        self.equity = initial_equity

    def check(
        self,
        signal: Signal,
        market_price: Decimal | None = None,
        today_start_ms: int = 0,
        *,
        equity: Decimal | None = None,
    ) -> RiskDecision:
        """检查信号。

        Args:
            signal: 待检查信号
            market_price: 当前市价；None 时尝试用持仓均价，仍无则跳过名义价值类检查
            today_start_ms: 当日 00:00 毫秒（UTC+8）；0 表示不过滤（全天）
            equity: 账户权益；None 或非正时用构造时的 initial_equity
        """
        base_equity = (
            equity
            if equity is not None and equity.is_finite() and equity > 0
            else self.equity
        )
        price = market_price
        position = self.store.get_position(signal.inst_id)
        if price is None and position.avg_price > 0:
            price = position.avg_price

        # 规则 4：日亏损上限
        today_pnl = self.store.get_today_realized_pnl(today_start_ms)
        loss_limit = base_equity * self.config.daily_loss_limit
        if today_pnl <= -loss_limit:
            return RiskDecision(
                approved=False,
                adjusted_qty=Decimal("0"),
                reason=f"daily loss limit hit: pnl={today_pnl}, limit=-{loss_limit}",
                halt=True,
            )

        # 规则 5：连续亏损保护
        if self._consecutive_losses() >= self.config.consecutive_loss_limit:
            return RiskDecision(
                approved=False,
                adjusted_qty=Decimal("0"),
                reason=(
                    f"consecutive loss limit hit: "
                    f"{self.config.consecutive_loss_limit}"
                ),
                halt=True,
            )

        qty = signal.qty
        signed = qty if signal.side == "buy" else -qty
        new_qty = position.qty + signed
        increasing = abs(new_qty) > abs(position.qty)

        # 价格未知时无法做名义价值检查，放行
        if price is None or price <= 0:
            return RiskDecision(approved=True, adjusted_qty=qty)

        # 规则 1：单笔最大下单量（仅加仓方向）
        if increasing:
            notional = qty * price
            if notional > self.config.max_order_qty:
                max_qty = self.config.max_order_qty / price
                if max_qty <= 0:
                    return RiskDecision(
                        approved=False,
                        adjusted_qty=Decimal("0"),
                        reason=f"order notional {notional} exceeds max",
                    )
                qty = max_qty
                signed = qty if signal.side == "buy" else -qty
                new_qty = position.qty + signed
                log.info(
                    f"qty adjusted by max_order_qty: {signal.qty} -> {qty}"
                )

        # 规则 2：单品种最大持仓比例
        position_limit = base_equity * self.config.max_position_pct
        if abs(new_qty) * price > position_limit:
            allowed_qty_c = position_limit / price
            if signal.side == "buy":
                allowed_qty = allowed_qty_c - position.qty
            else:
                allowed_qty = allowed_qty_c + position.qty
            if allowed_qty <= 0:
                return RiskDecision(
                    approved=False,
                    adjusted_qty=Decimal("0"),
                    reason=(
                        f"position limit hit: "
                        f"new={abs(new_qty) * price} > {position_limit}"
                    ),
                )
            if allowed_qty < qty:
                qty = allowed_qty
                signed = qty if signal.side == "buy" else -qty
                new_qty = position.qty + signed
                log.info(
                    f"qty adjusted by max_position_pct: "
                    f"{signal.qty} -> {qty}"
                )

        # 规则 3：总仓位上限
        total_value = sum(
            (abs(p.qty) * price for p in self.store.get_all_positions()),
            Decimal("0"),
        )
        current_inst_value = abs(position.qty) * price
        delta_value = abs(new_qty) * price - current_inst_value
        total_limit = base_equity * self.config.max_total_pct
        if total_value + delta_value > total_limit:
            remaining = total_limit - (total_value - current_inst_value)
            allowed_qty_c = max(remaining, Decimal("0")) / price
            if signal.side == "buy":
                allowed_qty = allowed_qty_c - position.qty
            else:
                allowed_qty = allowed_qty_c + position.qty
            if allowed_qty <= 0:
                return RiskDecision(
                    approved=False,
                    adjusted_qty=Decimal("0"),
                    reason=(
                        f"total position limit hit: "
                        f"{total_value + delta_value} > {total_limit}"
                    ),
                )
            if allowed_qty < qty:
                qty = allowed_qty
                log.info(
                    f"qty adjusted by max_total_pct: {signal.qty} -> {qty}"
                )

        return RiskDecision(approved=True, adjusted_qty=qty)

    def _consecutive_losses(self) -> int:
        """最近连续亏损回合数。"""
        fills = self.store.get_recent_trades(self.config.consecutive_loss_limit * 4)
        pnls = _round_trip_pnls(fills)
        count = 0
        for pnl in reversed(pnls):
            if pnl < 0:
                count += 1
            else:
                break
        return count


def _round_trip_pnls(fills: list[Fill]) -> list[Decimal]:
    """按 FIFO 配对计算每个已平仓回合的盈亏（含手续费）。

    逻辑抽取到 quant.risk.pnl.round_trip_pnls；此处保留转发以兼容旧引用。
    """
    return round_trip_pnls(fills)


__all__ = ["RiskChecker", "RiskDecision"]
