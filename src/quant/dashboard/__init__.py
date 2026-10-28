"""只读运维看板（M4 扩展）。

- service：聚合 Redis 快照 + SQLite 本地状态
- app：FastAPI 只读 JSON API，可选托管前端构建产物
"""
from quant.dashboard.app import create_app
from quant.dashboard.service import DashboardService

__all__ = ["DashboardService", "create_app"]
