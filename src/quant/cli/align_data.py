"""align-data 命令：多品种 K线对齐（spec §11.2）。

用法：
  quant align-data --insts BTC-USDT,ETH-USDT --bar 1H --fill ffill
  quant align-data --insts BTC-USDT,ETH-USDT --bar 1H \\
      --start 2024-01-01 --end 2024-12-31 --output aligned.parquet
"""
from __future__ import annotations

from pathlib import Path

from quant.cli.fetch_candles import parse_date_to_ms
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.storage.aligner import DataAligner
from quant.storage.bar_storage import BarStorage


def main(args) -> int:
    """多品种 K线对齐。返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.align_data")

    inst_ids = [s.strip() for s in args.insts.split(",") if s.strip()]
    if len(inst_ids) < 2:
        log.error("至少需要 2 个品种才能对齐")
        return 1

    storage = BarStorage(settings.storage.data_dir)

    # 确定时间范围
    if args.start and args.end:
        start_ts = parse_date_to_ms(args.start)
        end_ts = parse_date_to_ms(args.end)
    else:
        # 全量：取所有品种已有年份的最小/最大范围
        all_years: list[int] = []
        for inst_id in inst_ids:
            all_years.extend(storage.list_years(inst_id, args.bar))
        if not all_years:
            log.warning(
                f"无本地数据: insts={inst_ids} bar={args.bar}，"
                f"请先运行 fetch-candles"
            )
            return 1
        start_ts = int(min(all_years) * 1e9)
        end_ts = int((max(all_years) + 1) * 1e9)

    log.info(
        f"对齐: insts={inst_ids} bar={args.bar} fill={args.fill} "
        f"range=[{args.start or 'all'}, {args.end or 'all'}]"
    )

    # 加载各品种数据
    bars_by_inst: dict[str, list] = {}
    for inst_id in inst_ids:
        bars = storage.load_bars(inst_id, args.bar, start_ts, end_ts)
        if not bars:
            log.warning(f"品种 {inst_id} 无数据，跳过")
            continue
        bars_by_inst[inst_id] = bars
        log.info(f"  {inst_id}: {len(bars)} 根")

    if len(bars_by_inst) < 2:
        log.error("有效数据品种不足 2 个，无法对齐")
        return 1

    aligner = DataAligner()
    df = aligner.align(bars_by_inst, fill=args.fill)

    log.info(f"对齐完成: {len(df)} 行 x {len(df.columns)} 列")

    if args.output:
        out_path = Path(args.output)
        df.to_parquet(out_path)
        log.info(f"已写入: {out_path}")
    else:
        # 打印前 5 行预览
        log.info(f"预览:\n{df.head().to_string()}")

    return 0
