"""绩效指标计算（spec §4.5 / MS4-C §7）。

口径与 TradingView Pine 对齐：
- equity = cash + position.qty * bar.close（引擎逐根记录）
- total_return = (期末权益 - 期初权益) / 期初权益
- annual_return = (1 + total_return) ^ (365天 / 回测时长) - 1；回测不足 1 天返回 0
- sharpe = mean(日收益) / std(日收益) * sqrt(365)（加密 7×24）
- max_drawdown = max(1 - equity / running_max)
- win_rate = 盈利回合数 / 总回合数
- profit_factor = 总盈利 / 总亏损（含手续费）
- avg_holding_bars = 回合持仓 K线数均值（FIFO 配对）

扩展指标（MS4-C §7），**无法计算时返回 None 而不是 0 或 inf**：
- calmar = annual_return / abs(max_drawdown)；max_drawdown == 0 → None
- sortino = mean(日收益) / 下行偏差 * sqrt(365)；
  下行偏差 = sqrt(mean(min(日收益, 0)^2))，无负收益日 → None
- monthly_returns = 按自然月聚合的收益率序列（JSON 文本，供直接入库）
- max_consecutive_losses / max_consecutive_loss_amount = 最长连亏笔数与
  所有连亏段中的最大累计亏损额
- exposure_ratio = 有持仓的 bar 数 / 总 bar 数
- turnover = 总成交名义 / 平均权益
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from quant.backtest.order import Fill
from quant.models.bar import Bar

MS_PER_DAY = 86_400_000
MS_PER_YEAR = 365 * MS_PER_DAY

_ZERO_METRICS: dict[str, float | int | str | None] = {
    "total_return": 0.0,
    "annual_return": 0.0,
    "sharpe": 0.0,
    "max_drawdown": 0.0,
    "win_rate": 0.0,
    "profit_factor": 0.0,
    "trade_count": 0,
    "avg_holding_bars": 0.0,
    # 扩展指标：没有权益曲线时无从计算，按约定给 None
    "calmar": None,
    "sortino": None,
    "monthly_returns": None,
    "max_consecutive_losses": 0,
    # 金额列保持 Decimal 文本口径（与 _consecutive_losses 一致）
    "max_consecutive_loss_amount": "0",
    "exposure_ratio": 0.0,
    "turnover": 0.0,
}


def compute_drawdown_curve(
    equity_curve: list[tuple[int, Decimal]],
) -> list[tuple[int, Decimal]]:
    """计算回撤曲线：每个点相对历史最高权益的跌幅（0~1）。"""
    curve: list[tuple[int, Decimal]] = []
    peak: Decimal | None = None
    for ts, equity in equity_curve:
        if peak is None or equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else Decimal("0")
        curve.append((ts, dd))
    return curve


def compute_metrics(
    trades: list[Fill],
    equity_curve: list[tuple[int, Decimal]],
    bars: list[Bar],
) -> dict[str, float | int | str | None]:
    """计算全部绩效指标。无权益曲线时返回零值/None，不抛异常。"""
    if not equity_curve:
        return dict(_ZERO_METRICS)

    initial = float(equity_curve[0][1])
    final = float(equity_curve[-1][1])
    total_return = (final - initial) / initial if initial else 0.0

    drawdown_curve = compute_drawdown_curve(equity_curve)
    max_drawdown = max((float(dd) for _, dd in drawdown_curve), default=0.0)

    win_rate, profit_factor, trade_count, avg_holding_bars, pnls = _trade_stats(
        trades, bars
    )
    annual_return = _annual_return(total_return, bars)
    losses, loss_amount = _consecutive_losses(pnls)

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "sharpe": _sharpe(equity_curve),
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "trade_count": trade_count,
        "avg_holding_bars": avg_holding_bars,
        "calmar": _calmar(annual_return, max_drawdown),
        "sortino": _sortino(equity_curve),
        "monthly_returns": _monthly_returns(equity_curve),
        "max_consecutive_losses": losses,
        "max_consecutive_loss_amount": loss_amount,
        "exposure_ratio": _exposure_ratio(bars, trades),
        "turnover": _turnover(trades, equity_curve),
    }


def _annual_return(total_return: float, bars: list[Bar]) -> float:
    """年化收益。回测不足 1 天或收益 <= -100% 时返回 0。"""
    if len(bars) < 2 or total_return <= -1:
        return 0.0
    duration_ms = bars[-1].ts - bars[0].ts
    if duration_ms < MS_PER_DAY:
        return 0.0
    years = duration_ms / MS_PER_YEAR
    return (1 + total_return) ** (1 / years) - 1


def _daily_returns(
    equity_curve: list[tuple[int, Decimal]],
) -> list[float]:
    """按自然日取最后一根权益，返回日收益序列（首日无收益）。"""
    daily: dict[int, Decimal] = {}
    for ts, equity in equity_curve:
        daily[ts // MS_PER_DAY] = equity  # 同日取最后一根

    days = sorted(daily)
    values = [float(daily[d]) for d in days]
    return [
        (values[i] - values[i - 1]) / values[i - 1]
        for i in range(1, len(values))
        if values[i - 1] != 0
    ]


def _sharpe(equity_curve: list[tuple[int, Decimal]]) -> float:
    """日频夏普。交易日数不足 3 天或波动为 0 时返回 0。"""
    returns = _daily_returns(equity_curve)
    if len(returns) < 2:
        return 0.0

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = variance**0.5
    if std == 0:
        return 0.0
    return mean / std * (365**0.5)


def _sortino(equity_curve: list[tuple[int, Decimal]]) -> float | None:
    """索提诺比率：mean(日收益) / 下行偏差 * sqrt(365)。

    下行偏差取 `sqrt(mean(min(r, 0)^2))`（正收益日记 0），
    因此无负收益日时偏差为 0 —— 此时返回 None，不返回 inf。
    """
    returns = _daily_returns(equity_curve)
    if len(returns) < 2:
        return None

    downside = [min(r, 0.0) for r in returns]
    deviation = (sum(r * r for r in downside) / len(returns)) ** 0.5
    if deviation == 0:
        return None
    return sum(returns) / len(returns) / deviation * (365**0.5)


def _calmar(annual_return: float, max_drawdown: float) -> float | None:
    """年化收益 / 最大回撤；无回撤时返回 None（除零无意义）。"""
    if max_drawdown == 0:
        return None
    return annual_return / abs(max_drawdown)


def _monthly_returns(
    equity_curve: list[tuple[int, Decimal]],
) -> str | None:
    """按自然月聚合的收益率序列，序列化为 JSON 文本。

    每月取该月最后一根权益，收益以上一月末为基准；首月只作基准、不出行
    （否则会凭空多出一条 0 收益）。不足两个月（含只有一根 bar）返回 None。
    """
    if len(equity_curve) < 2:
        return None

    monthly: dict[str, float] = {}
    for ts, equity in equity_curve:
        month = datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m")
        monthly[month] = float(equity)

    rows: list[dict[str, object]] = []
    previous: float | None = None
    for month in sorted(monthly):
        value = monthly[month]
        if previous is not None and previous:
            rows.append({"month": month, "ret": (value - previous) / previous})
        previous = value
    if not rows:
        return None
    return json.dumps(rows, ensure_ascii=False)


def _consecutive_losses(pnls: list[Decimal]) -> tuple[int, float]:
    """返回 (最长连亏笔数, 所有连亏段中的最大累计亏损额)。

    累计亏损额按正数记（亏 30 记 30），便于页面直接展示。
    """
    longest = 0
    worst_amount = Decimal("0")
    streak = 0
    amount = Decimal("0")
    for pnl in pnls:
        if pnl < 0:
            streak += 1
            amount += -pnl
            longest = max(longest, streak)
            worst_amount = max(worst_amount, amount)
        else:
            streak = 0
            amount = Decimal("0")
    # 金额保持 Decimal 文本：float 入库会产生 0.10000000000000001（硬规则 #1）
    return longest, str(worst_amount)


def _exposure_ratio(bars: list[Bar], trades: list[Fill]) -> float:
    """有持仓的 bar 数 / 总 bar 数；分母是全部回测 bar。"""
    if not bars:
        return 0.0

    fills = sorted(trades, key=lambda fill: fill.ts)
    qty = Decimal("0")
    index = 0
    held = 0
    for bar in bars:
        while index < len(fills) and fills[index].ts <= bar.ts:
            fill = fills[index]
            qty += fill.qty if fill.side == "buy" else -fill.qty
            index += 1
        if qty != 0:
            held += 1
    return held / len(bars)


def _turnover(
    trades: list[Fill], equity_curve: list[tuple[int, Decimal]]
) -> float:
    """总成交名义 / 平均权益；无成交或无权益时返回 0。"""
    if not trades or not equity_curve:
        return 0.0
    notional = sum((abs(fill.qty * fill.price) for fill in trades), Decimal("0"))
    average = sum((equity for _, equity in equity_curve), Decimal("0")) / len(
        equity_curve
    )
    if average == 0:
        return 0.0
    return float(notional / average)


def _trade_stats(
    trades: list[Fill],
    bars: list[Bar],
) -> tuple[float, float, int, float, list[Decimal]]:
    """按回合统计：胜率、盈亏比、回合数、平均持仓 K线数、逐回合盈亏。

    采用 FIFO 配对：减仓部分消耗最早的开仓 lot；
    手续费按平仓数量占比计入该回合。
    """
    if not trades:
        return 0.0, 0.0, 0, 0.0, []

    interval_ms = _bar_interval_ms(bars)
    pos_qty = Decimal("0")
    avg_price = Decimal("0")
    lots: list[list] = []  # [signed_qty, entry_ts]
    pnls: list[Decimal] = []
    holding_sum = Decimal("0")
    holding_qty = Decimal("0")

    for fill in trades:
        signed = fill.qty if fill.side == "buy" else -fill.qty
        fee_per_unit = fill.fee / fill.qty

        if pos_qty == 0:
            pos_qty, avg_price = signed, fill.price
            lots.append([signed, fill.ts, fee_per_unit])
            continue

        if (pos_qty > 0) == (signed > 0):
            # 同方向加仓
            avg_price = (pos_qty * avg_price + signed * fill.price) / (
                pos_qty + signed
            )
            pos_qty += signed
            lots.append([signed, fill.ts, fee_per_unit])
            continue

        # 反向：平仓（可能反手）
        closing = min(abs(signed), abs(pos_qty))
        if pos_qty > 0:
            pnl = (fill.price - avg_price) * closing
        else:
            pnl = (avg_price - fill.price) * closing

        # 开仓手续费（FIFO 消耗 lot）+ 平仓手续费按平仓占比
        entry_fee = Decimal("0")
        consume = closing
        while consume > 0:
            lot = lots[0]
            lot_abs = abs(lot[0])
            take = min(consume, lot_abs)
            holding_sum += Decimal(fill.ts - lot[1]) * take
            holding_qty += take
            entry_fee += lot[2] * take
            if take == lot_abs:
                lots.pop(0)
            else:
                lot[0] -= take if lot[0] > 0 else -take
            consume -= take

        pnl -= entry_fee + fill.fee * closing / fill.qty
        pnls.append(pnl)

        old_qty = pos_qty
        pos_qty += signed
        if pos_qty == 0:
            avg_price = Decimal("0")
            lots.clear()
        elif abs(signed) > abs(old_qty):
            # 反手：剩余部分按成交价开新仓
            avg_price = fill.price
            lots = [[pos_qty, fill.ts, fee_per_unit]]

    if not pnls:
        return 0.0, 0.0, 0, 0.0, []

    wins = [p for p in pnls if p > 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = -sum((p for p in pnls if p < 0), Decimal("0"))
    win_rate = len(wins) / len(pnls)

    if gross_loss > 0:
        profit_factor = float(gross_profit / gross_loss)
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    if holding_qty > 0 and interval_ms > 0:
        avg_holding_bars = float(holding_sum / holding_qty / interval_ms)
    else:
        avg_holding_bars = 0.0

    return win_rate, profit_factor, len(pnls), avg_holding_bars, pnls


def _bar_interval_ms(bars: list[Bar]) -> int:
    """推断 bar 间隔（相邻 ts 差值的中位数近似：取第一段）。"""
    if len(bars) < 2:
        return 0
    return bars[1].ts - bars[0].ts


__all__ = ["compute_metrics", "compute_drawdown_curve"]
