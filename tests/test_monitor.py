"""监控模块测试（M4）：快照解析 + 告警规则 + 风险指标。"""
from __future__ import annotations

import json
from decimal import Decimal

import fakeredis.aioredis
import pytest

from quant.backtest.order import Fill, Order
from quant.backtest.signal import Signal
from quant.execution.kill_switch import KillSwitch
from quant.foundation.config import MonitorSettings, SmtpSettings
from quant.models.instrument import Instrument
from quant.monitor.alert_service import AlertService
from quant.monitor.alerter import Alert, EmailAlerter
from quant.monitor.monitor import Monitor
from quant.monitor.snapshot import (
    AccountSnapshot,
    PositionSnapshot,
    liq_distance,
)
from quant.state.sqlite_store import SQLiteStore


def _swap_instrument(inst_id: str = "BTC-USDT-SWAP", ct_val: str = "0.01"):
    """合约产品配置：ctVal 决定「张数 → 基础币」的倍数。"""
    return Instrument(
        inst_id=inst_id,
        inst_type="SWAP",
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy="USDT",
        tick_sz=Decimal("0.1"),
        lot_sz=Decimal("1"),
        min_sz=Decimal("1"),
        ct_val=Decimal(ct_val),
        state="live",
        list_time=0,
    )

# =====================================================================
# 快照解析
# =====================================================================


class TestAccountSnapshot:
    def test_from_okx_parses_usdt_details(self):
        raw = {
            "totalEq": "97802.68",
            "upl": "123.45",
            "details": [
                {"ccy": "BTC", "eq": "1", "availBal": "1", "frozenBal": "0"},
                {"ccy": "USDT", "eq": "5000", "availBal": "4993.25", "frozenBal": "6.75"},
            ],
        }
        snap = AccountSnapshot.from_okx(raw, 1_700_000_000_000)

        assert snap.ts == 1_700_000_000_000
        assert snap.total_equity == Decimal("97802.68")
        assert snap.avail_usdt == Decimal("4993.25")
        assert snap.frozen_usdt == Decimal("6.75")
        assert snap.unrealized_pnl == Decimal("123.45")
        assert snap.margin_ratio is None  # 现货无 mgnRatio

    def test_from_okx_margin_ratio(self):
        raw = {
            "totalEq": "1000",
            "mgnRatio": "50.5",
            "details": [{"ccy": "USDT", "availBal": "100"}],
        }
        snap = AccountSnapshot.from_okx(raw, 0)
        assert snap.margin_ratio == Decimal("50.5")

    def test_from_okx_empty_margin_ratio_is_none(self):
        raw = {"totalEq": "1000", "mgnRatio": "", "details": []}
        snap = AccountSnapshot.from_okx(raw, 0)
        assert snap.margin_ratio is None

    def test_missing_fields_default_zero(self):
        snap = AccountSnapshot.from_okx({}, 0)
        assert snap.total_equity == Decimal("0")
        assert snap.avail_usdt == Decimal("0")
        assert snap.unrealized_pnl == Decimal("0")


class TestPositionSnapshot:
    def test_from_okx(self):
        raw = {
            "instId": "BTC-USDT",
            "pos": "-0.04",
            "avgPx": "80297.4",
            "upl": "12.3",
            "lever": "1",
        }
        pos = PositionSnapshot.from_okx(raw)
        assert pos.inst_id == "BTC-USDT"
        assert pos.qty == Decimal("-0.04")
        assert pos.avg_price == Decimal("80297.4")
        assert pos.unrealized_pnl == Decimal("12.3")
        assert pos.leverage == Decimal("1")
        assert pos.liq_price is None  # 现货无强平价
        assert pos.mark_price == Decimal("80297.4")  # 回退 avg

    def test_default_leverage_one(self):
        pos = PositionSnapshot.from_okx({"instId": "ETH-USDT", "pos": "0.5"})
        assert pos.leverage == Decimal("1")

    def test_from_okx_swap_with_liq_price(self):
        raw = {
            "instId": "BTC-USDT-SWAP",
            "pos": "1",
            "avgPx": "80000",
            "markPx": "82000",
            "liqPx": "77000",
            "lever": "10",
        }
        pos = PositionSnapshot.from_okx(raw)
        assert pos.liq_price == Decimal("77000")
        assert pos.mark_price == Decimal("82000")


class TestLiqDistance:
    def test_distance(self):
        pos = PositionSnapshot.from_okx(
            {
                "instId": "BTC-USDT-SWAP",
                "pos": "1",
                "avgPx": "80000",
                "markPx": "82000",
                "liqPx": "77000",
            }
        )
        # |82000 - 77000| / 82000 = 5000/82000 ≈ 0.06097
        assert liq_distance(pos) == Decimal("5000") / Decimal("82000")

    def test_none_without_liq_price(self):
        pos = PositionSnapshot.from_okx(
            {"instId": "BTC-USDT", "pos": "0.1", "avgPx": "80000"}
        )
        assert liq_distance(pos) is None


# =====================================================================
# 告警规则
# =====================================================================


