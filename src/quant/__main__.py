"""`python -m quant` 入口，等同 `quant` 命令。

supervisor 等需要以子进程方式拉起其他 CLI 子命令，用 `-m` 可避免
依赖 PATH 上的 `quant` 脚本（venv 未激活时也能工作）。
"""
from __future__ import annotations

import sys

from quant.cli import main

if __name__ == "__main__":
    sys.exit(main())
