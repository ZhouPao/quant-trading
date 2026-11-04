"""当前代码版本（供审计追溯，MS4-C §3.3）。

参数变更与回滚都要记 `git_commit`：回滚记录原先恒为 NULL，审计链里
「这次回滚发生在哪版代码上」不可追溯（P2）。取不到版本时返回 None，
绝不影响主流程。
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def git_commit() -> str | None:
    """短 commit hash；非 git 环境或超时返回 None。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            cwd=Path(__file__).resolve().parents[3],
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit or None


__all__ = ["git_commit"]
