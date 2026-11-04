"""`quant restore`：从备份目录恢复状态库（M10-7，3-2）。"""
from __future__ import annotations

from quant.foundation.backup import RestoreError, restore_backup
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger


def main(args) -> int:
    """执行恢复（或 dry-run 校验），返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.restore")

    try:
        result = restore_backup(
            settings,
            args.backup,
            force=args.force,
            dry_run=args.dry_run,
        )
    except RestoreError as e:
        log.error(str(e))
        return 1

    if result.dry_run:
        print(
            f"dry-run 校验通过：{result.backup_dir}\n"
            f"  目标库：{result.target}\n"
            f"  备份 schema：v{result.schema_version}\n"
            "  未写入任何文件；去掉 --dry-run 才会真正恢复"
        )
        return 0

    print(f"已恢复：{result.backup_dir} -> {result.target}")
    if result.pre_backup is not None:
        print(f"  恢复前备份：{result.pre_backup}")
    print("  请重启 worker / dashboard 使连接指向新库")
    return 0
