"""模拟盘日报：每日定时生成「模拟盘 vs 回测」偏离报告。

调度由 monitor 主循环每分钟检查一次：本地时间过了 `report.at` 且当天还没
生成过，就生成一份报告，写 CSV 到 `report.out_dir`，并按需发送邮件。

是否已生成用 Redis 键 `report.last_run_key` 记录（存日期字符串）。用 Redis
而不是进程内状态，是为了 monitor 重启后不会重复出报告；monitor 若在
`at` 之后才启动，则会立刻补一次当天的报告。
"""
from __future__ import annotations

import asyncio
import csv
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from redis.asyncio import Redis

from quant.analysis.deviation import (
    DEFAULT_THRESHOLD,
    DeviationReport,
    compare_fills,
    format_report,
)
from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.runner import BacktestSetupError, load_funding_schedule
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger
from quant.foundation.schedule import is_due, parse_at
from quant.monitor.alerter import EmailAlerter
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage
from quant.strategies.loader import StrategyConfigError
from quant.strategies.params import ParamError, resolve_strategy

log = get_logger("report.daily")

BAR_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
    "6H": 21_600_000,
    "12H": 43_200_000,
    "1D": 86_400_000,
}


class ReportError(Exception):
    """日报无法生成（无成交、无 K线、周期不支持等），消息可直接记日志。"""


@dataclass(frozen=True, slots=True)
class DailyResult:
    """一次成功生成的日报。"""

    status: str  # PASS / FAIL
    csv_path: Path
    report: DeviationReport


def _load_strategy(
    strategy_id: str,
    config_dir: Path,
    store: SQLiteStore,
    resolve_from_db: bool,
):
    """按与实盘相同的两层合并解析策略（toml ← DB 覆盖）。

    原先用 `load_strategy` 只读 toml：看板改过参数后，模拟盘用 DB 参数、
    日报回测用 toml 参数，偏离报告从第一步就不同源、会误判 FAIL（P1）。
    """
    try:
        strategy_cls, params, _sources = resolve_strategy(
            strategy_id,
            config_dir,
            store,
            resolve_from_db=resolve_from_db,
        )
    except (StrategyConfigError, ParamError) as e:
        raise ReportError(str(e)) from e
    return strategy_cls(), params


def build_report(
    settings: Settings,
    store: SQLiteStore,
    *,
    strategy: str,
    inst: str,
    bar: str,
    limit: int = 500,
    window_hours: int = 24,
    threshold: float = DEFAULT_THRESHOLD,
    config_dir: Path | str | None = None,
) -> DeviationReport:
    """读取模拟盘成交 + 本地 K线重跑回测，返回偏离报告。

    Raises:
        ReportError: 周期不支持、无成交、无本地 K线或策略配置缺失
    """
    if bar not in BAR_MS:
        raise ReportError(f"unsupported bar: {bar}")

    paper_fills = [f for f in store.get_recent_trades(limit) if f.inst_id == inst]
    if not paper_fills:
        raise ReportError(f"no paper fills for {inst} in {settings.state.db_path}")

    window_ms = window_hours * 3_600_000
    start_ts = paper_fills[0].ts
    end_ts = paper_fills[-1].ts
    # 成交窗不足 window_hours 时向前扩，避免回测窗口过窄导致 0 笔成交
    if end_ts - start_ts < window_ms:
        start_ts = end_ts - window_ms
    interval = BAR_MS[bar]
    storage = BarStorage(settings.storage.data_dir)
    years = storage.list_years(inst, bar)
    if not years:
        raise ReportError(
            f"no local bars for {inst} {bar}; run fetch-candles first"
        )
    data_start = int(datetime(years[0], 1, 1, tzinfo=UTC).timestamp() * 1000)
    bars = storage.load_bars(inst, bar, data_start, end_ts + interval)
    if not bars:
        raise ReportError(
            f"no local bars for {inst} {bar} ({start_ts}..{end_ts}); "
            "run fetch-candles first"
        )

    strategy_obj, params = _load_strategy(
        strategy,
        Path(config_dir or settings.report.config_dir),
        store,
        settings.params.resolve_from_db,
    )
    try:
        # 模拟盘真实成交含资金费，回测侧不传会系统性偏离（P1）；
        # 只校验对比窗口的覆盖，早期历史成本不影响窗口内 fills 对比
        funding = load_funding_schedule(
            settings, inst, start_ts=start_ts, end_ts=end_ts
        )
    except BacktestSetupError as e:
        raise ReportError(str(e)) from e
    # 必须从本地最早一根 bar 开始连续成交，而不是只回看 window_size 根。
    # 模拟盘在窗口起点可能持有很早建立的仓位，回看段太短时回测会「空仓起步」，
    # 首笔平仓就对不上：实测 200 根回看会产生 52% 的假偏离，全量历史为 0%。
    # 注意这与 CLI 的 warmup 语义相反——那里只需指标就绪，这里还要还原持仓。
    result = Backtest(
        strategy=strategy_obj,
        matcher=Matcher(
            slippage_rate=settings.backtest.slippage_bps / Decimal("10000"),
            commission_rate=settings.backtest.commission_rate,
        ),
        initial_cash=settings.backtest.initial_cash,
        window_size=settings.backtest.window_size,
        params=params,
        funding=funding,
    ).run(bars)

    backtest_fills = [t for t in result.trades if start_ts <= t.ts <= end_ts]
    return compare_fills(paper_fills, backtest_fills, threshold=threshold)


