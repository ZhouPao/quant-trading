"""因子提供器（M5-5 / M5-12 / M5-13）。

回测与实盘共用这一个类，保证「窗口裁剪 / 增量回退 / 缺失处理」口径一致。

增量规则：已有缓存时从 `last_cached_ts - lookback * bar_ms` 起算，**回退
lookback 根做重叠计算**——否则窗口边界上的滚动指标会在增量拼接处跳变。
缓存只存原始值（`None` 保留），`fill`/`clip` 只在返回时应用，避免把
「前值填充」这种展示口径写进缓存。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from quant.backtest.runner import BAR_MS
from quant.factors.registry import get_factor
from quant.factors.storage import FactorStore
from quant.models.bar import Bar
from quant.storage.bar_storage import BarStorage

#: 缺失值处理：nan=保留空值；prev=前值填充；skip=直接剔除
FILL_MODES = frozenset({"nan", "prev", "skip"})


class FactorProvider:
    """按区间提供因子值：命中缓存直接返回，缺失段计算后落盘。"""

    def __init__(self, store: FactorStore, bars: BarStorage) -> None:
        self._store = store
        self._bars = bars

    def compute(
        self,
        factor_id: str,
        inst_id: str,
        bar: str,
        start_ts: int,
        end_ts: int,
        *,
        params: Mapping | None = None,
        fill: str = "nan",
        clip_quantile: float | None = None,
    ) -> list[tuple[int, Decimal | None]]:
        """返回 [start_ts, end_ts] 内的因子值（ts 升序）。

        Args:
            params: 覆盖因子默认参数；不同参数集走独立缓存
            fill: `nan`（默认）/`prev`/`skip`
            clip_quantile: 非空时按该分位裁剪极值（如 0.01 = 1%/99%）
        """
        if bar not in BAR_MS:
            raise ValueError(f"unsupported bar: {bar}")
        if fill not in FILL_MODES:
            raise ValueError(f"unknown fill mode: {fill}")
        spec = get_factor(factor_id)
        merged = dict(spec.default_params)
        merged.update(params or {})
        bar_ms = BAR_MS[bar]

        cached = self._store.load(
            factor_id, merged, inst_id, bar, start_ts, end_ts
        )
        cached_map = dict(cached)
        window = self._bars.load_bars(inst_id, bar, start_ts, end_ts)
        # 命中：请求区间内每根 bar 都有缓存值（含 None，None 也是有效值）
        if window and all(item.ts in cached_map for item in window):
            return self._postprocess(
                [(item.ts, cached_map[item.ts]) for item in window],
                fill,
                clip_quantile,
            )

        # 增量起点：有缓存时从最后缓存点回退 lookback 根
        warm_start = start_ts
        if cached:
            last_ts = max(ts for ts, _ in cached)
            if last_ts >= start_ts:
                warm_start = max(start_ts, last_ts - spec.lookback * bar_ms)

        calc = self._bars.load_bars(
            inst_id, bar, warm_start - spec.lookback * bar_ms, end_ts
        )
        if not calc:
            return self._postprocess(cached, fill, clip_quantile)

        values = spec.compute(calc, merged)
        if len(values) != len(calc):
            raise ValueError(
                f"{factor_id} returned {len(values)} values for "
                f"{len(calc)} bars (must be equal length)"
            )
        rows = [
            (item.ts, value)
            for item, value in zip(calc, values, strict=True)
            if warm_start <= item.ts <= end_ts
        ]
        if rows:
            self._store.save(factor_id, merged, inst_id, bar, rows)

        merged_rows = cached_map.copy()
        merged_rows.update(dict(rows))
        return self._postprocess(
            sorted(merged_rows.items()), fill, clip_quantile
        )

    @staticmethod
    def _postprocess(
        rows: Sequence[tuple[int, Decimal | None]],
        fill: str,
        clip_quantile: float | None,
    ) -> list[tuple[int, Decimal | None]]:
        cleaned = list(rows)
        if fill == "prev":
            last: Decimal | None = None
            filled: list[tuple[int, Decimal | None]] = []
            for ts, value in cleaned:
                if value is None:
                    filled.append((ts, last))
                else:
                    last = value
                    filled.append((ts, value))
            cleaned = filled

        if clip_quantile is not None:
            if not 0 <= clip_quantile < 0.5:
                raise ValueError("clip_quantile must be in [0, 0.5)")
            values = sorted(
                value for _, value in cleaned if value is not None
            )
            if values and clip_quantile > 0:
                low = values[int(len(values) * clip_quantile)]
                high = values[
                    min(len(values) - 1, int(len(values) * (1 - clip_quantile)))
                ]
                cleaned = [
                    (ts, None if v is None else max(low, min(high, v)))
                    for ts, v in cleaned
                ]

        if fill == "skip":
            cleaned = [(ts, v) for ts, v in cleaned if v is not None]
        return cleaned

    def values_for(
        self,
        factor_id: str,
        inst_id: str,
        bar: str,
        bars: Sequence[Bar],
        *,
        params: Mapping | None = None,
        fill: str = "nan",
    ) -> list[Decimal | None]:
        """把因子值对齐到给定 bar 窗口（供引擎填入 `Context.factors`）。

        只取窗口内 ts 对应的值；窗口外的缓存不参与。这是 M5-13 要求的
        「回测与实盘同一窗口裁剪规则」的落点。
        """
        if not bars:
            return []
        values = self.compute(
            factor_id,
            inst_id,
            bar,
            bars[0].ts,
            bars[-1].ts,
            params=params,
            fill=fill,
        )
        by_ts = dict(values)
        return [by_ts.get(item.ts) for item in bars]


__all__ = ["FILL_MODES", "FactorProvider"]
