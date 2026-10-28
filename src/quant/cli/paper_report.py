"""paper-report 命令：模拟盘 vs 回测偏离报告（M3）。

用法：
  quant paper-report --strategy sma_cross --inst BTC-USDT --bar 1H
  quant paper-report --strategy rsi --limit 200 --threshold 0.05 --out reports/paper.csv

流程：
1. 从 SQLite 读取模拟盘成交（fills 表）
2. 以成交时间窗为区间，加载本地 K线并跑一次回测
3. 对比成交笔数/数量/名义金额/手续费/方向序列
4. 偏离 > 阈值（默认 5%）返回非 0 退出码

与 monitor 的定时日报共用 `quant.report.daily.build_report`。
"""
from __future__ import annotations

from pathlib import Path

from quant.analysis.deviation import format_report
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.report.daily import ReportError, build_report, write_report_csv
from quant.state.sqlite_store import SQLiteStore


def main(args) -> int:
    """生成偏离报告。返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.paper_report")

    store = SQLiteStore(settings.state.db_path)
    try:
        report = build_report(
            settings,
            store,
            strategy=args.strategy,
            inst=args.inst,
            bar=args.bar,
            limit=args.limit,
            window_hours=args.window_hours,
            threshold=args.threshold,
            config_dir=args.config_dir,
        )
    except ReportError as e:
        log.error(str(e))
        return 1
    finally:
        store.close()

    print(format_report(report))

    if args.out:
        write_report_csv(Path(args.out), report)
        log.info(f"report written to {args.out}")

    if not report.within_threshold:
        log.error(
            f"deviation exceeds threshold: {report.max_deviation:.2%} "
            f"> {report.threshold:.2%}"
        )
        return 1
    return 0
