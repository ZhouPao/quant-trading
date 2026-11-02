"""本地策略实时信号引擎测试。

覆盖：
- 数据未就绪（无本地 K线 / 落后于最近已收盘 bar）
- 产生信号并入队、qty 以账户可用 USDT 计算
- 同一根 bar 只处理一次（Redis 水位）
- kill switch / 未完成挂单时跳过
- 快照缺失时用兜底资金
- 策略配置缺失时报错不推进水位
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import fakeredis.aioredis
import pytest

from quant.backtest.order import Order
from quant.backtest.position import Position
from quant.backtest.signal import Signal
from quant.engine.live import LiveSignalEngine
from quant.execution.kill_switch import KillSwitch
from quant.foundation.config import OKXSettings, Settings
from quant.models import Bar
from quant.queue.signal_queue import SignalQueue
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage

HOUR = 3_600_000
LAST_TS = (1_700_000_000_000 // HOUR) * HOUR
NOW = LAST_TS + HOUR  # 此时 LAST_TS 那根刚收盘


def _bars(count: int = 60, last_close: str = "120") -> list[Bar]:
    """前 count-1 根横盘 100，最后一根跳升（制造金叉）。"""
    bars = []
    for i in range(count):
        price = Decimal(last_close) if i == count - 1 else Decimal("100")
        bars.append(
            Bar(
                ts=LAST_TS - (count - 1 - i) * HOUR,
                open=price,
                high=price,
                low=price,
                close=price,
                vol=Decimal("10"),
                vol_quote=price * 10,
                confirm=1,
                inst_id="BTC-USDT",
                bar_size="1H",
            )
        )
    return bars


def _settings(
    tmp_path: Path,
    *,
    cash_fallback: str = "5000",
    max_catchup_bars: int = 24,
) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p"),
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "data" / "state" / "quant.db"},
        backtest={"window_size": 200},
        # 本文件的合成 K 线用大幅振幅制造金叉/破位（部分 bar 达 24%），
        # 调高熔断阈值避免干扰引擎行为用例；熔断本身由 test_circuit_breaker 覆盖
        risk={"circuit_amplitude_pct": "1.0"},
        engine={
            "strategy": "sma_cross",
            "inst": "BTC-USDT",
            "bar": "1H",
            "warmup_bars": 200,
            "max_catchup_bars": max_catchup_bars,
            "config_dir": tmp_path / "strategies",
            "cash_fallback": cash_fallback,
        },
        monitor={"dashboard_snapshot_key": "quant:dashboard:snapshot"},
    )


def _write_strategy_config(tmp_path: Path) -> None:
    cfg = tmp_path / "strategies"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "sma_cross.toml").write_text(
        "[strategy]\n"
        'strategy_id = "sma_cross"\n'
        'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n\n'
        "[params]\nfast_len = 10\nslow_len = 50\n"
    )


@pytest.fixture
def env(tmp_path):
    """Settings + 真实 store + fakeredis + 含 K线的 parquet。"""
    settings = _settings(tmp_path)
    _write_strategy_config(tmp_path)
    BarStorage(settings.storage.data_dir).save_bars(_bars())
    store = SQLiteStore(settings.state.db_path)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield settings, store, redis
    store.close()


def _engine(settings, store, redis) -> LiveSignalEngine:
    return LiveSignalEngine(
        settings,
        store,
        redis,
        queue=SignalQueue(
            redis,
            pending_key=settings.redis.queue_key,
            inflight_key=settings.redis.inflight_key,
            dead_key=settings.redis.dead_key,
        ),
    )


async def _queue_items(settings, redis) -> list[str]:
    return await redis.lrange(settings.redis.queue_key, 0, -1)


class TestWaitingForData:
    async def test_no_local_bars(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        _write_strategy_config(tmp_path)
        store = SQLiteStore(settings.state.db_path)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        try:
            tick = await _engine(settings, store, redis).run_once(now_ms=NOW)
        finally:
            store.close()
        assert tick.status == "waiting_data"
        assert tick.reason == "no local bars"

    async def test_bars_behind_last_closed(self, env) -> None:
        settings, store, redis = env
        # 本地最后一根是 LAST_TS，但此刻已到下一个小时，数据还没同步
        tick = await _engine(settings, store, redis).run_once(
            now_ms=NOW + HOUR
        )
        assert tick.status == "waiting_data"
        assert "behind" in tick.reason

    async def test_unclosed_bar_ignored(self, env) -> None:
        """库里混入未收盘 bar 时，评估仍落在最近已收盘 bar 上。"""
        settings, store, redis = env
        BarStorage(settings.storage.data_dir).save_bars(_bars(61))

        tick = await _engine(settings, store, redis).run_once(now_ms=NOW)

        assert tick.bar_ts == LAST_TS


class TestParamSource:
    """引擎与回测共用 resolve_strategy：DB 覆盖必须对实盘生效（MS4-C §3.2）。"""

    async def _rerun(self, settings, store, redis):
        # 清掉水位，否则同一根 bar 会被判 duplicate
        await redis.delete(f"{settings.engine.last_bar_key}:sma_cross:BTC-USDT")
        return await _engine(settings, store, redis).run_once(now_ms=NOW)

    async def test_db_override_reaches_engine(self, env) -> None:
        settings, store, redis = env
        assert (await self._rerun(settings, store, redis)).status == "signal"

        # slow_len=200 后窗口不足 → 本根不产生信号
        store.set_strategy_param("sma_cross", "slow_len", "200", NOW)

        assert (await self._rerun(settings, store, redis)).status == "no_signal"

    async def test_rollback_switch_ignores_db(self, env) -> None:
        settings, store, redis = env
        store.set_strategy_param("sma_cross", "slow_len", "200", NOW)
        settings.params.resolve_from_db = False

        assert (await self._rerun(settings, store, redis)).status == "signal"

    async def test_invalid_db_override_does_not_break_engine(self, env) -> None:
        settings, store, redis = env
        store.set_strategy_param("sma_cross", "slow_len", "not-a-number", NOW)

        assert (await self._rerun(settings, store, redis)).status == "signal"


class TestSignal:
    async def test_signal_enqueued(self, env) -> None:
        settings, store, redis = env
        engine = _engine(settings, store, redis)

        tick = await engine.run_once(now_ms=NOW)

        assert tick.status == "signal"
        assert tick.bar_ts == LAST_TS
        assert tick.signal is not None
        assert tick.signal.side == "buy"
        assert tick.signal.strategy_id == "sma_cross"
        assert tick.signal.signal_id

        items = await _queue_items(settings, redis)
        assert len(items) == 1
        assert '"side":"buy"' in items[0]

        marker = await redis.get("quant:engine:last-bar:sma_cross:BTC-USDT")
        assert marker == str(LAST_TS)

    async def test_qty_uses_account_snapshot(self, env) -> None:
        settings, store, redis = env
        await redis.set(
            settings.monitor.dashboard_snapshot_key,
            '{"account": {"avail_usdt": "10000"}}',
        )

        tick = await _engine(settings, store, redis).run_once(now_ms=NOW)

        assert tick.signal is not None
        # SmaCross 全仓买入并预留 0.1% 手续费：10000 / (120 * 1.001)
        expected = Decimal("10000") / (Decimal("120") * Decimal("1.001"))
        assert abs(tick.signal.qty - expected) < Decimal("0.0001")

    async def test_qty_uses_fallback_when_snapshot_missing(self, env) -> None:
        settings, store, redis = env

        tick = await _engine(settings, store, redis).run_once(now_ms=NOW)

        assert tick.signal is not None
        expected = Decimal("5000") / (Decimal("120") * Decimal("1.001"))
        assert abs(tick.signal.qty - expected) < Decimal("0.0001")

    async def test_qty_uses_fallback_when_snapshot_malformed(self, env) -> None:
        settings, store, redis = env
        await redis.set(settings.monitor.dashboard_snapshot_key, "not-json")

        tick = await _engine(settings, store, redis).run_once(now_ms=NOW)

        assert tick.signal is not None
        assert tick.signal.qty > 0


class TestDeduplication:
    async def test_same_bar_processed_once(self, env) -> None:
        settings, store, redis = env
        engine = _engine(settings, store, redis)

        first = await engine.run_once(now_ms=NOW)
        second = await engine.run_once(now_ms=NOW + 30 * 60_000)

        assert first.status == "signal"
        assert second.status == "duplicate"
        assert len(await _queue_items(settings, redis)) == 1

    async def test_dedup_hit_does_not_enqueue(self, env) -> None:
        """水位丢失但信号指纹相同（重启）时不重复入队。"""
        settings, store, redis = env
        engine = _engine(settings, store, redis)

        first = await engine.run_once(now_ms=NOW)
        assert first.signal is not None
        await redis.delete("quant:engine:last-bar:sma_cross:BTC-USDT")

        second = await engine.run_once(now_ms=NOW)

        assert first.status == "signal"
        assert second.status == "duplicate"
        assert second.reason == "dedup hit"
        assert len(await _queue_items(settings, redis)) == 1

    async def test_no_signal_still_advances_marker(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        _write_strategy_config(tmp_path)
        BarStorage(settings.storage.data_dir).save_bars(
            _bars(last_close="100")
        )
        store = SQLiteStore(settings.state.db_path)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        try:
            tick = await _engine(settings, store, redis).run_once(now_ms=NOW)
        finally:
            store.close()

        assert tick.status == "no_signal"
        assert await redis.get("quant:engine:last-bar:sma_cross:BTC-USDT") == str(
            LAST_TS
        )


class TestSkips:
    async def test_kill_switch(self, env) -> None:
        settings, store, redis = env
        await KillSwitch(redis).enable("manual")

        tick = await _engine(settings, store, redis).run_once(now_ms=NOW)

        assert tick.status == "skipped"
        assert tick.reason == "kill switch active"
        assert await _queue_items(settings, redis) == []
        # 水位已推进，解除 kill switch 后不会补发这根旧信号
        assert await redis.get("quant:engine:last-bar:sma_cross:BTC-USDT") == str(
            LAST_TS
        )

    async def test_pending_order_for_inst(self, env) -> None:
        settings, store, redis = env
        sig = Signal(
            strategy_id="sma_cross",
            side="buy",
            order_type="market",
            inst_id="BTC-USDT",
            qty=Decimal("0.1"),
            ts=LAST_TS,
            signal_id="s1",
        )
        # 真实链路里 worker 先落信号再落订单，open_orders 才能 join 出 inst_id
        store.save_signal(sig, "received")
        store.save_order(
            Order(order_id="o1", signal=sig, status="pending", created_ts=LAST_TS)
        )

        tick = await _engine(settings, store, redis).run_once(now_ms=NOW)

        assert tick.status == "skipped"
        assert tick.reason == "pending orders"
        assert await _queue_items(settings, redis) == []

    async def test_pending_order_other_inst_does_not_block(self, env) -> None:
        settings, store, redis = env
        sig = Signal(
            strategy_id="sma_cross",
            side="buy",
            order_type="market",
            inst_id="ETH-USDT",
            qty=Decimal("0.1"),
            ts=LAST_TS,
            signal_id="s2",
        )
        store.save_signal(sig, "received")
        store.save_order(
            Order(order_id="o2", signal=sig, status="pending", created_ts=LAST_TS)
        )

        tick = await _engine(settings, store, redis).run_once(now_ms=NOW)

        assert tick.status == "signal"


class TestConfigErrors:
    async def test_missing_strategy_config(self, env) -> None:
        settings, store, redis = env
        (settings.engine.config_dir / "sma_cross.toml").unlink()

        tick = await _engine(settings, store, redis).run_once(now_ms=NOW)

        assert tick.status == "error"
        assert "not found" in tick.reason
        assert await redis.get("quant:engine:last-bar:sma_cross:BTC-USDT") is None

    def test_invalid_bar_rejected(self, env) -> None:
        settings, store, redis = env
        settings.engine.bar = "2S"
        with pytest.raises(ValueError, match="engine.bar invalid"):
            LiveSignalEngine(settings, store, redis)


def _wave_bars(pivot: int = 55, total: int = 66) -> list[Bar]:
    """横盘 100 → 第 pivot 根跳升 120（金叉）→ 回落（pivot+10 处死叉）。

    fast_len=10 的窗口在第 pivot+10 根移出跳升值，此时 fast 下穿 slow。
    """
    bars = []
    for i in range(total):
        price = Decimal("120") if i == pivot else Decimal("100")
        bars.append(
            Bar(
                ts=LAST_TS - (total - 1 - i) * HOUR,
                open=price,
                high=price,
                low=price,
                close=price,
                vol=Decimal("10"),
                vol_quote=price * 10,
                confirm=1,
                inst_id="BTC-USDT",
                bar_size="1H",
            )
        )
    return bars


class TestCatchUp:
    """水位之后漏掉的 bar 要逐根补评估，且策略状态连续。"""

    async def _prepare(
        self, tmp_path: Path, *, max_catchup_bars: int = 24, marker_at: int = 54
    ):
        settings = _settings(tmp_path, max_catchup_bars=max_catchup_bars)
        _write_strategy_config(tmp_path)
        bars = _wave_bars()
        BarStorage(settings.storage.data_dir).save_bars(bars)
        store = SQLiteStore(settings.state.db_path)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        if marker_at >= 0:
            # 水位停在金叉前一根，模拟金叉发生在进程休眠期间
            marker = (
                f"{settings.engine.last_bar_key}:"
                f"{settings.engine.strategy}:{settings.engine.inst}"
            )
            await redis.set(marker, str(bars[marker_at].ts))
        return settings, store, redis, bars

    async def test_replays_missed_crossovers(self, tmp_path) -> None:
        settings, store, redis, bars = await self._prepare(tmp_path)
        try:
            tick = await _engine(settings, store, redis).run_once(
                now_ms=bars[-1].ts + HOUR
            )

            assert tick.status == "signal"
            # 金叉买入 + 死叉平仓都要补发：虚拟持仓让死叉看到多头
            assert tick.signal_count == 2
            # 水位在 bars[54]，最新是 bars[65]：11 根待评估，10 根属于补评估
            assert tick.catch_up == 10
            assert tick.signal is not None
            assert tick.signal.side == "sell"

            items = await _queue_items(settings, redis)
            assert len(items) == 2
            # LPUSH 后进先出，队列头部是最新入队的死叉
            assert '"side":"sell"' in items[0]
            assert '"side":"buy"' in items[1]
        finally:
            store.close()
            await redis.aclose()

    async def test_first_run_does_not_backfill(self, tmp_path) -> None:
        """无水位时只评估最新一根，不能把窗口内的历史交叉全部补发。"""
        settings, store, redis, bars = await self._prepare(tmp_path, marker_at=-1)
        try:
            tick = await _engine(settings, store, redis).run_once(
                now_ms=bars[-1].ts + HOUR
            )

            # 最新一根是死叉，但账户空仓，不会产生卖出信号
            assert tick.status == "no_signal"
            assert await _queue_items(settings, redis) == []
        finally:
            store.close()
            await redis.aclose()

    async def test_catch_up_limit_drops_older_bars(self, tmp_path) -> None:
        """超出 max_catchup_bars 时丢弃更早的 bar，金叉因此不被补发。"""
        settings, store, redis, bars = await self._prepare(
            tmp_path, max_catchup_bars=5
        )
        try:
            tick = await _engine(settings, store, redis).run_once(
                now_ms=bars[-1].ts + HOUR
            )

            # 金叉在 pivot=55，被截断丢弃；只剩死叉且空仓，不触发
            assert tick.status == "no_signal"
            # 截断后只评估最后 5 根，其中 4 根属于补评估
            assert tick.catch_up == 4
            assert await _queue_items(settings, redis) == []
        finally:
            store.close()
            await redis.aclose()


class TestProtectiveExit:
    """支撑/压力止盈止损在补评估路径上也要生效。"""

    async def test_stop_out_enqueues_sell(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        cfg = tmp_path / "strategies"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "sma_cross.toml").write_text(
            "[strategy]\n"
            'strategy_id = "sma_cross"\n'
            'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n\n'
            "[params]\nfast_len = 10\nslow_len = 50\n"
            "sr_enabled = true\nsr_left_right = 2\n"
        )
        bars = _sr_bars()
        BarStorage(settings.storage.data_dir).save_bars(bars)
        store = SQLiteStore(settings.state.db_path)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        marker = (
            f"{settings.engine.last_bar_key}:"
            f"{settings.engine.strategy}:{settings.engine.inst}"
        )
        # 水位停在金叉前一根，让补评估覆盖金叉与随后的破位
        await redis.set(marker, str(bars[55].ts))
        try:
            tick = await _engine(settings, store, redis).run_once(
                now_ms=bars[-1].ts + HOUR
            )

            # 金叉买入 + 触及支撑 90 的止损卖出
            assert tick.status == "signal"
            assert tick.signal_count == 2
            assert tick.signal is not None
            assert tick.signal.side == "sell"

            items = await _queue_items(settings, redis)
            assert len(items) == 2
            assert '"side":"sell"' in items[0]
            assert '"side":"buy"' in items[1]
        finally:
            store.close()
            await redis.aclose()

    async def test_existing_long_reseeds_levels(self, tmp_path) -> None:
        """已持仓时不会再走开仓分支，需用持仓均价重新定档才有保护。"""
        settings = _settings(tmp_path)
        cfg = tmp_path / "strategies"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "sma_cross.toml").write_text(
            "[strategy]\n"
            'strategy_id = "sma_cross"\n'
            'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n\n'
            "[params]\nfast_len = 10\nslow_len = 50\n"
            "sr_enabled = true\nsr_left_right = 2\n"
        )
        bars = _sr_bars()
        BarStorage(settings.storage.data_dir).save_bars(bars)
        store = SQLiteStore(settings.state.db_path)
        store.update_position(
            Position(inst_id="BTC-USDT", qty=Decimal("1"), avg_price=Decimal("120"))
        )
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        marker = (
            f"{settings.engine.last_bar_key}:"
            f"{settings.engine.strategy}:{settings.engine.inst}"
        )
        await redis.set(marker, str(bars[56].ts))
        try:
            tick = await _engine(settings, store, redis).run_once(
                now_ms=bars[-1].ts + HOUR
            )

            # 只有止损卖出，没有买入（持仓已存在）
            assert tick.status == "signal"
            assert tick.signal_count == 1
            assert tick.signal is not None
            assert tick.signal.side == "sell"

            items = await _queue_items(settings, redis)
            assert len(items) == 1
            assert '"side":"sell"' in items[0]
        finally:
            store.close()
            await redis.aclose()


def _sr_bars() -> list[Bar]:
    """横盘 100 → 第 50 根探底 90（摆动低点）→ 第 56 根跳升 125（金叉）→ 第 58 根跌破 90。"""
    bars = []
    total = 60
    for i in range(total):
        if i == 50:
            price = Decimal("90")
        elif i == 56:
            price = Decimal("125")
        elif i == 58:
            price = Decimal("86")
        else:
            price = Decimal("100")
        high = Decimal("124") if i == 57 else price
        low = Decimal("85") if i == 58 else price
        bars.append(
            Bar(
                ts=LAST_TS - (total - 1 - i) * HOUR,
                open=price,
                high=high,
                low=low,
                close=price,
                vol=Decimal("10"),
                vol_quote=price * 10,
                confirm=1,
                inst_id="BTC-USDT",
                bar_size="1H",
            )
        )
    return bars


class TestStatusPublish:
    """每轮发布的状态 JSON 供看板读取。"""

    async def test_publishes_status_and_counters(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        _write_strategy_config(tmp_path)
        bars = _wave_bars()
        BarStorage(settings.storage.data_dir).save_bars(bars)
        store = SQLiteStore(settings.state.db_path)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        marker = (
            f"{settings.engine.last_bar_key}:"
            f"{settings.engine.strategy}:{settings.engine.inst}"
        )
        await redis.set(marker, str(bars[54].ts))
        try:
            engine = _engine(settings, store, redis)
            tick = await engine.run_once(now_ms=bars[-1].ts + HOUR)
            await engine.publish_status(tick)

            assert (
                engine.status_key
                == "quant:engine:status:sma_cross:BTC-USDT"
            )
            payload = json.loads(await redis.get(engine.status_key))
            assert payload["status"] == "signal"
            assert payload["bar_ts"] == bars[-1].ts
            assert payload["catch_up"] == 10
            assert payload["ticks"] == 1
            assert payload["signals_total"] == 2
            assert payload["catch_up_total"] == 10
            assert payload["skipped_total"] == 0
            assert payload["started_ts"] > 0

            # M3-15：同一轮评估另外落一份定长历史，供策略详情页读取
            history = await redis.lrange(
                f"{settings.engine.status_key}:history:"
                f"{settings.engine.strategy}:{settings.engine.inst}",
                0,
                -1,
            )
            assert len(history) == 1
            assert json.loads(history[0])["bar_ts"] == bars[-1].ts
        finally:
            store.close()
            await redis.aclose()
