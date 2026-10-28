"""fetch-instruments 命令：拉取 OKX 产品配置并落盘（spec §11.1）。

用法：
  quant fetch-instruments --inst-type SPOT
  quant fetch-instruments --inst-type SWAP
"""
from __future__ import annotations

from quant.fetcher.instrument_fetcher import InstrumentFetcher
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.okx.client import OKXClient


def main(args) -> int:
    """拉取产品配置。返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.fetch_instruments")

    inst_type = args.inst_type.upper()
    if inst_type not in ("SPOT", "SWAP", "FUTURES", "OPTION"):
        log.error(
            f"inst_type 必须是 SPOT/SWAP/FUTURES/OPTION, 收到: {inst_type}"
        )
        return 1

    log.info(f"开始拉取: inst_type={inst_type}")

    client = OKXClient(settings.okx)
    try:
        fetcher = InstrumentFetcher(client, settings.storage.data_dir)
        count = fetcher.fetch(inst_type=inst_type)
        log.info(f"拉取完成: {count} 条 live 产品已落盘")
        return 0
    except Exception as e:
        log.error(f"拉取失败: {e}")
        return 1
    finally:
        client.close()
