"""fetch-open-interest 命令：拉取永续合约持仓量并落盘（M2-14）。

用法：
  quant fetch-open-interest --inst BTC-USDT-SWAP
  quant fetch-open-interest --inst BTC-USDT-SWAP --inst ETH-USDT-SWAP
"""
from __future__ import annotations

from pathlib import Path

from quant.fetcher.open_interest_fetcher import OpenInterestFetcher
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger
from quant.okx.client import OKXClient

_log = get_logger("cli.fetch_open_interest")


def main(args) -> int:
    """拉取持仓量并落盘。"""
    settings = Settings.load(Path("config/config.toml"))
    client = OKXClient(settings.okx)

    try:
        fetcher = OpenInterestFetcher(client, settings.data.data_dir)
        total_added = 0
        for inst_id in args.inst:
            added = fetcher.fetch(inst_id)
            total_added += added
        _log.info(f"fetch_open_interest done total_added={total_added}")
        return 0
    finally:
        client.close()
