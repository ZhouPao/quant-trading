"""绩效归因（PRD §4.6.3 / M6-11）。

把组合相对「等权基准」的超额收益拆到单元，区分：

- ``allocation`` 权重配置贡献：把资金从低收益单元挪到高收益单元带来的部分
- ``selection`` 单元自身收益贡献：单元相对等权基准的表现
- ``base`` 等权基准收益（每项均分）

采用 Brinson-Fachler 的等权基准简化：
``base_i = R_b/N``、``alloc_i = (w_i - 1/N)·R_b``、
``select_i = w_i·(r_i - R_b)``，其中 ``w_i`` 是期初权重、
``r_i`` 是单元区间收益、``R_b = mean(r_i)``。
三项之和逐项累加后等于组合区间收益 ``R_p = Σ w_i·r_i``（单元权益完备时）。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from .models import PortfolioConfig, PortfolioSnapshot

_ZERO = Decimal("0")


class AttributionError(ValueError):
    """快照不足或权益数据不完备；显式报错，不静默给 0 贡献。"""


@dataclass(frozen=True, slots=True)
class AttributionRow:
    """某一单元在全部区间上的累计贡献（比例，非金额）。"""

    unit_key: str
    base: Decimal
    allocation: Decimal
    selection: Decimal
    total: Decimal


class Attribution:
    """由快照序列计算绩效归因。"""

    def __init__(
        self,
        snapshots: Sequence[PortfolioSnapshot],
        config: PortfolioConfig,
    ) -> None:
        if len(snapshots) < 2:
            raise AttributionError("need at least 2 snapshots for attribution")
        self._snapshots = list(snapshots)
        self._config = config

    def rows(self) -> list[AttributionRow]:
        keys = [unit.unit_key for unit in self._config.enabled_units()]
        if not keys:
            raise AttributionError("portfolio has no enabled units")
        count = Decimal(len(keys))
        acc = {
            key: {"base": _ZERO, "allocation": _ZERO, "selection": _ZERO}
            for key in keys
        }

        for prev, cur in zip(self._snapshots, self._snapshots[1:], strict=False):
            if prev.equity <= 0:
                raise AttributionError("previous equity must be positive")
            returns: dict[str, Decimal] = {}
            weights: dict[str, Decimal] = {}
            for key in keys:
                e0 = prev.unit_equity.get(key, _ZERO)
                e1 = cur.unit_equity.get(key, _ZERO)
                if e0 <= 0:
                    returns[key] = _ZERO
                    weights[key] = _ZERO
                else:
                    returns[key] = (e1 - e0) / e0
                    weights[key] = e0 / prev.equity
            benchmark = sum(returns.values(), _ZERO) / count
            for key in keys:
                acc[key]["base"] += benchmark / count
                acc[key]["allocation"] += (
                    weights[key] - Decimal(1) / count
                ) * benchmark
                acc[key]["selection"] += weights[key] * (
                    returns[key] - benchmark
                )

        return [
            AttributionRow(
                unit_key=key,
                base=values["base"],
                allocation=values["allocation"],
                selection=values["selection"],
                total=values["base"] + values["allocation"] + values["selection"],
            )
            for key, values in acc.items()
        ]

    def report(self) -> pd.DataFrame:
        """PRD §4.6.3 签名：返回 DataFrame（展示用 float，精确值见 ``rows()``）。"""
        return pd.DataFrame(
            [
                {
                    "unit_key": row.unit_key,
                    "base": float(row.base),
                    "allocation": float(row.allocation),
                    "selection": float(row.selection),
                    "total": float(row.total),
                }
                for row in self.rows()
            ],
            columns=["unit_key", "base", "allocation", "selection", "total"],
        )
