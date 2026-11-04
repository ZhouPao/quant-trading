"""check-strategies 命令：双副本一致性校验（M3-13）。

用法：
  quant check-strategies              # 列出全部差异（含已登记），退出码 0
  quant check-strategies --strict     # 存在未登记差异时退出 1（CI 用）
  quant check-strategies --pine-dir ... --config-dir ...
"""
from __future__ import annotations

from pathlib import Path

from quant.foundation.logger import get_logger, setup_logger
from quant.strategies.consistency import check, unregistered


def main(args) -> int:
    """执行校验，返回退出码。"""
    setup_logger()
    log = get_logger("cli.check_strategies")

    items = check(
        Path(args.pine_dir) if args.pine_dir else None,
        Path(args.config_dir) if args.config_dir else None,
    )
    missing = unregistered(items)

    for item in items:
        if item.known:
            print(item.text())
    for item in missing:
        print(item.text())

    if not items:
        print("一致性校验通过：未发现任何差异")
        return 0

    print(
        f"共 {len(items)} 项差异，其中未登记 {len(missing)} 项"
        f"（已登记差异属基线副本与生产参数的有意区别）"
    )
    if missing and args.strict:
        log.error("存在未登记漂移：要么消除差异，要么在 consistency.py 登记")
        return 1
    return 0
