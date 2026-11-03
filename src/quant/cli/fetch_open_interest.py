"""fetch-open-interest 命令：拉取永续合约持仓量并落盘（M2-14）。

用法：
  quant fetch-open-interest --inst BTC-USDT-SWAP
  quant fetch-open-interest --inst BTC-USDT-SWAP --inst ETH-USDT-SWAP
"""
from __future__ import annotations

from quant.fetcher.open_interest_fetcher import OpenInterestFetcher
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.okx.client import OKXClient


def main(args) -> int:
    """拉取持仓量并落盘。返回退出码。"""
    # 尊重 --config 参数（原先写死 config/config.toml，参数形同虚设）
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.fetch_open_interest")

    client = OKXClient(settings.okx)
    try:
        # P0 修复：数据目录是 settings.storage.data_dir（原先写成不存在的
        # settings.data，本命令一调即崩 AttributeError）
        fetcher = OpenInterestFetcher(client, settings.storage.data_dir)
        total_added = 0
        for inst_id in args.inst:
            added = fetcher.fetch(inst_id)
            total_added += added
        log.info(f"fetch_open_interest done total_added={total_added}")
        return 0
    except Exception as e:  # noqa: BLE001
        log.error(f"拉取失败: {e}")
        return 1
    finally:
        client.close()
