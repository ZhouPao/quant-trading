"""fetch-funding-rates 命令：拉取永续合约资金费率并落盘（spec §11.1）。

用法：
  quant fetch-funding-rates --inst BTC-USDT-SWAP
"""
from __future__ import annotations

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

    log.info(f"开始拉取: inst={args.inst}")

    client = OKXClient(settings.okx)
    try:
        fetcher = FundingRateFetcher(client, settings.storage.data_dir)
        count = fetcher.fetch(inst_id=args.inst)
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
