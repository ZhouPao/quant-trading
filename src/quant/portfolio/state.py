"""组合快照读写门面（M6-10）。

把 ``SQLiteStore`` 的原始 dict 与领域模型 ``PortfolioSnapshot`` 互相转换，
使 state 层不反向依赖 portfolio 包。金额在 DB 里是 Decimal 字符串，
读回后是 Decimal（AGENTS #1）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from .models import PortfolioSnapshot

if TYPE_CHECKING:
    from quant.state.sqlite_store import SQLiteStore


class PortfolioStore:
    """一个组合的快照/状态仓库。"""

    def __init__(self, store: SQLiteStore, portfolio_id: str) -> None:
        if not portfolio_id:
            raise ValueError("portfolio_id must not be empty")
        self._store = store
        self._portfolio_id = portfolio_id

    def save(self, snapshot: PortfolioSnapshot) -> None:
        if snapshot.portfolio_id != self._portfolio_id:
            raise ValueError(
                f"snapshot belongs to {snapshot.portfolio_id!r}, "
                f"store is {self._portfolio_id!r}"
            )
        self._store.save_portfolio_snapshot(
            snapshot.portfolio_id,
            snapshot.ts,
            snapshot.equity,
            snapshot.unit_equity,
            snapshot.weights,
            snapshot.drawdown,
        )

    def snapshots(self, limit: int = 1000) -> list[PortfolioSnapshot]:
        raw = self._store.get_portfolio_snapshots(self._portfolio_id, limit)
        return [PortfolioSnapshot(**cast(dict[str, Any], row)) for row in raw]

    def save_state(
        self,
        weights: Mapping[str, Decimal],
        enabled_units: Sequence[str],
        ts: int,
    ) -> None:
        self._store.save_portfolio_state(
            self._portfolio_id, weights, enabled_units, ts
        )

    def state(self) -> tuple[dict[str, Decimal], set[str], int] | None:
        return self._store.get_portfolio_state(self._portfolio_id)
