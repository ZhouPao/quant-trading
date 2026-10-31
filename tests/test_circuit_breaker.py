"""极端行情熔断测试（M7-6）。

覆盖四层：
- `CircuitBreaker` 的振幅判定、TTL、人工解除与坏数据兜底
- 引擎：超阈值 → 跳过该品种 + 落 risk_events + 告警 + 推进水位
- worker：熔断期间该品种的信号被拒绝，不产生订单
- 看板：`/api/risk-limits` 返回 circuits，与 kill switch 状态键互不影响
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import fakeredis.aioredis
import pytest

from quant.backtest.signal import Signal
from quant.dashboard.service import DashboardService
from quant.engine.live import LiveSignalEngine
from quant.execution.executor import Executor
from quant.execution.kill_switch import KillSwitch
from quant.execution.worker import Worker
from quant.foundation.config import OKXSettings, Settings
from quant.models import Bar
from quant.queue.signal_queue import SignalQueue
from quant.risk.checker import RiskChecker
from quant.risk.circuit_breaker import CircuitBreaker
from quant.risk.config import RiskConfig
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage

HOUR = 3_600_000
LAST_TS = (1_700_000_000_000 // HOUR) * HOUR
NOW = LAST_TS + HOUR
THRESHOLD = Decimal("0.10")
COOLDOWN = 900


def _bar(
    ts: int = LAST_TS,
    *,
    open_: str = "100",
    high: str = "100",
    low: str = "100",
    close: str = "100",
) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        vol=Decimal("10"),
        vol_quote=Decimal(close) * 10,
        confirm=1,
        inst_id="BTC-USDT",
        bar_size="1H",
    )


def _redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _signal(signal_id: str = "sig-1") -> Signal:
    return Signal(
        strategy_id="sma_cross",
        side="buy",
        order_type="market",
        inst_id="BTC-USDT",
        qty=Decimal("1"),
        ts=LAST_TS,
        signal_id=signal_id,
    )


class TestAmplitude:
    def test_amplitude(self) -> None:
        bar = _bar(open_="100", high="130", low="90")
        assert CircuitBreaker.amplitude(bar) == Decimal("0.40")


class TestTrip:
    async def test_evaluate_at_threshold_does_not_trip(self) -> None:
        redis = _redis()
        breaker = CircuitBreaker(redis)
        # (110-100)/100 = 恰好 10%，属于正常波动边界
        bar = _bar(open_="100", high="110", low="100")

        assert (
            await breaker.evaluate(
                bar, threshold=THRESHOLD, cooldown_seconds=COOLDOWN
            )
            is None
        )
        assert await breaker.is_tripped("BTC-USDT") is False

    async def test_evaluate_above_threshold_trips(self) -> None:
        redis = _redis()
        breaker = CircuitBreaker(redis)
        bar = _bar(open_="100", high="130", low="90")

        state = await breaker.evaluate(
            bar, threshold=THRESHOLD, cooldown_seconds=COOLDOWN, now=NOW
        )

        assert state is not None
        assert state.inst_id == "BTC-USDT"
        assert state.resume_at == NOW + COOLDOWN * 1000
        assert "40.00%" in state.reason
        current = await breaker.state("BTC-USDT")
        assert current is not None and current.triggered_ts == NOW
        # 冷却期靠 TTL 自动到期
        ttl = await redis.ttl(breaker.key_for("BTC-USDT"))
        assert 0 < ttl <= COOLDOWN

    async def test_trip_is_per_inst(self) -> None:
        redis = _redis()
        breaker = CircuitBreaker(redis)

        await breaker.trip(
            "BTC-USDT", "test", cooldown_seconds=COOLDOWN, now=NOW
        )

        assert await breaker.is_tripped("BTC-USDT") is True
        assert await breaker.is_tripped("ETH-USDT") is False

    async def test_states_lists_only_tripped(self) -> None:
        redis = _redis()
        breaker = CircuitBreaker(redis)
        await breaker.trip("BTC-USDT", "a", cooldown_seconds=COOLDOWN, now=NOW)
        await breaker.trip(
            "ETH-USDT", "b", cooldown_seconds=COOLDOWN, now=NOW + 1000
        )

        states = await breaker.states()

        assert [s.inst_id for s in states] == ["ETH-USDT", "BTC-USDT"]

    async def test_resume_clears_state(self) -> None:
        redis = _redis()
        breaker = CircuitBreaker(redis)
        await breaker.trip("BTC-USDT", "test", cooldown_seconds=COOLDOWN)

        assert await breaker.resume("BTC-USDT") is True
        assert await breaker.is_tripped("BTC-USDT") is False
        assert await breaker.resume("BTC-USDT") is False

    async def test_expired_key_means_armed(self) -> None:
        redis = _redis()
        breaker = CircuitBreaker(redis)
        await breaker.trip("BTC-USDT", "test", cooldown_seconds=COOLDOWN)
        # 模拟冷却期结束：键自然过期
        await redis.expire(breaker.key_for("BTC-USDT"), 0)

        assert await breaker.is_tripped("BTC-USDT") is False

    async def test_corrupt_state_is_still_tripped(self) -> None:
        redis = _redis()
        breaker = CircuitBreaker(redis)
        await redis.set(breaker.key_for("BTC-USDT"), "not-json")

        state = await breaker.state("BTC-USDT")

        # 键在但内容坏了：宁可少交易，不可多交易
        assert state is not None
        assert "unreadable" in state.reason


def _settings(tmp_path: Path, *, circuit_enabled: bool = True) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p"),
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "data" / "state" / "quant.db"},
        backtest={"window_size": 200},
        risk={
            "circuit_enabled": circuit_enabled,
            "circuit_amplitude_pct": THRESHOLD,
            "circuit_cooldown_seconds": COOLDOWN,
        },
        engine={
            "strategy": "sma_cross",
            "inst": "BTC-USDT",
            "bar": "1H",
            "warmup_bars": 200,
            "config_dir": tmp_path / "strategies",
            "cash_fallback": "5000",
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


def _bars(count: int = 60) -> list[Bar]:
    """前 count-1 根横盘，最后一根振幅 40%（high 130 / low 90 / open 100）。"""
    bars = [
        _bar(LAST_TS - (count - 1 - i) * HOUR)
        for i in range(count - 1)
    ]
    bars.append(_bar(LAST_TS, open_="100", high="130", low="90", close="100"))
    return bars


@pytest.fixture
def env(tmp_path):
    settings = _settings(tmp_path)
    _write_strategy_config(tmp_path)
    BarStorage(settings.storage.data_dir).save_bars(_bars())
    store = SQLiteStore(settings.state.db_path)
    redis = _redis()
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


class TestEngineTrip:
    async def test_wide_bar_trips_and_skips(self, env) -> None:
        settings, store, redis = env

        tick = await _engine(settings, store, redis).run_once(now_ms=NOW)

        assert tick.status == "skipped"
        assert tick.reason.startswith("circuit breaker:")
        assert tick.bar_ts == LAST_TS
        # 熔断期间不产生信号
        assert await redis.lrange(settings.redis.queue_key, 0, -1) == []
        # 水位推进：恢复后不补发这根已经过去的 bar
        marker = await redis.get("quant:engine:last-bar:sma_cross:BTC-USDT")
        assert marker == str(LAST_TS)

        breaker = CircuitBreaker(redis)
        assert await breaker.is_tripped("BTC-USDT") is True

    async def test_trip_is_audited_and_alerted(self, env) -> None:
        settings, store, redis = env

        await _engine(settings, store, redis).run_once(now_ms=NOW)

        events = store.list_risk_events(rule="circuit_breaker")
        assert len(events) == 1
        assert events[0]["decision"] == "halted"
        assert events[0]["inst_id"] == "BTC-USDT"
        assert Decimal(events[0]["actual_value"]) == Decimal("0.40")

        raw = await redis.lrange(settings.monitor.dashboard_alerts_key, 0, -1)
        assert len(raw) == 1
        alert = json.loads(raw[0])
        assert alert["rule"] == "circuit_breaker"
        assert alert["level"] == "ERROR"
        assert alert["inst_id"] == "BTC-USDT"

    async def test_active_state_skips_following_bars(self, env) -> None:
        settings, store, redis = env
        await _engine(settings, store, redis).run_once(now_ms=NOW)
        # 下一根恢复正常振幅
        BarStorage(settings.storage.data_dir).save_bars(
            [_bar(LAST_TS + HOUR)]
        )

        tick = await _engine(settings, store, redis).run_once(now_ms=NOW + HOUR)

        assert tick.status == "skipped"
        assert tick.reason.startswith("circuit breaker active until")

    async def test_disabled_circuit_does_not_block(self, tmp_path) -> None:
        settings = _settings(tmp_path, circuit_enabled=False)
        _write_strategy_config(tmp_path)
        BarStorage(settings.storage.data_dir).save_bars(_bars())
        store = SQLiteStore(settings.state.db_path)
        redis = _redis()
        try:
            tick = await _engine(settings, store, redis).run_once(now_ms=NOW)

            assert tick.status != "skipped"
            assert await CircuitBreaker(redis).is_tripped("BTC-USDT") is False
        finally:
            store.close()


@pytest.fixture
def worker_env(tmp_path):
    redis = _redis()
    store = SQLiteStore(tmp_path / "quant.db")
    queue = SignalQueue(redis)
    risk = RiskChecker(
        RiskConfig(
            strategy_id="sma_cross",
            max_position_pct=Decimal("0.9"),
            max_total_pct=Decimal("0.95"),
            max_order_qty=Decimal("100000"),
            daily_loss_limit=Decimal("0.5"),
            consecutive_loss_limit=3,
        ),
        store,
    )
    worker = Worker(
        queue=queue,
        risk=risk,
        executor=Executor(),
        store=store,
        redis=redis,
        price_provider=lambda inst_id: Decimal("100"),
        heartbeat_interval=1,
        circuit=CircuitBreaker(redis),
    )
    yield worker, queue, store, redis
    store.close()


class TestWorkerReject:
    async def test_rejects_signal_for_tripped_inst(self, worker_env) -> None:
        worker, queue, store, redis = worker_env
        await CircuitBreaker(redis).trip(
            "BTC-USDT", "test crash", cooldown_seconds=COOLDOWN
        )
        await queue.push(_signal())

        handled = await worker.run_once(timeout=0)

        assert handled is True
        assert store.get_signal_status("sig-1") == "rejected"
        # 没有生成订单：熔断是拒绝，不是下单
        assert store.get_open_orders() == []
        events = store.list_risk_events(rule="circuit_breaker")
        assert len(events) == 1
        assert events[0]["decision"] == "rejected"

    async def test_other_inst_still_trades(self, worker_env) -> None:
        worker, queue, store, redis = worker_env
        await CircuitBreaker(redis).trip(
            "ETH-USDT", "test crash", cooldown_seconds=COOLDOWN
        )
        await queue.push(_signal())

        await worker.run_once(timeout=0)

        assert store.get_signal_status("sig-1") == "executed"

    async def test_circuit_does_not_touch_kill_switch(self, worker_env) -> None:
        _, _, store, redis = worker_env
        await CircuitBreaker(redis).trip(
            "BTC-USDT", "test crash", cooldown_seconds=COOLDOWN
        )

        assert await KillSwitch(redis).is_active() is False


class TestDashboard:
    async def test_risk_limits_reports_circuits(self, tmp_path) -> None:
        redis = _redis()
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            await CircuitBreaker(redis).trip(
                "BTC-USDT", "单根振幅超阈值", cooldown_seconds=COOLDOWN, now=NOW
            )
            service = DashboardService(store, redis, _settings(tmp_path))

            data = await service.risk_limits()

            assert [c["inst_id"] for c in data["circuits"]] == ["BTC-USDT"]
            assert data["circuits"][0]["resume_at"] == NOW + COOLDOWN * 1000
        finally:
            store.close()
