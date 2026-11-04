"""`quant backup`：备份 SQLite 状态库与配置快照（M10-7）。"""
from __future__ import annotations

from quant.foundation.backup import BackupError, create_backup
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger


def main(args) -> int:
    """执行备份，返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.backup")

    try:
        result = create_backup(
            settings,
            out_dir=args.out,
            config_path=args.config,
        )
    except BackupError as e:
        log.error(str(e))
        return 1

    print(f"备份完成：{result.dir}")
    for item in result.files:
        print(
            f"  {item['kind']:<8} {item['name']}  "
            f"{item['size']} bytes  sha256={str(item['sha256'])[:12]}…"
        )
    print(f"清单：{result.manifest}")
    return 0
