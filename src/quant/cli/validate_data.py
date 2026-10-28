"""validate-data 命令：校验本地 K线数据完整性（spec §11.2）。

用法：
  quant validate-data --inst BTC-USDT --bar 1H
  quant validate-data --inst BTC-USDT --bar 1H --start 2024-01-01 --end 2024-12-31
"""
from __future__ import annotations

from datetime import UTC, datetime

from quant.cli.fetch_candles import parse_date_to_ms
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.storage.bar_storage import BarStorage
from quant.storage.validator import DataValidator


def main(args) -> int:
    """校验本地 K线数据。返回退出码：0=通过，1=有错误。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.validate_data")

    storage = BarStorage(settings.storage.data_dir)

    # 确定时间范围
    if args.start and args.end:
        start_ts = parse_date_to_ms(args.start)
        end_ts = parse_date_to_ms(args.end)
    else:
        # 全量：用已有年份文件的最小/最大范围
        years = storage.list_years(args.inst, args.bar)
        if not years:
            log.warning(
                f"无本地数据: inst={args.inst} bar={args.bar}，"
                f"请先运行 fetch-candles"
            )
            return 1
        start_ts = int(datetime(years[0], 1, 1, tzinfo=UTC).timestamp() * 1000)
        end_ts = int(
            datetime(years[-1] + 1, 1, 1, tzinfo=UTC).timestamp() * 1000
        )

    log.info(
        f"校验: inst={args.inst} bar={args.bar} "
        f"range=[{args.start or 'all'}, {args.end or 'all'}]"
    )

    bars = storage.load_bars(args.inst, args.bar, start_ts, end_ts)
    if not bars:
        log.warning("加载到 0 根 K线，检查 inst/bar/时间范围")
        return 1

    validator = DataValidator()
    report = validator.validate(bars)

    log.info(f"加载 {len(bars)} 根 K线")
    log.info(f"校验结果: {report.summary()}")

    if report.duplicates:
        log.warning(f"重复时间戳 {len(report.duplicates)} 处:")
        for d in report.duplicates[:10]:
            log.warning(f"  ts={d.ts} count={d.count}")
        if len(report.duplicates) > 10:
            log.warning(f"  ... 还有 {len(report.duplicates) - 10} 处")

    if report.missing:
        log.warning(f"缺失区间 {len(report.missing)} 处:")
        for m in report.missing[:10]:
            log.warning(
                f"  prev={m.prev_ts} next={m.next_ts} "
                f"missing={m.missing_count}"
            )
        if len(report.missing) > 10:
            log.warning(f"  ... 还有 {len(report.missing) - 10} 处")

    if report.invalid_ohlc:
        log.error(f"OHLC 非法 {len(report.invalid_ohlc)} 处:")
        for r in report.invalid_ohlc[:10]:
            log.error(f"  ts={r.ts} reason={r.reason}")
        if len(report.invalid_ohlc) > 10:
            log.error(f"  ... 还有 {len(report.invalid_ohlc) - 10} 处")

    if report.errors:
        for e in report.errors:
            log.error(e)

    return 0 if report.is_ok else 1
