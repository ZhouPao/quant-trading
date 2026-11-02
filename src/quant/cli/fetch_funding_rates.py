"""fetch-funding-rates 命令：拉取永续合约资金费率并落盘（spec §11.1）。

用法：
  quant fetch-funding-rates --inst BTC-USDT-SWAP
  quant fetch-funding-rates --inst BTC-USDT-SWAP --start 2024-01-01 [--end 2024-12-31]

不传 `--start` 只拉最近一条；传了则按区间回补历史（M2-4），供 M4-15
资金费率成本回测使用。
"""
from __future__ import annotations

from quant.cli.fetch_candles import parse_date_to_ms
from quant.fetcher.funding_rate_fetcher import FundingRateFetcher
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.okx.client import OKXClient


def main(args) -> int:
    """拉取资金费率。返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.fetch_funding_rates")

    if not args.inst:
        log.error("inst 不能为空")
        return 1
    if args.end and not args.start:
        log.error("--end 需要与 --start 一起使用")
        return 1

    start_ts = parse_date_to_ms(args.start) if args.start else None
    end_ts = parse_date_to_ms(args.end) if args.end else None

    if start_ts is not None and end_ts is not None and start_ts >= end_ts:
        log.error(f"--start ({args.start}) must be before --end ({args.end})")
        return 1

    mode = f"history {args.start}~{args.end or 'now'}" if start_ts else "latest"
    log.info(f"开始拉取: inst={args.inst} mode={mode}")

    client = OKXClient(settings.okx)
    try:
        fetcher = FundingRateFetcher(client, settings.storage.data_dir)
        if start_ts is None:
            count = fetcher.fetch(inst_id=args.inst)
        else:
            count = fetcher.fetch_history(
                inst_id=args.inst,
                start_ts=start_ts,
                end_ts=end_ts,
                max_pages=args.max_pages,
            )
        if count > 0:
            log.info(f"拉取完成: 净增 {count} 条资金费率记录")
        else:
            log.info("拉取完成: 无新增（数据已存在或 OKX 无返回）")
        return 0
    except Exception as e:
        log.error(f"拉取失败: {e}")
        return 1
    finally:
        client.close()
