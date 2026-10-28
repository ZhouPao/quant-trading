"""prune 命令：手动触发本地数据保留清理（`storage.retention_days`）。

用法：
  quant prune                 # 按配置的 retention_days 清理一次
  quant prune --days 180      # 临时覆盖保留天数

清理 K线 parquet、SQLite 历史记录与日报 CSV；由 monitor 每日自动执行，
本命令用于首次清理或调整保留期后立即生效。
"""
from __future__ import annotations

import time

from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.state.sqlite_store import SQLiteStore
from quant.storage.retention import RetentionPurger


def main(args) -> int:
    """执行一次保留清理，返回退出码。"""
    settings = Settings.load(args.config)
    if args.days is not None:
        settings.storage.retention_days = args.days

    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.prune")

    if settings.storage.retention_days <= 0:
        log.error(
            f"retention_days must be positive, got {settings.storage.retention_days}"
        )
        return 1

    store = SQLiteStore(settings.state.db_path)
    try:
        result = RetentionPurger(settings, store).run(int(time.time() * 1000))
    finally:
        store.close()

    log.info(f"prune done cutoff={result.cutoff_ts} {result.summary()}")
    return 0
