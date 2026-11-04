"""因子看板服务（M5-15 后端）。

只读：列表 / 因子值 / 评价指标 / 相关性。计算在 `to_thread` 里跑，
避免阻塞事件循环；品种数与因子数设上限，防止一次请求拉爆。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pandas as pd

from quant.backtest.runner import BacktestSetupError, parse_range
from quant.factors import (
    FactorProvider,
    FactorStore,
    correlation_matrix,
    evaluate,
    get_factor,
    list_factors,
)
from quant.foundation.config import Settings
from quant.storage.bar_storage import BarStorage

#: 单次请求的品种 / 因子上限
MAX_INSTS = 10
MAX_FACTORS = 8


class FactorError(Exception):
    """请求参数或数据问题；路由转 400。"""


class FactorNotFound(FactorError):
    """未知因子 ID；路由转 404。"""


def parse_params(raw: str | None) -> dict | None:
    """`params` 查询参数是 JSON 文本；空返回 None，坏 JSON 报 400。"""
    if raw is None or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise FactorError(f"params 不是合法 JSON：{e}") from e
    if not isinstance(payload, dict):
        raise FactorError("params 必须是 JSON 对象")
    return payload


def _split_insts(insts: str, default: str | None) -> list[str]:
    values = [item.strip() for item in insts.split(",") if item.strip()]
    if not values and default:
        values = [default]
    if not values:
        raise FactorError("至少需要一个品种")
    if len(values) > MAX_INSTS:
        raise FactorError(f"最多 {MAX_INSTS} 个品种")
    return values


class FactorService:
    """看板侧的因子数据服务。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._store = FactorStore(settings.storage.data_dir)
        self._bars = BarStorage(settings.storage.data_dir)
        self._provider = FactorProvider(self._store, self._bars)

    def list(self) -> list[dict[str, object]]:
        return [
            {
                "factor_id": spec.factor_id,
                "name": spec.name,
                "category": spec.category,
                "scope": spec.scope,
                "lookback": spec.lookback,
                "default_params": dict(spec.default_params),
                "description": spec.description,
                "aliases": list(spec.aliases),
            }
            for spec in list_factors()
        ]

    def _range(self, start: str, end: str) -> tuple[int, int]:
        try:
            return parse_range(start, end)
        except BacktestSetupError as e:
            raise FactorError(str(e)) from e

    def values(
        self,
        factor_id: str,
        inst: str,
        bar: str,
        start: str,
        end: str,
        params: Mapping | None = None,
    ) -> dict[str, object]:
        start_ts, end_ts = self._range(start, end)
        try:
            get_factor(factor_id)
            rows = self._provider.compute(
                factor_id, inst, bar, start_ts, end_ts, params=params
            )
        except KeyError as e:
            raise FactorNotFound(str(e)) from e
        return {
            "factor_id": factor_id,
            "inst": inst,
            "bar": bar,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "values": [
                {"ts": ts, "value": None if value is None else str(value)}
                for ts, value in rows
            ],
        }

    def evaluate(
        self,
        factor_id: str,
        insts: str,
        bar: str,
        start: str,
        end: str,
        *,
        horizon: int = 1,
        quantiles: int = 5,
    ) -> dict[str, object]:
        start_ts, end_ts = self._range(start, end)
        targets = _split_insts(insts, self._settings.engine.inst)
        try:
            get_factor(factor_id)
        except KeyError as e:
            raise FactorNotFound(str(e)) from e

        factor_values: dict[str, dict[int, object]] = {}
        prices: dict[str, dict[int, object]] = {}
        for inst in targets:
            rows = self._provider.compute(
                factor_id, inst, bar, start_ts, end_ts
            )
            factor_values[inst] = {
                ts: value for ts, value in rows if value is not None
            }
            prices[inst] = {
                item.ts: item.close
                for item in self._bars.load_bars(
                    inst, bar, start_ts, end_ts
                )
            }
        try:
            result = evaluate(
                factor_values,
                prices,
                horizon=horizon,
                quantiles=quantiles,
            )
        except ValueError as e:
            raise FactorError(str(e)) from e
        return {
            "factor_id": factor_id,
            "insts": targets,
            "bar": bar,
            "horizon": result.horizon,
            "ic": [
                {"ts": ts, "value": value} for ts, value in result.ic
            ],
            "rank_ic": [
                {"ts": ts, "value": value} for ts, value in result.rank_ic
            ],
            "ic_mean": result.ic_mean,
            "rank_ic_mean": result.rank_ic_mean,
            "icir": result.icir,
            "quantile_returns": [
                {"ts": ts, "values": row}
                for ts, row in result.quantile_returns
            ],
            "long_short": [
                {"ts": ts, "value": value}
                for ts, value in result.long_short
            ],
            "decay": [
                {"lag": lag, "ic": value} for lag, value in result.decay
            ],
            "turnover": result.turnover,
        }

    def meta(
        self, inst: str | None = None, bar: str | None = None
    ) -> dict[str, object]:
        """因子页元信息：可用品种/周期与指定组合的数据覆盖范围。

        只读 parquet 的 `ts` 列统计首尾与条数，不载入整表；失败按「无数据」
        降级并返回原因，不让页面 500。前端据此把默认区间落到真实数据上，
        并替换掉硬编码的多品种列表。
        """
        root = Path(self._settings.storage.data_dir) / "candles"
        insts: set[str] = set()
        if root.is_dir():
            for type_dir in root.iterdir():
                if not type_dir.is_dir():
                    continue
                for inst_dir in type_dir.iterdir():
                    if inst_dir.is_dir() and any(inst_dir.rglob("*.parquet")):
                        insts.add(inst_dir.name)
        inst_list = sorted(insts)

        target_inst = inst or (
            inst_list[0] if inst_list else self._settings.engine.inst
        )
        try:
            bars = self._bars.list_bar_sizes(target_inst)
        except Exception:  # noqa: BLE001  非法品种/目录异常一律降级为空
            bars = []
        target_bar = bar or (bars[0] if bars else self._settings.engine.bar)

        first_ts: int | None = None
        last_ts: int | None = None
        count = 0
        reason: str | None = None
        try:
            years = self._bars.list_years(target_inst, target_bar)
            if not years:
                reason = f"本地没有 {target_inst} {target_bar} 的 K 线"
            for year in years:
                # 复用 BarStorage 的路径推断，避免在这里重写 inst_type 规则
                path = self._bars._path_for(target_inst, target_bar, year)
                frame = pd.read_parquet(path, columns=["ts"])
                if frame.empty:
                    continue
                count += len(frame)
                low = int(frame["ts"].min())
                high = int(frame["ts"].max())
                first_ts = low if first_ts is None else min(first_ts, low)
                last_ts = high if last_ts is None else max(last_ts, high)
        except Exception as e:  # noqa: BLE001
            reason = f"读取 K 线范围失败：{e}"

        return {
            "insts": inst_list,
            "bars": bars,
            "inst": target_inst,
            "bar": target_bar,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "count": count,
            "reason": reason,
        }

    def correlations(
        self,
        factor_ids: str,
        inst: str,
        bar: str,
        start: str,
        end: str,
    ) -> dict[str, object]:
        ids = [item.strip() for item in factor_ids.split(",") if item.strip()]
        if not ids:
            raise FactorError("至少需要一个因子")
        if len(ids) > MAX_FACTORS:
            raise FactorError(f"最多 {MAX_FACTORS} 个因子")
        start_ts, end_ts = self._range(start, end)

        series: dict[str, Sequence] = {}
        for factor_id in ids:
            try:
                get_factor(factor_id)
            except KeyError as e:
                raise FactorNotFound(str(e)) from e
            rows = self._provider.compute(
                factor_id, inst, bar, start_ts, end_ts
            )
            series[factor_id] = [value for _, value in rows]
        result = correlation_matrix(series)
        return {
            "inst": inst,
            "bar": bar,
            "factor_ids": ids,
            "matrix": [
                {"a": a, "b": b, "corr": corr}
                for (a, b), corr in result.matrix.items()
            ],
            "redundant": [
                {"a": a, "b": b} for a, b in result.redundant
            ],
        }


__all__ = ["MAX_FACTORS", "MAX_INSTS", "FactorError", "FactorService", "parse_params"]
