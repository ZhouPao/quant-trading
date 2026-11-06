"""上线检查清单（P0-2）：判断「这个策略够不够格切实盘」。

四项检查全部只读，数据源各自独立，任一项读取失败按 `unknown` 显式
降级并写进 ``degraded``（AGENTS #7），绝不静默当通过。**只给结论与
建议，不改变任何行为**——启停矩阵的 Pine 门禁拦截在 M4-16 已实现，
这里把散落的风险信号汇成一张清单。
"""
from __future__ import annotations

import json
import time
from typing import Any

from quant.foundation.config import Settings
from quant.state.sqlite_store import SQLiteStore
from quant.strategies.consistency import check as check_consistency
from quant.strategies.loader import StrategyConfigError
from quant.strategies.params import ParamError, resolve_params

#: 与启停矩阵同源的 Pine 偏离阈值（M4-16）
_DEVIATION_LIMIT = 0.05
#: 模拟盘连续运行的建议观察时长（天）
_MIN_PAPER_DAYS = 14
_MS_PER_DAY = 86_400_000


def _jsonable_params(params: dict) -> dict:
    return {
        k: (v if isinstance(v, (int, float, bool, str)) else str(v))
        for k, v in params.items()
    }


class ReadinessView:
    """只读聚合；同一实例可被 HTTP 路由与测试复用。"""

    def __init__(self, store: SQLiteStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    def _item(self, key: str, title: str, status: str, detail: str) -> dict:
        return {"key": key, "title": title, "status": status, "detail": detail}

    def _consistency(self, strategy_id: str, degraded: list[str]) -> dict:
        try:
            items = check_consistency()
        except Exception:  # noqa: BLE001 - 文件缺失等按未评估处理
            degraded.append("consistency")
            return self._item(
                "consistency",
                "Pine / Python 双副本参数一致",
                "unknown",
                "一致性校验执行失败（见 degraded）",
            )
        missing = [
            item
            for item in items
            if not item.known and item.strategy == strategy_id
        ]
        if not missing:
            return self._item(
                "consistency",
                "Pine / Python 双副本参数一致",
                "pass",
                "无未登记差异",
            )
        return self._item(
            "consistency",
            "Pine / Python 双副本参数一致",
            "fail",
            f"{len(missing)} 项未登记差异（运行 quant check-strategies 查看）",
        )

    def _pine(self, strategy_id: str, degraded: list[str]) -> dict:
        try:
            params, _sources = resolve_params(
                strategy_id,
                self._settings.engine.config_dir,
                self._store,
                resolve_from_db=self._settings.params.resolve_from_db,
            )
            params_text = json.dumps(
                _jsonable_params(params), sort_keys=True
            )
            row = self._store.worst_pine_deviation(strategy_id, params_text)
        except (StrategyConfigError, ParamError):
            return self._item(
                "pine",
                "Pine 交叉验证",
                "unknown",
                "策略参数解析失败，无法比对",
            )
        except Exception:  # noqa: BLE001
            degraded.append("pine")
            return self._item(
                "pine",
                "Pine 交叉验证",
                "unknown",
                "偏离度查询失败（见 degraded）",
            )
        if row is None:
            return self._item(
                "pine",
                "Pine 交叉验证",
                "unknown",
                "当前参数集未做过 Pine 对比（导入一次再上线）",
            )
        try:
            deviation = float(row["deviation"])
        except (TypeError, ValueError):
            return self._item(
                "pine", "Pine 交叉验证", "unknown", "偏离度记录不可读"
            )
        if deviation > _DEVIATION_LIMIT:
            return self._item(
                "pine",
                "Pine 交叉验证",
                "fail",
                f"最大偏离 {deviation:.2%} 超过 {_DEVIATION_LIMIT:.0%}（run {row['run_id'][:8]}）",
            )
        return self._item(
            "pine",
            "Pine 交叉验证",
            "pass",
            f"最大偏离 {deviation:.2%} ≤ {_DEVIATION_LIMIT:.0%}",
        )

    def _walkforward(self, strategy_id: str, degraded: list[str]) -> dict:
        try:
            _rows, total = self._store.list_backtest_runs(
                limit=1, strategy_id=strategy_id, status="done",
                kind="walkforward",
            )
        except Exception:  # noqa: BLE001
            degraded.append("walkforward")
            return self._item(
                "walkforward",
                "样本外检验（walk-forward）",
                "unknown",
                "回测历史查询失败（见 degraded）",
            )
        if total == 0:
            return self._item(
                "walkforward",
                "样本外检验（walk-forward）",
                "unknown",
                "该策略未跑过 walk-forward（防过拟合，建议先做一次）",
            )
        return self._item(
            "walkforward",
            "样本外检验（walk-forward）",
            "pass",
            f"已有 {total} 次完成的样本外检验",
        )

    def _paper_days(self, degraded: list[str]) -> dict:
        try:
            earliest = self._store.earliest_equity_ts()
        except Exception:  # noqa: BLE001
            degraded.append("equity")
            return self._item(
                "paper",
                "模拟盘连续运行",
                "unknown",
                "权益快照读取失败（见 degraded）",
            )
        if earliest is None:
            return self._item(
                "paper",
                "模拟盘连续运行",
                "unknown",
                "暂无权益快照（monitor 未运行或刚启动）",
            )
        now = int(time.time() * 1000)
        days = max(0.0, (now - earliest) / _MS_PER_DAY)
        if days < _MIN_PAPER_DAYS:
            return self._item(
                "paper",
                "模拟盘连续运行",
                "fail",
                f"仅 {days:.1f} 天（建议 ≥ {_MIN_PAPER_DAYS} 天再上实盘）",
            )
        return self._item(
            "paper",
            "模拟盘连续运行",
            "pass",
            f"已连续运行 {days:.0f} 天",
        )

    def snapshot(self, strategy_id: str | None = None) -> dict[str, Any]:
        sid = strategy_id or self._settings.engine.strategy
        degraded: list[str] = []
        items = [
            self._consistency(sid, degraded),
            self._pine(sid, degraded),
            self._walkforward(sid, degraded),
            self._paper_days(degraded),
        ]
        return {
            "ts": int(time.time() * 1000),
            "strategy": sid,
            "items": items,
            "ready": all(item["status"] == "pass" for item in items),
            "degraded": degraded,
            "note": (
                "清单只做汇总提示，不改变行为；启用绑定的 Pine 偏离门禁"
                "在启停矩阵上由后端拦截（M4-16）。"
            ),
        }