class FakeClient:
    def __init__(self, balance: dict) -> None:
        self.balance = balance
        self._error_rate: float | None = None
        self.positions: dict[str, list[dict]] = {}
        self.tickers: dict[str, list[dict]] = {}
        self.instruments: dict[str, Instrument] = {}

    def get_balance(self) -> dict:
        return self.balance

    def get_error_rate(self) -> float | None:
        return self._error_rate

    def get_positions(self, inst_type: str = "SPOT") -> list[dict]:
        return self.positions.get(inst_type, [])

    def get_instrument(self, inst_id: str) -> Instrument | None:
        """合约面值查询（`pos` 张数 → 基础币换算用）。"""
        return self.instruments.get(inst_id)

    def get(self, path: str, params: dict | None = None) -> list[dict]:
        """公开行情（现货标记价用）。"""
        if path.endswith("/market/ticker"):
            return self.tickers.get((params or {}).get("instId", ""), [])
        return []


def _balance(total_eq: str, mgn_ratio: str = "") -> dict:
    return {
        "totalEq": total_eq,
        "mgnRatio": mgn_ratio,
        "details": [{"ccy": "USDT", "availBal": total_eq, "frozenBal": "0"}],
    }


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _monitor(redis, balance, **overrides) -> Monitor:
    settings = MonitorSettings(**overrides)
    return Monitor(
        client=FakeClient(balance),
        redis=redis,
        settings=settings,
        alerter=EmailAlerter(SmtpSettings()),
    )


@pytest.mark.asyncio
async def test_no_alerts_when_healthy(redis) -> None:
    await redis.set("quant:heartbeat:worker", "0")  # 0 会被判定超时，需用当前时间
    # 用当前时间使心跳新鲜
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor = _monitor(redis, _balance("10000"))
    alerts = await monitor.run_once()
    assert alerts == []


@pytest.mark.asyncio
async def test_worker_heartbeat_missing(redis) -> None:
    monitor = _monitor(redis, _balance("10000"))
    alerts = await monitor.run_once()
    assert any(a.rule == "worker_heartbeat" for a in alerts)


@pytest.mark.asyncio
async def test_worker_heartbeat_stale(redis) -> None:
    import time

    stale = int(time.time() * 1000) - 300_000  # 5 分钟前
    await redis.set("quant:heartbeat:worker", str(stale))
    monitor = _monitor(redis, _balance("10000"))
    alerts = await monitor.run_once()
    assert any(a.rule == "worker_heartbeat" for a in alerts)


