"""FIFO 回合盈亏计算（spec §10 连续亏损、M4 监控共用）。"""
from __future__ import annotations

from decimal import Decimal

from quant.backtest.order import Fill


def round_trip_pnls(fills: list[Fill]) -> list[Decimal]:
    """按 FIFO 配对计算每个已平仓回合的盈亏（含手续费）。

    与风控 RiskChecker 使用的逻辑一致，供风控与监控共享。

    先按 `inst_id` 分组再配对：现货与合约、不同品种的成交不能互相抵平，
    否则同一时刻持有的 BTC 现货会被拿去和 BTC-USDT-SWAP 的卖单配对。
    各组回合按平仓时间排序后合并，保证“最近连续亏损”仍按时间先后判断。
    """
    by_inst: dict[str, list[Fill]] = {}
    for fill in fills:
        by_inst.setdefault(fill.inst_id, []).append(fill)

    closed: list[tuple[int, Decimal]] = []
    for inst_fills in by_inst.values():
        closed.extend(_fifo_round_trips(inst_fills))
    closed.sort(key=lambda item: item[0])
    return [pnl for _, pnl in closed]


def _fifo_round_trips(fills: list[Fill]) -> list[tuple[int, Decimal]]:
    """单一品种的 FIFO 配对，返回 (平仓时间, 回合盈亏)。"""
    pos_qty = Decimal("0")
    avg_price = Decimal("0")
    lots: list[list[Decimal]] = []
    pnls: list[tuple[int, Decimal]] = []

    for fill in fills:
        signed = fill.qty if fill.side == "buy" else -fill.qty
        fee_per_unit = fill.fee / fill.qty

        if pos_qty == 0:
            pos_qty, avg_price = signed, fill.price
            lots.append([signed, fee_per_unit])
            continue

        if (pos_qty > 0) == (signed > 0):
            avg_price = (
                pos_qty * avg_price + signed * fill.price
            ) / (pos_qty + signed)
            pos_qty += signed
            lots.append([signed, fee_per_unit])
            continue

        closing = min(abs(signed), abs(pos_qty))
        if pos_qty > 0:
            pnl = (fill.price - avg_price) * closing
        else:
            pnl = (avg_price - fill.price) * closing

        consume = closing
        entry_fee = Decimal("0")
        while consume > 0:
            lot = lots[0]
            take = min(consume, abs(lot[0]))
            entry_fee += lot[1] * take
            if take == abs(lot[0]):
                lots.pop(0)
            else:
                lot[0] -= take if lot[0] > 0 else -take
            consume -= take

        pnls.append((fill.ts, pnl - entry_fee - fee_per_unit * closing))

        old_qty = pos_qty
        pos_qty += signed
        if pos_qty == 0:
            avg_price = Decimal("0")
            lots.clear()
        elif abs(signed) > abs(old_qty):
            avg_price = fill.price
            lots = [[pos_qty, fee_per_unit]]

    return pnls


__all__ = ["round_trip_pnls"]
