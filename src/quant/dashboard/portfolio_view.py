"""组合页只读聚合（M6-14）。

把 8-1 ~ 8-3 的领域层结果（配置、目标权重、快照、归因、再平衡留痕）
拼成一次响应；不做计算、不下单。读取失败按 AGENTS #7 显式写进
``degraded``，绝不把缺失数据渲染成 0。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from quant.foundation.config import Settings
from quant.portfolio import (
    AllocationError,
    Allocator,
    Attribution,
    AttributionError,
    PortfolioConfig,
    PortfolioConfigError,
    PortfolioSnapshot,
    PortfolioStore,
    RebalancePlan,
    Rebalancer,
    load_portfolios,
)
from quant.state.sqlite_store import SQLiteStore


def _num(value: Decimal) -> str:
    """金额/比例一律以字符串出网，避免 JSON float 丢精度。"""
    return str(value)


def _snapshot_row(snap: PortfolioSnapshot) -> dict[str, Any]:
    return {
        "ts": snap.ts,
        "equity": _num(snap.equity),
        "drawdown": _num(snap.drawdown),
        "unit_equity": {k: _num(v) for k, v in snap.unit_equity.items()},
        "weights": {k: _num(v) for k, v in snap.weights.items()},
    }


class PortfolioView:
    """组合页数据门面；同一实例也供 ``quant portfolio-tick`` 写快照。"""

    def __init__(self, store: SQLiteStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    def _configs(self, degraded: list[str]) -> list[PortfolioConfig]:
        try:
            return load_portfolios(self._settings.portfolio.config_dir)
        except (PortfolioConfigError, OSError):
            degraded.append("portfolio_config")
            return []

    def _pick(
        self, configs: list[PortfolioConfig]
    ) -> PortfolioConfig | None:
        active = self._settings.portfolio.active
        if active:
            return next((c for c in configs if c.portfolio_id == active), None)
        return configs[0] if configs else None

    def snapshot(self, limit: int = 200) -> dict[str, Any]:
        cfg = self._settings.portfolio
        degraded: list[str] = []
        configs = self._configs(degraded)
        picked = self._pick(configs)
        base: dict[str, Any] = {
            "enabled": cfg.enabled,
            "portfolio_id": picked.portfolio_id if picked else None,
            "available": [c.portfolio_id for c in configs],
            "degraded": degraded,
            "reason": None,
            "units": [],
            "weights": {},
            "snapshots": [],
            "attribution": [],
            "rebalance_attempts": [],
            "allocator": None,
            "rebalance": None,
            "risk": None,
            "state": None,
        }
        if picked is None:
            base["reason"] = (
                "组合配置读取失败（见 degraded）"
                if degraded
                else "未配置组合：config/portfolios/ 下没有 <id>.toml"
            )
            return base

        try:
            target = Allocator(picked).weights()
        except AllocationError:
            degraded.append("allocator")
            target = {}
        base["allocator"] = picked.allocator
        base["weights"] = {k: _num(v) for k, v in target.items()}
        base["units"] = [
            {
                "unit_key": unit.unit_key,
                "strategy_id": unit.strategy_id,
                "inst_id": unit.inst_id,
                "enabled": unit.enabled,
                "weight": _num(target.get(unit.unit_key, Decimal("0"))),
            }
            for unit in picked.units
        ]
        base["rebalance"] = {
            "mode": picked.rebalance.mode,
            "interval_hours": picked.rebalance.interval_hours,
            "drift_pct": _num(picked.rebalance.drift_pct),
            "cooldown_hours": picked.rebalance.cooldown_hours,
        }
        base["risk"] = {
            "max_drawdown_pct": _num(picked.risk.max_drawdown_pct),
            "max_correlation": _num(picked.risk.max_correlation),
            "max_unit_contribution_pct": _num(
                picked.risk.max_unit_contribution_pct
            ),
        }

        repo = PortfolioStore(self._store, picked.portfolio_id)
        try:
            snaps = repo.snapshots(limit)
        except Exception:  # noqa: BLE001 - DB 读取失败必须显式降级
            degraded.append("sqlite")
            snaps = []
        base["snapshots"] = [_snapshot_row(s) for s in snaps]

        if len(snaps) >= 2:
            try:
                base["attribution"] = [
                    {
                        "unit_key": row.unit_key,
                        "allocation": _num(row.allocation),
                        "selection": _num(row.selection),
                        "total": _num(row.total),
                    }
                    for row in Attribution(snaps, picked).rows()
                ]
            except AttributionError:
                base["attribution"] = []
        try:
            base["rebalance_attempts"] = self._store.list_rebalance_attempts(
                picked.portfolio_id, 50
            )
        except Exception:  # noqa: BLE001
            degraded.append("sqlite")
        try:
            state = repo.state()
            base["state"] = (
                None
                if state is None
                else {
                    "weights": {k: _num(v) for k, v in state[0].items()},
                    "enabled_units": sorted(state[1]),
                    "updated_ts": state[2],
                }
            )
        except Exception:  # noqa: BLE001
            degraded.append("sqlite")
        if not cfg.enabled:
            base["reason"] = "组合层未启用（[portfolio] enabled=false）：只展示配置，不产生调仓"
        return base

    def tick(
        self, equity: Decimal, now_ts: int
    ) -> tuple[PortfolioSnapshot | None, RebalancePlan | None, str | None]:
        """快照生产者 + 再平衡评估（由 CLI 调用，不自动提交）。

        单元权益按目标权重分摊总权益（组合层只产出目标权重，不逐单元读持仓）；
        这点在响应里显式标注，避免被误读成真实持仓估值。
        """
        degraded: list[str] = []
        configs = self._configs(degraded)
        picked = self._pick(configs)
        if picked is None:
            return None, None, "未配置组合或配置读取失败"
        if equity <= 0:
            return None, None, "总权益必须为正"

        weights = Allocator(picked).weights()
        unit_equity = {k: equity * w for k, w in weights.items()}
        repo = PortfolioStore(self._store, picked.portfolio_id)
        previous = repo.snapshots()
        peak = max(
            [equity, *(s.equity for s in previous)],
            default=equity,
        )
        drawdown = (peak - equity) / peak if peak > 0 else Decimal("0")
        snap = PortfolioSnapshot(
            portfolio_id=picked.portfolio_id,
            ts=now_ts,
            equity=equity,
            unit_equity=unit_equity,
            weights=weights,
            drawdown=drawdown,
        )
        repo.save(snap)
        repo.save_state(
            weights,
            [u.unit_key for u in picked.enabled_units()],
            now_ts,
        )
        last = previous[-1].ts if previous else None
        plan = Rebalancer(picked).evaluate(snap, last_rebalance_ts=last)
        return snap, plan, None
