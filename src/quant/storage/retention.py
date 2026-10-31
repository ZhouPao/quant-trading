"""本地数据保留清理（`storage.retention_days`）。

清理范围：
- K线 parquet：删除早于 cut-off 的行；整年文件清空后直接删文件
- SQLite：signals / orders / fills / equity_snapshots 早于 cut-off 的记录
  （权益快照保留历史峰值那一行，见 `SQLiteStore.purge_before`）；告警按
  级别分档清理（ERROR 90 天 / WARNING 30 天 / INFO 7 天，见 M9-12）
- 日报 CSV：`report.out_dir` 下修改时间早于 cut-off 的文件

调度：monitor 每轮调用 `RetentionPurger.maybe_run`，本地时间过
`storage.retention_at` 且当天未执行则跑一次（Redis 键去重，重启不重复）。
也可用 `quant prune` 手动触发。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from redis.asyncio import Redis

from quant.foundation.config import Settings
from quant.foundation.logger import get_logger
from quant.foundation.schedule import is_due, parse_at
from quant.state.sqlite_store import SQLiteStore

log = get_logger("storage.retention")

DAY_MS = 86_400_000
_PARQUET_WRITE = {
    "index": False,
    "engine": "pyarrow",
    "compression": "zstd",
    "row_group_size": 8192,
}


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """一次清理的结果统计。"""

    cutoff_ts: int
    bars_deleted: int = 0
    bar_files_removed: int = 0
    signals_deleted: int = 0
    orders_deleted: int = 0
    fills_deleted: int = 0
    equity_deleted: int = 0
    report_files_removed: int = 0
    alerts_deleted: int = 0

    def summary(self) -> str:
        """单行摘要，便于写日志。"""
        return (
            f"bars={self.bars_deleted} bar_files={self.bar_files_removed} "
            f"signals={self.signals_deleted} orders={self.orders_deleted} "
            f"fills={self.fills_deleted} equity={self.equity_deleted} "
            f"reports={self.report_files_removed} alerts={self.alerts_deleted}"
        )


def purge_bars(data_dir: Path, cutoff_ms: int) -> tuple[int, int]:
    """删除 K线中早于 cutoff_ms 的行，返回 (删除行数, 删除文件数)。"""
    root = Path(data_dir) / "candles"
    if not root.exists():
        return 0, 0

    rows = 0
    removed = 0
    for path in sorted(root.rglob("*.parquet")):
        try:
            df = pd.read_parquet(path)
        except Exception as e:  # noqa: BLE001
            log.warning(f"prune skip unreadable {path}: {e}")
            continue
        kept = df[df["ts"] >= cutoff_ms]
        if len(kept) == len(df):
            continue

        rows += len(df) - len(kept)
        if kept.empty:
            path.unlink()
            removed += 1
            continue
        kept.to_parquet(path, **_PARQUET_WRITE)
    return rows, removed


def purge_reports(out_dir: Path, cutoff_ms: int) -> int:
    """删除 out_dir 下修改时间早于 cutoff_ms 的文件，返回删除个数。"""
    out_dir = Path(out_dir)
    if not out_dir.exists():
        return 0

    cutoff_s = cutoff_ms / 1000
    removed = 0
    for path in sorted(out_dir.iterdir()):
        if not path.is_file() or path.stat().st_mtime >= cutoff_s:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            log.warning(f"prune report failed {path}: {e}")
    return removed


class RetentionPurger:
    """按 `storage.retention_*` 配置清理本地历史数据。"""

    def __init__(
        self, settings: Settings, store: SQLiteStore, redis: Redis | None = None
    ) -> None:
        self._settings = settings
        self._cfg = settings.storage
        self._store = store
        self._redis = redis
        self._at_minutes = parse_at(self._cfg.retention_at)
        if self._cfg.retention_enabled and self._at_minutes is None:
            log.error(
                f"retention disabled: invalid storage.retention_at="
                f"{self._cfg.retention_at!r} (expect HH:MM)"
            )

    @property
    def enabled(self) -> bool:
        """配置开启、时间格式合法且保留天数有效。"""
        return (
            self._cfg.retention_enabled
            and self._at_minutes is not None
            and self._cfg.retention_days > 0
        )

    async def maybe_run(self, now_ms: int) -> PurgeResult | None:
        """到点则清理一次；未开启/未到点/当天已跑返回 None。

        异常向上抛给调用方（monitor 只记日志），且不写当天标记，
        便于人工修复后手动 `quant prune` 补跑。
        """
        if not self.enabled:
            return None
        if not is_due(now_ms, self._at_minutes, await self._last_date()):
            return None

        date_str = time.strftime("%Y-%m-%d", time.localtime(now_ms / 1000))
        result = await asyncio.to_thread(self.run, now_ms)
        await self._mark(date_str)
        log.info(f"retention {date_str} cutoff={result.cutoff_ts} {result.summary()}")
        return result

    def run(self, now_ms: int) -> PurgeResult:
        """立即执行清理并返回统计。"""
        cutoff = now_ms - self._cfg.retention_days * DAY_MS
        bars, bar_files = purge_bars(self._settings.storage.data_dir, cutoff)
        deleted = self._store.purge_before(cutoff)
        reports = purge_reports(self._settings.report.out_dir, cutoff)
        # 告警按级别分档（M9-12）：ERROR 留得更久，INFO 恢复通知最短
        monitor = self._settings.monitor
        alerts = sum(
            self._store.purge_alerts(
                now_ms,
                error_days=monitor.alert_retention_error_days,
                warning_days=monitor.alert_retention_warning_days,
                info_days=monitor.alert_retention_info_days,
            ).values()
        )
        return PurgeResult(
            cutoff_ts=cutoff,
            bars_deleted=bars,
            bar_files_removed=bar_files,
            signals_deleted=deleted.get("signals", 0),
            orders_deleted=deleted.get("orders", 0),
            fills_deleted=deleted.get("fills", 0),
            equity_deleted=deleted.get("equity_snapshots", 0),
            report_files_removed=reports,
            alerts_deleted=alerts,
        )

    # internal

    async def _last_date(self) -> str | None:
        if self._redis is None:
            return None
        try:
            return await self._redis.get(self._cfg.retention_last_run_key)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read retention marker failed: {e}")
            return None

    async def _mark(self, date_str: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(
                self._cfg.retention_last_run_key, date_str, ex=7 * 86_400
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"write retention marker failed: {e}")


__all__ = [
    "PurgeResult",
    "RetentionPurger",
    "purge_bars",
    "purge_reports",
]
