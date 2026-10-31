"""run-worker 命令：启动信号消费 worker（spec §9，M3 增补执行/行情接入）。

用法：
  quant run-worker --strategy sma_cross
  quant run-worker --strategy sma_cross --broker okx --td-mode cash
  quant run-worker --strategy sma_cross --ws          # WS 实时价
  quant run-worker --strategy sma_cross --paper-mode  # 模拟盘验证期放宽仓位

行情来源优先级：
1. `--ws`：WS tickers 实时价
2. `--broker okx`：OKX 公开 ticker REST 实时价
3. 回退：本地 1H K线最近收盘价
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from redis.asyncio import Redis

from quant.execution.account import read_account_field
from quant.execution.executor import Executor
from quant.execution.kill_switch import KillSwitch
from quant.execution.okx_broker import OkxBroker
from quant.execution.price import LivePriceBook, RestPriceProvider
from quant.execution.reconciler import Reconciler
from quant.execution.worker import Worker
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.okx.client import OKXClient
from quant.okx.ws_client import WSClient
from quant.queue.signal_queue import SignalQueue
from quant.risk.checker import RiskChecker
from quant.risk.circuit_breaker import CircuitBreaker
from quant.risk.config import load_risk_config, load_strategy_risk
from quant.risk.limits import (
    LIMIT_FIELDS,
    RiskLimits,
    limits_payload,
    resolve_limits,
)
from quant.risk.preflight import PreflightChecker
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage

DAY_MS = 86_400_000


def _bar_price_provider(data_dir):
    """从本地 1H K线读最近收盘价；无数据返回 None。"""
    storage = BarStorage(data_dir)

    def provider(inst_id: str) -> Decimal | None:
        now_ms = int(time.time() * 1000)
        bars = storage.load_bars(inst_id, "1H", now_ms - 2 * DAY_MS, now_ms)
        if not bars:
            return None
        return bars[-1].close

    return provider


def _build_preflight(
    args, settings: Settings, client: OKXClient | None
) -> PreflightChecker | None:
    """下单前预检（M7-7）只在直连交易所时启用。

    模拟撮合（Executor）没有产品配置与账户余额，预检会变成无意义的
    拒绝，因此直接返回 None。
    """
    if args.broker != "okx" or client is None:
        return None
    if not settings.risk.preflight_enabled:
        return None
    return PreflightChecker(
        client,
        min_notional=settings.risk.preflight_min_notional_usdt,
        balance_buffer=settings.risk.preflight_balance_buffer_pct,
        balance_cache_seconds=settings.risk.preflight_balance_cache_seconds,
    )


def _apply_paper_mode(config):
    """模拟盘验证模式：放宽仓位比例，使风控不截断回测的全仓 sizing。

    仅覆盖仓位比例；`max_order_qty`、日亏损、连续亏损等仍生效。
    `max_order_qty`（配置 10000）与 `[backtest].initial_cash` 同量级，
    真实账户下会成为实际的单笔上限——这正是日报偏离对比需要的口径：
    模拟盘成交名义应与回测初始资金对齐，而不是与账户可用余额对齐。
    """
    return config.model_copy(
        update={
            "max_position_pct": Decimal("1.0"),
            "max_total_pct": Decimal("1.0"),
        }
    )


def _build_broker(args, settings: Settings, client: OKXClient | None):
    if args.broker == "okx":
        if client is None:
            raise RuntimeError("okx broker requires an OKXClient")
        return OkxBroker(client, td_mode=args.td_mode)
    return Executor(settings.backtest.commission_rate)


async def _run(args, settings: Settings) -> int:
    log = get_logger("cli.run_worker")

    redis = Redis.from_url(settings.redis.url, decode_responses=True)
    store = SQLiteStore(settings.state.db_path)
    queue = SignalQueue(
        redis,
        pending_key=settings.redis.queue_key,
        inflight_key=settings.redis.inflight_key,
        dead_key=settings.redis.dead_key,
        # 死信双写 SQLite，供看板查看与重放（MS4-B §10）
        store=store if settings.storage.dead_letter_persist else None,
    )
    # 生效参数 = ① 代码默认 / ② 全局 [risk] / ③ 策略 [risk] 取严后的结果
    risk_config = load_risk_config(
        args.strategy,
        config_dir=args.config_dir,
        defaults=settings.risk,
    )
    if args.paper_mode:
        risk_config = _apply_paper_mode(risk_config)
    risk = RiskChecker(
        risk_config,
        store,
        settings.backtest.initial_cash,
        allow_initial_equity_fallback=(
            settings.risk.allow_initial_equity_fallback
        ),
        event_log=settings.risk.event_log,
    )
    # 发布的是「生效值」而非配置文件原值：paper-mode 覆盖只存在于本进程
    limits = resolve_limits(
        args.strategy,
        defaults=settings.risk,
        strategy_risk=load_strategy_risk(
            args.strategy, config_dir=args.config_dir
        ),
    )
    if args.paper_mode:
        # paper-mode 绕过取严直接改生效值，来源记为 override（人工决定）
        merged = {name: limits.value(name) for name in LIMIT_FIELDS}
        merged["max_position_pct"] = risk_config.max_position_pct
        merged["max_total_pct"] = risk_config.max_total_pct
        sources = {name: limits.source(name) for name in LIMIT_FIELDS}
        sources["max_position_pct"] = "override"
        sources["max_total_pct"] = "override"
        limits = RiskLimits(
            strategy_id=limits.strategy_id,
            applied=sources,
            **merged,  # type: ignore[arg-type]
        )
    # 基线 = 合并后的「全局+策略」，paper-mode 覆盖也只存在于本进程。
    # worker 每轮心跳在其上叠 Redis 在线覆盖层（M7-8）。
    base_limits = limits
    risk_limits = limits_payload(base_limits, paper_mode=bool(args.paper_mode))

    client: OKXClient | None = None
    public_client: OKXClient | None = None
    ws: WSClient | None = None
    ws_task: asyncio.Task | None = None
    price_book: LivePriceBook | None = None
    bar_provider = _bar_price_provider(settings.storage.data_dir)

    try:
        if args.broker == "okx":
            client = OKXClient(settings.okx)
        broker = _build_broker(args, settings, client)

        # 仅 OKX broker 支持对账（需 get_order 查询远端订单状态）
        order_timeout = (
            args.order_timeout
            if args.order_timeout is not None
            else settings.execution.order_timeout_seconds
        )
        reconcile_interval = (
            args.reconcile_interval
            if args.reconcile_interval is not None
            else settings.execution.reconcile_interval_seconds
        )
        reconciler = (
            Reconciler(store, broker, order_timeout_seconds=order_timeout)
            if args.broker == "okx"
            else None
        )

        # 公开行情不需要鉴权：mock 模式也可用 REST 实时价
        if client is not None:
            public_client = client
        else:
            public_client = OKXClient(settings.okx)

        if args.ws:
            price_book = LivePriceBook()
            ws = WSClient()
            await ws.subscribe("tickers", args.inst, price_book.update_from_payload)
            ws_task = asyncio.create_task(ws.run())

        rest_provider = RestPriceProvider(public_client)

        async def equity_provider() -> Decimal | None:
            """风控基数取账户总权益，与 engine 读同一份 monitor 快照。

            返回 None 表示「不可用」：键缺失、值非法，或快照超龄
            （monitor 卡死后键仍在 TTL 内，但数值已过时）。随后由
            checker 按 allow_initial_equity_fallback 决定兜底还是拒单。
            """
            value = await read_account_field(
                redis,
                settings.monitor.dashboard_snapshot_key,
                "total_equity",
                now_ms=int(time.time() * 1000),
                max_age_ms=settings.risk.snapshot_max_age_ms,
            )
            if value is None:
                log.warning(
                    "account snapshot unavailable; risk equity strict mode="
                    f"{not settings.risk.allow_initial_equity_fallback}"
                )
            return value

        def price_provider(inst_id: str) -> Decimal | None:
            if price_book is not None:
                live = price_book.get_price(inst_id)
                if live is not None:
                    return live
            rest = rest_provider(inst_id)
            if rest is not None:
                return rest
            return bar_provider(inst_id)

        worker = Worker(
            queue=queue,
            risk=risk,
            executor=broker,
            store=store,
            redis=redis,
            price_provider=price_provider,
            heartbeat_interval=args.heartbeat_interval,
            reconciler=reconciler,
            kill_switch=KillSwitch(redis),
            reconcile_interval=reconcile_interval,
            equity_provider=equity_provider,
            risk_limits_key=(
                f"{settings.monitor.risk_limits_key}:{args.strategy}"
            ),
            risk_limits=risk_limits,
            risk_limits_ttl=settings.monitor.risk_limits_ttl_seconds,
            circuit=(
                CircuitBreaker(redis)
                if settings.risk.circuit_enabled
                else None
            ),
            overrides_key=settings.monitor.risk_overrides_key,
            base_limits=base_limits,
            paper_mode=bool(args.paper_mode),
            preflight=_build_preflight(args, settings, public_client),
        )

        log.info(
            f"worker started strategy={args.strategy} broker={args.broker} "
            f"ws={'on' if args.ws else 'off'} "
            f"paper_mode={'on' if args.paper_mode else 'off'} "
            f"max_position_pct={risk_config.max_position_pct} "
            f"reconcile={reconcile_interval}s order_timeout={order_timeout}s "
            f"db={settings.state.db_path}"
        )
        await worker.run()
        return 0
    finally:
        if ws is not None:
            await ws.close()
        if ws_task is not None:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                log.warning(f"ws task stopped with error: {e}")
        if public_client is not None:
            public_client.close()
        store.close()
        await redis.aclose()


def main(args) -> int:
    """启动 worker（阻塞直到 SIGINT/SIGTERM）。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.run_worker")
    try:
        return asyncio.run(_run(args, settings))
    except KeyboardInterrupt:
        log.info("worker interrupted")
        return 0
