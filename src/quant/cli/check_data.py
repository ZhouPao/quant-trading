"""check-data 命令：校验本地 K线并生成数据质量报告（MS4-C §9.3）。

用法：
  quant check-data --inst BTC-USDT --bar 1H
  quant check-data --inst BTC-USDT --bar 1H --start 2024-01-01 --end 2024-12-31

与 `validate-data` 的区别：本命令把结果并入
`data/quality/{inst}/{bar}/latest.json`，跨次累计「已修复 / 未修复」缺口，
供看板与 monitor 告警使用。
"""
from __future__ import annotations

from datetime import UTC, datetime

from quant.cli.fetch_candles import parse_date_to_ms
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.storage.bar_storage import BarStorage
from quant.storage.quality import QualityReporter, report_path


def main(args) -> int:
    """生成质量报告。返回退出码：0=无未修复缺口，1=有缺口或非法 K 线。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.check_data")

    storage = BarStorage(settings.storage.data_dir)
    if args.start and args.end:
        start_ts = parse_date_to_ms(args.start)
        end_ts = parse_date_to_ms(args.end)
    else:
        years = storage.list_years(args.inst, args.bar)
        if not years:
            log.warning(
                f"无本地数据: inst={args.inst} bar={args.bar}，请先同步 K线"
            )
            return 1
        start_ts = int(datetime(years[0], 1, 1, tzinfo=UTC).timestamp() * 1000)
        end_ts = int(
            datetime(years[-1] + 1, 1, 1, tzinfo=UTC).timestamp() * 1000
        )

    reporter = QualityReporter(settings.storage.data_dir, storage=storage)
    report = reporter.check(
        args.inst, args.bar, start_ts=start_ts, end_ts=end_ts
    )
    if report is None:
        log.warning("区间内没有 K线，未生成报告")
        return 1

    summary = report["summary"]
    log.info(
        f"质量报告 inst={args.inst} bar={args.bar} "
        f"expected={summary['expected']} present={summary['present']} "
        f"open={summary['gaps_open']} repaired={summary['gaps_repaired']} "
        f"invalid_ohlc={len(report['invalid_ohlc'])}"
    )
    for gap in report["gaps"]:
        if gap["status"] != "open":
            continue
        log.warning(
            f"  未修复缺口 from={gap['from']} to={gap['to']} bars={gap['bars']}"
        )
    log.info(f"报告已写入 {report_path(settings.storage.data_dir, args.inst, args.bar)}")

    return 0 if summary["gaps_open"] == 0 and not report["invalid_ohlc"] else 1