def report_csv_rows(
    report: DeviationReport,
) -> list[tuple[str, str, str, str]]:
    """偏离报告 -> CSV 行（metric/paper/backtest/deviation）。"""
    return [
        (
            "trade_count",
            str(report.paper.trade_count),
            str(report.backtest.trade_count),
            f"{report.trade_count_dev:.6f}",
        ),
        (
            "qty",
            str(report.paper.buy_qty + report.paper.sell_qty),
            str(report.backtest.buy_qty + report.backtest.sell_qty),
            f"{report.qty_dev:.6f}",
        ),
        (
            "notional",
            str(report.paper.notional),
            str(report.backtest.notional),
            f"{report.notional_dev:.6f}",
        ),
        (
            "fee",
            str(report.paper.fee),
            str(report.backtest.fee),
            f"{report.fee_dev:.6f}",
        ),
        (
            "side_sequence",
            "match" if report.side_sequence_match else "mismatch",
            "match",
            "0" if report.side_sequence_match else "1",
        ),
    ]


def write_report_csv(path: Path, report: DeviationReport) -> None:
    """把偏离报告写成 CSV（自动创建父目录）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "paper", "backtest", "deviation"])
        writer.writerows(report_csv_rows(report))


class DailyReporter:
    """按配置生成并分发日报；由 monitor 每轮调用 maybe_run。"""

    def __init__(
        self,
        settings: Settings,
        store: SQLiteStore,
        redis: Redis,
        alerter: EmailAlerter | None = None,
        config_dir: Path | str | None = None,
    ) -> None:
        self._settings = settings
        self._cfg = settings.report
        self._store = store
        self._redis = redis
        self._alerter = alerter or EmailAlerter(settings.smtp)
        self._config_dir = Path(config_dir or self._cfg.config_dir)
        self._at_minutes = parse_at(self._cfg.at)
        if self._cfg.enabled and self._at_minutes is None:
            log.error(
                f"daily report disabled: invalid report.at={self._cfg.at!r} "
                "(expect HH:MM)"
            )

    @property
    def enabled(self) -> bool:
        """配置开启且时间格式合法。"""
        return self._cfg.enabled and self._at_minutes is not None

    async def maybe_run(self, now_ms: int) -> DailyResult | None:
        """到点则生成一次日报；未开启/未到点/当天已出返回 None。"""
        if not self.enabled:
            return None
        if not is_due(now_ms, self._at_minutes, await self._last_date()):
            return None

        date_str = time.strftime("%Y-%m-%d", time.localtime(now_ms / 1000))
        try:
            result = await asyncio.to_thread(self._generate, date_str)
        except ReportError as e:
            log.error(f"daily report {date_str} skipped: {e}")
            # 失败也标记当天，避免每分钟重跑回测刷日志；可手动跑 paper-report
            await self._mark(date_str)
            return None

        await self._mark(date_str)
        log.info(
            f"daily report {date_str} {result.status} "
            f"max_dev={result.report.max_deviation:.2%} -> {result.csv_path}"
        )
        if self._cfg.email:
            await asyncio.to_thread(self._notify, date_str, result)
        return result

    # internal

    def _generate(self, date_str: str) -> DailyResult:
        report = build_report(
            self._settings,
            self._store,
            strategy=self._cfg.strategy,
            inst=self._cfg.inst,
            bar=self._cfg.bar,
            limit=self._cfg.limit,
            window_hours=self._cfg.window_hours,
            threshold=self._cfg.threshold,
            config_dir=self._config_dir,
        )
        path = (
            Path(self._cfg.out_dir)
            / f"{date_str}-{self._cfg.strategy}-{self._cfg.inst}.csv"
        )
        write_report_csv(path, report)
        return DailyResult(
            status="PASS" if report.within_threshold else "FAIL",
            csv_path=path,
            report=report,
        )

    def _notify(self, date_str: str, result: DailyResult) -> None:
        if not self._alerter.enabled():
            return
        body = format_report(result.report) + f"\n\nCSV: {result.csv_path}"
        self._alerter.send(f"[quant] 模拟盘日报 {date_str} {result.status}", body)

    async def _last_date(self) -> str | None:
        try:
            return await self._redis.get(self._cfg.last_run_key)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read daily report marker failed: {e}")
            return None

    async def _mark(self, date_str: str) -> None:
        try:
            await self._redis.set(self._cfg.last_run_key, date_str, ex=7 * 86_400)
        except Exception as e:  # noqa: BLE001
            log.warning(f"write daily report marker failed: {e}")