@pytest.mark.asyncio
async def test_queue_backlog(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    for i in range(60):
        await redis.lpush("quant:signals", f"sig-{i}")
    monitor = _monitor(redis, _balance("10000"), queue_backlog_threshold=50)
    alerts = await monitor.run_once()
    assert any(a.rule == "queue_backlog" for a in alerts)


@pytest.mark.asyncio
async def test_dead_letter_new(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor = _monitor(redis, _balance("10000"))
    # 第一轮初始化基线，无死信告警
    await monitor.run_once()
    # 新增死信
    await redis.lpush("quant:signals:dead", "bad")
    alerts = await monitor.run_once()
    assert any(a.rule == "dead_letter" for a in alerts)


@pytest.mark.asyncio
async def test_margin_ratio_low(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor = _monitor(
        redis, _balance("10000", mgn_ratio="0.05"), margin_ratio_threshold=0.1
    )
    alerts = await monitor.run_once()
    assert any(a.rule == "margin_ratio" for a in alerts)


@pytest.mark.asyncio
async def test_balance_change(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    client = FakeClient(_balance("10000"))
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(balance_change_threshold=0.1),
        alerter=EmailAlerter(SmtpSettings()),
    )
    await monitor.run_once()  # 建立基线
    client.balance = _balance("12000")  # +20% 波动
    alerts = await monitor.run_once()
    assert any(a.rule == "balance_change" for a in alerts)


# =====================================================================
# 风险指标（F5.3）
# =====================================================================


def _seed_orders(store: SQLiteStore, statuses: list[str]) -> None:
    for i, status in enumerate(statuses):
        signal = Signal(
            strategy_id="sma_cross",
            side="buy",
            order_type="market",
            inst_id="BTC-USDT",
            qty=Decimal("1"),
            ts=1_700_000_000_000 + i,
            signal_id=f"sig-{i}",
        )
        store.save_signal(signal, status)
        store.save_order(
            Order(
                order_id=f"o-{i}",
                signal=signal,
                status=status,  # type: ignore[arg-type]
                created_ts=1_700_000_000_000 + i,
            )
        )


@pytest.mark.asyncio
async def test_cancel_rate(redis, tmp_path) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    try:
        _seed_orders(store, ["filled", "filled", "filled", "canceled"])
        monitor = Monitor(
            client=FakeClient(_balance("10000")),
            redis=redis,
            settings=MonitorSettings(),
            alerter=EmailAlerter(SmtpSettings()),
            store=store,
        )
        await monitor.run_once()
        assert monitor.metrics.cancel_rate == Decimal("0.25")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cancel_rate_none_without_store(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor = _monitor(redis, _balance("10000"))
    await monitor.run_once()
    assert monitor.metrics.cancel_rate is None


@pytest.mark.asyncio
async def test_cancel_rate_alert(redis, tmp_path) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    try:
        # 5 笔订单中 2 笔撤销 → 撤单率 0.4 > 0.2 阈值
        _seed_orders(store, ["filled", "filled", "filled", "canceled", "canceled"])
        monitor = Monitor(
            client=FakeClient(_balance("10000")),
            redis=redis,
            settings=MonitorSettings(cancel_rate_threshold=0.2),
            alerter=EmailAlerter(SmtpSettings()),
            store=store,
        )
        alerts = await monitor.run_once()
        assert any(a.rule == "cancel_rate" for a in alerts)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cancel_rate_no_alert_below_threshold(redis, tmp_path) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    try:
        _seed_orders(store, ["filled", "filled", "filled", "canceled"])
        monitor = Monitor(
            client=FakeClient(_balance("10000")),
            redis=redis,
            settings=MonitorSettings(cancel_rate_threshold=0.5),
            alerter=EmailAlerter(SmtpSettings()),
            store=store,
        )
        alerts = await monitor.run_once()
        assert not any(a.rule == "cancel_rate" for a in alerts)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_drawdown_tracks_peak(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    client = FakeClient(_balance("10000"))
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
    )
    await monitor.run_once()  # peak = 10000
    assert monitor.metrics.max_drawdown == Decimal("0")

    client.balance = _balance("9000")  # 回撤 10%
    await monitor.run_once()
    assert monitor.metrics.max_drawdown == Decimal("0.1")

    client.balance = _balance("9500")  # 恢复到 5% 回撤
    await monitor.run_once()
    assert monitor.metrics.max_drawdown == Decimal("0.05")


# =====================================================================
# 连续亏损 + API 错误率（F5.4）
# =====================================================================


def _seed_loss_fills(store: SQLiteStore, rounds: int) -> None:
    """写入 `rounds` 组 buy 高价 + sell 低价 的亏损回合。"""
    base = 1_700_000_000_000
    for i in range(rounds):
        for j, (side, price) in enumerate([("buy", "100"), ("sell", "90")]):
            store.save_fill(
                Fill(
                    order_id=f"fill-{i}-{j}",
                    inst_id="BTC-USDT",
                    side=side,
                    qty=Decimal("1"),
                    price=Decimal(price),
                    fee=Decimal("0"),
                    ts=base + i * 10 + j,
                )
            )


@pytest.mark.asyncio
async def test_consecutive_losses_alert(redis, tmp_path) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    try:
        _seed_loss_fills(store, 3)
        monitor = Monitor(
            client=FakeClient(_balance("10000")),
            redis=redis,
            settings=MonitorSettings(consecutive_loss_threshold=3),
            alerter=EmailAlerter(SmtpSettings()),
            store=store,
        )
        alerts = await monitor.run_once()
        assert any(a.rule == "consecutive_losses" for a in alerts)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_consecutive_losses_no_alert(redis, tmp_path) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    try:
        _seed_loss_fills(store, 2)
        monitor = Monitor(
            client=FakeClient(_balance("10000")),
            redis=redis,
            settings=MonitorSettings(consecutive_loss_threshold=3),
            alerter=EmailAlerter(SmtpSettings()),
            store=store,
        )
        alerts = await monitor.run_once()
        assert not any(a.rule == "consecutive_losses" for a in alerts)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_api_error_rate_alert(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    client = FakeClient(_balance("10000"))
    client._error_rate = 0.8
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(api_error_rate_threshold=0.5),
        alerter=EmailAlerter(SmtpSettings()),
    )
    alerts = await monitor.run_once()
    assert any(a.rule == "api_error_rate" for a in alerts)


@pytest.mark.asyncio
async def test_api_error_rate_none_no_alert(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor = _monitor(redis, _balance("10000"))
    alerts = await monitor.run_once()
    assert not any(a.rule == "api_error_rate" for a in alerts)


@pytest.mark.asyncio
async def test_kill_switch_alert(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    ks = KillSwitch(redis)
    await ks.enable("panic")
    monitor = _monitor(redis, _balance("10000"))
    alerts = await monitor.run_once()
    assert any(a.rule == "kill_switch" for a in alerts)


@pytest.mark.asyncio
async def test_kill_switch_no_alert_when_inactive(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor = _monitor(redis, _balance("10000"))
    alerts = await monitor.run_once()
    assert not any(a.rule == "kill_switch" for a in alerts)


@pytest.mark.asyncio
async def test_kill_switch_dedupes_while_active(redis) -> None:
    """持续激活只报一次；原因变化、解除后重新激活才新增。"""
    import time

    ks = KillSwitch(redis)
    monitor = _monitor(redis, _balance("10000"))

    async def round_():
        await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
        return await monitor.run_once()

    await ks.enable("panic")
    first = await round_()
    assert len([a for a in first if a.rule == "kill_switch"]) == 1

    # 同一原因的第二轮：已经在上一轮报过，不再重复
    assert not any(a.rule == "kill_switch" for a in await round_())

    # 原因变了 → 视为新的激活事件
    await ks.enable("changed")
    third = await round_()
    assert len([a for a in third if a.rule == "kill_switch"]) == 1

    # 解除 → 恢复通知；再次激活重新报
    await ks.disable()
    assert any(
        a.rule == "kill_switch" and a.level == "INFO" for a in await round_()
    )
    await ks.enable("again")
    fifth = await round_()
    assert any(
        a.rule == "kill_switch" and a.level == "ERROR" for a in fifth
    )


@pytest.mark.asyncio
async def test_min_liq_distance(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    client = FakeClient(_balance("10000"))
    client.instruments["BTC-USDT-SWAP"] = _swap_instrument()
    client.positions["SWAP"] = [
        {
            "instId": "BTC-USDT-SWAP",
            "pos": "1",
            "avgPx": "80000",
            "markPx": "82000",
            "liqPx": "77000",
        }
    ]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
    )
    await monitor.run_once()
    assert monitor.metrics.min_liq_distance == Decimal("5000") / Decimal("82000")


@pytest.mark.asyncio
async def test_min_liq_distance_none_without_swap(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor = _monitor(redis, _balance("10000"))
    await monitor.run_once()
    assert monitor.metrics.min_liq_distance is None


# =====================================================================
# 合约单位统一（P3）：快照里的数量一律是基础币
# =====================================================================


def _swap_monitor(redis, positions: list[dict], instruments) -> Monitor:
    client = FakeClient(_balance("10000"))
    client.positions["SWAP"] = positions
    client.instruments.update(instruments)
    return Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
    )


@pytest.mark.asyncio
async def test_contract_position_converts_contracts_to_base(redis) -> None:
    """`pos` 是张数：快照数量要把 ctVal 乘回来，与本地记账同口径。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor = _swap_monitor(
        redis,
        [
            {
                "instId": "BTC-USDT-SWAP",
                "pos": "5",
                "avgPx": "80000",
                "markPx": "80000",
                "upl": "0",
            }
        ],
        {"BTC-USDT-SWAP": _swap_instrument(ct_val="0.01")},
    )

    await monitor.run_once()

    assert len(monitor.positions) == 1
    assert monitor.positions[0].qty == Decimal("0.05")


@pytest.mark.asyncio
async def test_contract_position_skipped_without_ct_val(redis) -> None:
    """查不到面值时跳过该行：张数当基础币会让数量差 ctVal 倍。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor = _swap_monitor(
        redis,
        [
            {
                "instId": "BTC-USDT-SWAP",
                "pos": "5",
                "avgPx": "80000",
                "markPx": "80000",
            }
        ],
        {},
    )

    await monitor.run_once()

    assert monitor.positions == []


@pytest.mark.asyncio
async def test_spot_position_keeps_raw_qty(redis) -> None:
    """现货没有面值概念：单连字符品种不做换算。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    client = FakeClient(_spot_balance("BTC", "0.5", "30000", avg="80000"))
    client.tickers["BTC-USDT"] = [{"last": "80000"}]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
    )

    await monitor.run_once()

    assert monitor.positions[0].qty == Decimal("0.5")
    assert monitor.positions[0].unrealized_pnl == Decimal("0")


# =====================================================================
# 现货持仓对账（F5.2 补全）
# =====================================================================


def _spot_balance(ccy: str, qty: str, usdt: str = "0", avg: str = "") -> dict:
    """账户余额：USDT 现金 + 一个现货币种。

    `avg` 对应 OKX 的 `openAvgPx`（当前持仓均价），空字符串表示交易所没给。
    """
    return {
        "totalEq": "10000",
        "details": [
            {"ccy": "USDT", "cashBal": usdt, "availBal": usdt},
            {
                "ccy": ccy,
                "cashBal": qty,
                "eq": qty,
                "availBal": qty,
                "openAvgPx": avg,
            },
        ],
    }


@pytest.mark.asyncio
async def test_spot_position_uses_ticker_and_local_cost(redis, tmp_path):
    """现货持仓：数量取余额，标记价取 ticker，成本基数取本地记账。"""
    import time

    from quant.backtest.position import Position

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    store.update_position(
        Position(
            inst_id="BTC-USDT",
            qty=Decimal("0.5"),
            avg_price=Decimal("60000"),
        )
    )
    client = FakeClient(_spot_balance("BTC", "0.5", "30000"))
    client.tickers["BTC-USDT"] = [{"last": "80000"}]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
        store=store,
    )

    await monitor.run_once()

    by_inst = {p.inst_id: p for p in monitor.positions}
    spot = by_inst["BTC-USDT"]
    assert spot.qty == Decimal("0.5")
    assert spot.mark_price == Decimal("80000")
    assert spot.avg_price == Decimal("60000")
    # (80000 - 60000) * 0.5
    assert spot.unrealized_pnl == Decimal("10000")
    assert spot.liq_price is None
    assert spot.leverage == Decimal("1")
    store.close()


@pytest.mark.asyncio
async def test_spot_position_without_local_cost(redis):
    """本地无成本基数时不编造：均价记 0、未实现盈亏记 0。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    client = FakeClient(_spot_balance("ETH", "2", "30000"))
    client.tickers["ETH-USDT"] = [{"last": "3000"}]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
    )

    await monitor.run_once()

    by_inst = {p.inst_id: p for p in monitor.positions}
    spot = by_inst["ETH-USDT"]
    assert spot.qty == Decimal("2")
    assert spot.mark_price == Decimal("3000")
    assert spot.avg_price == Decimal("0")
    assert spot.unrealized_pnl == Decimal("0")


@pytest.mark.asyncio
async def test_spot_position_exceeding_local_cost_is_unknown(redis, tmp_path):
    """交易所余额大于本地数量：多出部分成本未知，不得用本地均价混算盈亏。

    复现实盘：账户有 0.7663 BTC（多为外部转入），策略只记账了 0.001。
    按「本地均价 × 全量余额」会得到虚高的未实现盈亏。
    """
    import time

    from quant.backtest.position import Position

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    store.update_position(
        Position(
            inst_id="BTC-USDT",
            qty=Decimal("0.001"),
            avg_price=Decimal("80573.8"),
        )
    )
    client = FakeClient(_spot_balance("BTC", "0.76633774", "30000"))
    client.tickers["BTC-USDT"] = [{"last": "80665.4"}]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
        store=store,
    )

    await monitor.run_once()

    spot = {p.inst_id: p for p in monitor.positions}["BTC-USDT"]
    assert spot.qty == Decimal("0.76633774")
    assert spot.mark_price == Decimal("80665.4")
    # 成本基数与盈亏都留空，而不是 (80665.4 - 80573.8) * 0.7663 ≈ 70.20
    assert spot.avg_price == Decimal("0")
    assert spot.unrealized_pnl == Decimal("0")
    store.close()


@pytest.mark.asyncio
async def test_spot_position_prefers_exchange_open_avg_px(redis, tmp_path):
    """交易所给了 openAvgPx 就采用，哪怕本地记账覆盖不了全量余额。

    复现实盘 BTC：0.7663 枚里只有 0.001 是策略买的，本地均价 80,573.80
    不能拿来乘全量；交易所的当前持仓均价 80,279.82 才是账户口径成本。
    """
    import time

    from quant.backtest.position import Position

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    store.update_position(
        Position(
            inst_id="BTC-USDT",
            qty=Decimal("0.001"),
            avg_price=Decimal("80573.8"),
        )
    )
    client = FakeClient(
        _spot_balance("BTC", "0.76633774", "30000", avg="80279.82184511068")
    )
    client.tickers["BTC-USDT"] = [{"last": "80597.3"}]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
        store=store,
    )

    await monitor.run_once()

    spot = {p.inst_id: p for p in monitor.positions}["BTC-USDT"]
    qty = Decimal("0.76633774")
    avg = Decimal("80279.82184511068")
    assert spot.qty == qty
    assert spot.avg_price == avg
    assert spot.unrealized_pnl == (Decimal("80597.3") - avg) * qty
    store.close()


@pytest.mark.asyncio
async def test_spot_position_invalid_open_avg_px_falls_back_to_local(redis, tmp_path):
    """openAvgPx 缺失或非法时回退本地记账。"""
    import time

    from quant.backtest.position import Position

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    store.update_position(
        Position(
            inst_id="BTC-USDT",
            qty=Decimal("0.5"),
            avg_price=Decimal("60000"),
        )
    )
    client = FakeClient(_spot_balance("BTC", "0.5", "30000", avg="not-a-number"))
    client.tickers["BTC-USDT"] = [{"last": "80000"}]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
        store=store,
    )

    await monitor.run_once()

    spot = {p.inst_id: p for p in monitor.positions}["BTC-USDT"]
    assert spot.avg_price == Decimal("60000")
    assert spot.unrealized_pnl == Decimal("10000")
    store.close()


@pytest.mark.asyncio
async def test_spot_position_covered_by_local_cost(redis, tmp_path):
    """交易所余额不超过本地数量时，本地均价可覆盖，正常计算盈亏。"""
    import time

    from quant.backtest.position import Position

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    store.update_position(
        Position(
            inst_id="BTC-USDT",
            qty=Decimal("0.3"),
            avg_price=Decimal("70000"),
        )
    )
    # 本地记账多于交易所余额（外部卖出过一部分），剩余仍按均价计成本
    client = FakeClient(_spot_balance("BTC", "0.2", "30000"))
    client.tickers["BTC-USDT"] = [{"last": "80000"}]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
        store=store,
    )

    await monitor.run_once()

    spot = {p.inst_id: p for p in monitor.positions}["BTC-USDT"]
    assert spot.qty == Decimal("0.2")
    assert spot.avg_price == Decimal("70000")
    assert spot.unrealized_pnl == Decimal("2000")
    store.close()


@pytest.mark.asyncio
async def test_spot_position_skips_pair_without_ticker(redis):
    """没有对应 {ccy}-USDT 行情的余额（如理财份额）不计入持仓。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    client = FakeClient(_spot_balance("OKB", "100", "30000"))
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
    )

    await monitor.run_once()

    assert monitor.positions == []


@pytest.mark.asyncio
async def test_spot_position_disabled_by_empty_quote(redis):
    """spot_quote_ccy 置空即关闭现货对账。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    client = FakeClient(_spot_balance("BTC", "0.5", "30000"))
    client.tickers["BTC-USDT"] = [{"last": "80000"}]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(spot_quote_ccy=""),
        alerter=EmailAlerter(SmtpSettings()),
    )

    await monitor.run_once()

    assert monitor.positions == []


@pytest.mark.asyncio
async def test_spot_ticker_failure_does_not_break_cycle(redis):
    """行情失败只跳过该币种，不拖垮整轮（快照仍发布）。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    client = FakeClient(_spot_balance("BTC", "0.5", "30000"))

    def boom(path, params=None):
        raise RuntimeError("ticker down")

    client.get = boom  # type: ignore[method-assign]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
    )

    alerts = await monitor.run_once()

    assert alerts == []
    assert monitor.positions == []


# =====================================================================
# 持仓漂移检测（P1）
# =====================================================================


def _drift_monitor(redis, tmp_path, local_qty: str, exchange_qty: str | None):
    """构造「本地记账 local_qty、交易所余额 exchange_qty」的 monitor。

    `exchange_qty=None` 表示交易所完全没有该币（快照里不出现）。
    """
    from quant.backtest.position import Position

    store = SQLiteStore(tmp_path / "quant.db")
    store.update_position(
        Position(
            inst_id="BTC-USDT",
            qty=Decimal(local_qty),
            avg_price=Decimal("80000"),
        )
    )
    balance = (
        _balance("10000")
        if exchange_qty is None
        else _spot_balance("BTC", exchange_qty, "30000")
    )
    client = FakeClient(balance)
    client.tickers["BTC-USDT"] = [{"last": "80000"}]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
        store=store,
    )
    return monitor, store


@pytest.mark.asyncio
async def test_position_drift_alerts_when_local_exceeds_exchange(
    redis, tmp_path
) -> None:
    """本地记账多于交易所余额：卖出会因可用量不足被拒，必须告警。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor, store = _drift_monitor(redis, tmp_path, "0.5", "0.4")

    alerts = await monitor.run_once()

    drift = [a for a in alerts if a.rule == "position_drift"]
    assert len(drift) == 1
    assert drift[0].level == "WARNING"
    assert "0.5" in drift[0].message and "0.4" in drift[0].message
    store.close()


@pytest.mark.asyncio
async def test_position_drift_covers_contracts(redis, tmp_path) -> None:
    """合约也按基础币比对：本地 0.05 张折 0.04 即为漂移。"""
    import time

    from quant.backtest.position import Position

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    store = SQLiteStore(tmp_path / "quant.db")
    store.update_position(
        Position(
            inst_id="BTC-USDT-SWAP",
            qty=Decimal("0.05"),
            avg_price=Decimal("80000"),
        )
    )
    client = FakeClient(_balance("10000"))
    client.instruments["BTC-USDT-SWAP"] = _swap_instrument(ct_val="0.01")
    client.positions["SWAP"] = [
        {
            "instId": "BTC-USDT-SWAP",
            "pos": "4",
            "avgPx": "80000",
            "markPx": "80000",
            "upl": "0",
        }
    ]
    monitor = Monitor(
        client=client,
        redis=redis,
        settings=MonitorSettings(),
        alerter=EmailAlerter(SmtpSettings()),
        store=store,
    )

    alerts = await monitor.run_once()

    drift = [a for a in alerts if a.rule == "position_drift"]
    assert len(drift) == 1
    assert "BTC-USDT-SWAP" in drift[0].message
    store.close()


@pytest.mark.asyncio
async def test_position_drift_silent_when_exchange_has_more(redis, tmp_path) -> None:
    """交易所余额多于本地记账是常态（外部转入），不告警。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor, store = _drift_monitor(redis, tmp_path, "0.001", "0.76633774")

    alerts = await monitor.run_once()

    assert [a for a in alerts if a.rule == "position_drift"] == []
    store.close()


@pytest.mark.asyncio
async def test_position_drift_within_tolerance_silent(redis, tmp_path) -> None:
    """差额在容差（默认 0.5%）内不告警，避免手续费级别的抖动刷屏。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    # 本地 0.001，交易所 0.000999 -> 超出 0.1%
    monitor, store = _drift_monitor(redis, tmp_path, "0.001", "0.000999")

    alerts = await monitor.run_once()

    assert [a for a in alerts if a.rule == "position_drift"] == []
    store.close()


@pytest.mark.asyncio
async def test_position_drift_alerts_when_exchange_has_none(
    redis, tmp_path
) -> None:
    """交易所已无该币、本地仍有记账：属于完全漂移（如手工提币）。"""
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor, store = _drift_monitor(redis, tmp_path, "0.001", None)

    alerts = await monitor.run_once()

    drift = [a for a in alerts if a.rule == "position_drift"]
    assert len(drift) == 1
    assert "0.001" in drift[0].message
    store.close()


# =====================================================================
# engine 心跳 / 恢复通知 / 存活邮件
# =====================================================================

ENGINE_KEY = "quant:engine:status:sma_cross:BTC-USDT"


class RecordingAlerter:
    """记录发送内容的假 alerter（不真的发信）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def enabled(self) -> bool:
        return True

    def send(self, subject: str, body: str) -> bool:
        self.sent.append((subject, body))
        return True


def _engine_status(ts: int) -> str:
    return json.dumps({"ts": ts, "status": "no_signal", "bar_ts": ts})


@pytest.mark.asyncio
async def test_engine_heartbeat_missing(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    monitor = _monitor(
        redis, _balance("10000"), engine_status_key=ENGINE_KEY
    )
    alerts = await monitor.run_once()
    engine = [a for a in alerts if a.rule == "engine_heartbeat"]
    assert engine and "从未上报" in engine[0].message


@pytest.mark.asyncio
async def test_engine_heartbeat_stale(redis) -> None:
    import time

    now = int(time.time() * 1000)
    await redis.set("quant:heartbeat:worker", str(now))
    await redis.set(ENGINE_KEY, _engine_status(now - 600_000))
    monitor = _monitor(
        redis, _balance("10000"), engine_status_key=ENGINE_KEY
    )
    alerts = await monitor.run_once()
    engine = [a for a in alerts if a.rule == "engine_heartbeat"]
    assert engine and "陈旧" in engine[0].message


@pytest.mark.asyncio
async def test_engine_heartbeat_fresh(redis) -> None:
    import time

    now = int(time.time() * 1000)
    await redis.set("quant:heartbeat:worker", str(now))
    await redis.set(ENGINE_KEY, _engine_status(now))
    monitor = _monitor(
        redis, _balance("10000"), engine_status_key=ENGINE_KEY
    )
    alerts = await monitor.run_once()
    assert not any(a.rule == "engine_heartbeat" for a in alerts)


@pytest.mark.asyncio
async def test_engine_heartbeat_unparsable(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    await redis.set(ENGINE_KEY, "not-json")
    monitor = _monitor(
        redis, _balance("10000"), engine_status_key=ENGINE_KEY
    )
    alerts = await monitor.run_once()
    engine = [a for a in alerts if a.rule == "engine_heartbeat"]
    assert engine and "无法解析" in engine[0].message


@pytest.mark.asyncio
async def test_recovery_notice_emitted_once(redis) -> None:
    import time

    stale = int(time.time() * 1000) - 600_000
    await redis.set("quant:heartbeat:worker", str(stale))
    monitor = _monitor(redis, _balance("10000"))

    first = await monitor.run_once()
    assert any(a.rule == "worker_heartbeat" for a in first)
    assert not any(a.level == "INFO" for a in first)

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    second = await monitor.run_once()
    recovery = [a for a in second if a.level == "INFO"]
    assert len(recovery) == 1
    assert recovery[0].rule == "worker_heartbeat"
    assert "已恢复" in recovery[0].message

    third = await monitor.run_once()
    assert not any(a.level == "INFO" for a in third)


@pytest.mark.asyncio
async def test_suspend_gap_detects_long_freeze(redis) -> None:
    import time

    monitor = _monitor(redis, _balance("10000"))
    now = int(time.time() * 1000)

    # 首轮没有基准，不判挂起
    assert monitor._take_suspend_gap(now) == 0
    # 正常 60s 间隔不判挂起
    assert monitor._take_suspend_gap(now + 60_000) == 0
    # 间隔 22 分钟（宿主机休眠）判挂起，返回扣除一个正常周期后的时长
    gap = monitor._take_suspend_gap(now + 60_000 + 1_320_000)
    assert gap == 1_260_000


@pytest.mark.asyncio
async def test_suspended_round_skips_heartbeat_alerts(redis) -> None:
    """系统挂起恢复的那一轮：不报心跳 ERROR，也不补发假的「已恢复」。"""
    import time

    stale = int(time.time() * 1000) - 1_320_000
    await redis.set("quant:heartbeat:worker", str(stale))
    await redis.set(
        "quant:engine:status:sma_cross:BTC-USDT",
        json.dumps({"ts": stale, "status": "no_signal"}),
    )
    monitor = _monitor(
        redis,
        _balance("10000"),
        engine_status_key="quant:engine:status",
    )

    first = await monitor.run_once()
    assert any(a.rule == "worker_heartbeat" for a in first)
    assert any(a.rule == "engine_heartbeat" for a in first)

    # 模拟整机被冻结 22 分钟：把上一轮时间戳往前拨
    monitor._last_cycle_ts = int(time.time() * 1000) - 1_320_000
    await redis.set("quant:heartbeat:worker", str(stale))
    second = await monitor.run_once()

    assert not any(a.rule in ("worker_heartbeat", "engine_heartbeat") for a in second)
    assert not any(a.level == "INFO" for a in second)
    # 误报状态已丢弃，下一轮心跳正常时不会冒出「已恢复」
    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    third = await monitor.run_once()
    assert not any(a.rule == "worker_heartbeat" for a in third)


@pytest.mark.asyncio
async def test_recovery_notice_bypasses_cooldown(redis) -> None:
    import time

    monitor = _monitor(redis, _balance("10000"))
    now = int(time.time() * 1000)
    await monitor._publish_alerts(
        [Alert("worker_heartbeat", "ERROR", "心跳丢失", now)]
    )
    await monitor._publish_alerts(
        [Alert("worker_heartbeat", "INFO", "已恢复", now + 1)]
    )
    kept = await redis.llen("quant:dashboard:alerts")
    assert kept == 2


@pytest.mark.asyncio
async def test_alive_email_persists_across_restart(redis) -> None:
    import time

    await redis.set("quant:heartbeat:worker", str(int(time.time() * 1000)))
    first = RecordingAlerter()
    monitor = Monitor(
        client=FakeClient(_balance("10000")),
        redis=redis,
        settings=MonitorSettings(),
        alerter=first,
    )
    await monitor.run_once()
    assert len(first.sent) == 1

    # 模拟重启：进程内计数归零，但 Redis 时间戳仍在，不应重复发
    second = RecordingAlerter()
    restarted = Monitor(
        client=FakeClient(_balance("10000")),
        redis=redis,
        settings=MonitorSettings(),
        alerter=second,
    )
    await restarted.run_once()
    assert second.sent == []


# =====================================================================
# 冷却迁 Redis + AlertService 接入（MS4-A §5.4 / §7）
# =====================================================================


@pytest.mark.asyncio
async def test_cooldown_survives_restart(redis) -> None:
    """冷却放 Redis：monitor 重启后不会立刻把同一规则再报一遍。"""
    import time

    now = int(time.time() * 1000)
    first = _monitor(redis, _balance("10000"))
    await first._publish_alerts([Alert("margin_ratio", "ERROR", "低", now)])
    assert await redis.llen("quant:dashboard:alerts") == 1

    # 新实例：进程内状态全部丢失，只剩 Redis
    restarted = _monitor(redis, _balance("10000"))
    await restarted._publish_alerts([Alert("margin_ratio", "ERROR", "低", now + 1)])

    assert await redis.llen("quant:dashboard:alerts") == 1


@pytest.mark.asyncio
async def test_cooldown_is_per_instrument(redis) -> None:
    """多品种下 BTC 的告警不能吞掉 ETH 的同类告警。"""
    import time

    now = int(time.time() * 1000)
    monitor = _monitor(redis, _balance("10000"))

    await monitor._publish_alerts(
        [Alert("position_drift", "ERROR", "btc", now, inst_id="BTC-USDT")]
    )
    await monitor._publish_alerts(
        [Alert("position_drift", "ERROR", "eth", now + 1, inst_id="ETH-USDT")]
    )
    await monitor._publish_alerts(
        [Alert("position_drift", "ERROR", "btc again", now + 2, inst_id="BTC-USDT")]
    )

    assert await redis.llen("quant:dashboard:alerts") == 2


@pytest.mark.asyncio
async def test_publish_writes_alert_to_sqlite(redis, tmp_path) -> None:
    import time

    from quant.state.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path / "quant.db")
    now = int(time.time() * 1000)
    try:
        settings = MonitorSettings()
        monitor = Monitor(
            client=FakeClient(_balance("10000")),
            redis=redis,
            settings=settings,
            alerter=EmailAlerter(SmtpSettings()),
            alert_service=AlertService(
                store, redis, EmailAlerter(SmtpSettings()), settings
            ),
        )

        await monitor._publish_alerts(
            [Alert("margin_ratio", "ERROR", "保证金率过低", now)]
        )

        rows, total = store.list_alerts(10)
        assert total == 1
        assert rows[0]["rule"] == "margin_ratio"
        assert rows[0]["state"] == "open"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_recovery_resolves_open_alerts(redis, tmp_path) -> None:
    import time

    from quant.state.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path / "quant.db")
    now = int(time.time() * 1000)
    try:
        settings = MonitorSettings()
        service = AlertService(
            store, redis, EmailAlerter(SmtpSettings()), settings
        )
        monitor = Monitor(
            client=FakeClient(_balance("10000")),
            redis=redis,
            settings=settings,
            alerter=EmailAlerter(SmtpSettings()),
            alert_service=service,
        )

        await monitor._publish_alerts(
            [Alert("margin_ratio", "ERROR", "保证金率过低", now)]
        )
        await monitor._publish_alerts(
            [Alert("margin_ratio", "INFO", "已恢复", now + 1)]
        )

        rows, _ = store.list_alerts(10)
        states = {r["level"]: r["state"] for r in rows}
        # ERROR 被恢复通知关闭；INFO 本身不是「待处理问题」，同样立即 resolved
        assert states == {"ERROR": "resolved", "INFO": "resolved"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_recovery_keeps_inst_scope(redis, tmp_path) -> None:
    """恢复通知要带上 inst_id，否则只会关掉 inst_id IS NULL 的记录。"""
    import time

    from quant.state.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path / "quant.db")
    now = int(time.time() * 1000)
    try:
        settings = MonitorSettings()
        service = AlertService(
            store, redis, EmailAlerter(SmtpSettings()), settings
        )
        monitor = Monitor(
            client=FakeClient(_balance("10000")),
            redis=redis,
            settings=settings,
            alerter=EmailAlerter(SmtpSettings()),
            alert_service=service,
        )
        alert = Alert(
            "position_drift", "ERROR", "btc drift", now, inst_id="BTC-USDT"
        )
        monitor._active_rules = {"position_drift": alert}

        recovered = monitor._with_recovery([], now + 1)
        assert recovered[0].inst_id == "BTC-USDT"
        await monitor._publish_alerts(recovered)

        # 恢复通知带 inst_id → 关掉的是 BTC-USDT 那条，而不是全 NULL 记录
        rows, _ = store.list_alerts(10)
        assert {r["state"] for r in rows} == {"resolved"}
        assert {r["inst_id"] for r in rows} == {"BTC-USDT"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_service_email_is_per_alert(redis, tmp_path) -> None:
    """接入 AlertService 后邮件逐条发送，不再一轮聚合一封。"""
    import time

    from quant.state.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path / "quant.db")
    now = int(time.time() * 1000)
    alerter = RecordingAlerter()
    try:
        settings = MonitorSettings()
        service = AlertService(store, redis, alerter, settings)
        monitor = Monitor(
            client=FakeClient(_balance("10000")),
            redis=redis,
            settings=settings,
            alerter=alerter,
            alert_service=service,
        )

        await monitor._publish_alerts(
            [
                Alert("margin_ratio", "ERROR", "低", now),
                Alert("queue_backlog", "WARNING", "堆积", now + 1),
            ]
        )

        assert len(alerter.sent) == 2
    finally:
        store.close()

