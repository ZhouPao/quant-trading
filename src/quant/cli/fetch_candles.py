"""fetch-candles 命令：拉取历史 K线并保存到 parquet（spec §11.1）。

用法：
  quant fetch-candles --inst BTC-USDT --bar 1H --start 2024-01-01 --end 2024-12-31
"""
from __future__ import annotations

from datetime import UTC, datetime

from quant.fetcher.candle_fetcher import CandleFetcher
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.okx.client import OKXClient


def parse_date_to_ms(date_str: str) -> int:
    """将 YYYY-MM-DD 转为 UTC 毫秒时间戳。"""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def main(args) -> int:
    """拉取历史 K线。返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.fetch_candles")

    start_ts = parse_date_to_ms(args.start)
    end_ts = parse_date_to_ms(args.end)

    if start_ts >= end_ts:
        log.error(f"start ({args.start}) must be before end ({args.end})")
        return 1

    log.info(
        f"开始拉取: inst={args.inst} bar={args.bar} "
        f"start={args.start} end={args.end}"
    )

    client = OKXClient(settings.okx)
    try:
        fetcher = CandleFetcher(client, settings.storage.data_dir)
        count = fetcher.fetch(
            inst_id=args.inst,
            bar_size=args.bar,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        log.info(f"拉取完成: 净增 {count} 根 K线")
        return 0
    except Exception as e:
        log.error(f"拉取失败: {e}")
        return 1
    finally:
        client.close()
