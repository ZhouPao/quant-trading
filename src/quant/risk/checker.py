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

    rule / limit_name / limit_value / actual_value 由命中的规则填写，
    用于 `risk_events` 审计：没有它们只能看到「拒了」，看不出「被谁拒的」。
    """

    approved: bool
    adjusted_qty: Decimal
    reason: str = ""
    halt: bool = False
    rule: str | None = None
    limit_name: str | None = None
    limit_value: Decimal | None = None
    actual_value: Decimal | None = None


class RiskChecker:
    """信号风控检查器。"""

    def __init__(
        self,
        config: RiskConfig,
        store: SQLiteStore,
        initial_equity: Decimal = Decimal("10000"),
        *,
        allow_initial_equity_fallback: bool = True,
        event_log: bool = True,
    ) -> None:
        """构造检查器。

        Args:
            config: 生效限额
            store: 状态存储（持仓/成交/风控事件）
            initial_equity: 兜底权益（取不到账户快照时使用）
            allow_initial_equity_fallback: 是否允许用 initial_equity 兜底。
                默认 True 保持既有行为；**生产入口必须显式传入配置值**
                （`[risk] allow_initial_equity_fallback`，默认 False），
                否则 monitor 挂掉时会按初始资金算比例、放大真实敞口
            event_log: 是否把拒绝/减量/硬停止写入 `risk_events`
        """
        self.config = config
        self.store = store
        self.equity = initial_equity
        self.allow_initial_equity_fallback = allow_initial_equity_fallback
        self.event_log = event_log

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
            equity: 账户权益；None 或非正时按 allow_initial_equity_fallback
                决定是退回 initial_equity 还是拒绝开仓
        """
        if signal.qty is None:
            # 服务端算量（M8-14）未开启或算量失败时不应走到这里；
            # 显式拒单，避免把 None 带进后面的乘除。
            return RiskDecision(
                approved=False,
                adjusted_qty=Decimal("0"),
                reason="signal qty not provided",
                rule="qty_missing",
            )

        if equity is None or not equity.is_finite() or equity <= 0:
            if not self.allow_initial_equity_fallback:
                # 权益缺失是数据链路问题而非账户风险：拒绝开仓 + 告警即可，
                # 不置 halt（monitor 恢复后一个轮询周期就能自愈）
                return RiskDecision(
                    approved=False,
                    adjusted_qty=Decimal("0"),
                    reason="account snapshot unavailable",
                    halt=False,
                    rule="equity_unavailable",
                )
            log.warning(
                "risk equity falls back to initial_equity="
                f"{self.equity} (explicitly allowed)"
            )
            base_equity = self.equity
        else:
            base_equity = equity

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
                rule="daily_loss_limit",
                limit_name="daily_loss_limit",
                limit_value=loss_limit,
                actual_value=today_pnl,
            )

        # 规则 5：连续亏损保护
        losses = self._consecutive_losses()
        if losses >= self.config.consecutive_loss_limit:
            return RiskDecision(
                approved=False,
                adjusted_qty=Decimal("0"),
                reason=(
                    f"consecutive loss limit hit: "
                    f"{self.config.consecutive_loss_limit}"
                ),
                halt=True,
                rule="consecutive_loss_limit",
                limit_name="consecutive_loss_limit",
                limit_value=Decimal(self.config.consecutive_loss_limit),
                actual_value=Decimal(losses),
            )

        qty = signal.qty
        signed = qty if signal.side == "buy" else -qty
        new_qty = position.qty + signed
        increasing = abs(new_qty) > abs(position.qty)

        # 记录「最终真正约束住下单量」的规则，供 risk_events 溯源：
        # 多条规则依次减量时，取最后一次生效的那条。
        hit: tuple[str, Decimal, Decimal] | None = None

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
                        rule="max_order_qty",
                        limit_name="max_order_qty",
                        limit_value=self.config.max_order_qty,
                        actual_value=notional,
                    )
                qty = max_qty
                signed = qty if signal.side == "buy" else -qty
                new_qty = position.qty + signed
                hit = ("max_order_qty", self.config.max_order_qty, notional)
                log.info(
                    f"qty adjusted by max_order_qty: {signal.qty} -> {qty}"
                )

        # 规则 2：单品种最大持仓比例
        position_limit = base_equity * self.config.max_position_pct
        actual_position = abs(new_qty) * price
        if actual_position > position_limit:
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
                        f"new={actual_position} > {position_limit}"
                    ),
                    rule="max_position_pct",
                    limit_name="max_position_pct",
                    limit_value=position_limit,
                    actual_value=actual_position,
                )
            if allowed_qty < qty:
                qty = allowed_qty
                signed = qty if signal.side == "buy" else -qty
                new_qty = position.qty + signed
                hit = ("max_position_pct", position_limit, actual_position)
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
        actual_total = total_value + delta_value
        if actual_total > total_limit:
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
                        f"{actual_total} > {total_limit}"
                    ),
                    rule="max_total_pct",
                    limit_name="max_total_pct",
                    limit_value=total_limit,
                    actual_value=actual_total,
                )
            if allowed_qty < qty:
                qty = allowed_qty
                hit = ("max_total_pct", total_limit, actual_total)
                log.info(
                    f"qty adjusted by max_total_pct: {signal.qty} -> {qty}"
                )

        if hit is None:
            return RiskDecision(approved=True, adjusted_qty=qty)
        rule, limit_value, actual_value = hit
        return RiskDecision(
            approved=True,
            adjusted_qty=qty,
            reason=f"qty reduced from {signal.qty} to {qty} by {rule}",
            rule=rule,
            limit_name=rule,
            limit_value=limit_value,
            actual_value=actual_value,
        )

    def record_event(
        self,
        decision: RiskDecision,
        signal: Signal,
        *,
        now_ms: int,
        equity: Decimal | None,
    ) -> bool:
        """把拒绝/减量/硬停止写入 `risk_events`。

        Returns:
            是否实际写入。写库失败只记 ERROR 不向上抛——审计不能
            反过来阻断下单主流程（spec §15）。
        """
        from quant.risk.events import event_from_decision, should_log

        if not self.event_log or not should_log(decision, signal):
            return False
        event = event_from_decision(
            decision, signal, now_ms=now_ms, equity=equity
        )
        try:
            self.store.insert_risk_event(**event.as_row())  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001
            log.error(f"record risk event failed: {e}")
            return False
        return True

    def record_sized(
        self,
        signal: Signal,
        *,
        now_ms: int,
        equity: Decimal | None,
        raw_qty: Decimal,
        qty: Decimal,
        capped_by: str | None,
        size_pct: Decimal | None = None,
    ) -> bool:
        """记录一次服务端算量（M8-14）。

        每次算量都写：回答「这单数量从哪来、被谁截断」。写库失败只记
        ERROR，不阻断下单主流程。
        """
        from quant.risk.events import sized_event

        if not self.event_log:
            return False
        event = sized_event(
            signal,
            now_ms=now_ms,
            equity=equity,
            raw_qty=raw_qty,
            qty=qty,
            capped_by=capped_by,
            size_pct=size_pct,
        )
        try:
            self.store.insert_risk_event(**event.as_row())  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001
            log.error(f"record sized event failed: {e}")
            return False
        return True

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
