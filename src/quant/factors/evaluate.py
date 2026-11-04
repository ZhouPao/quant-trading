"""因子评价（M5-6~11）。

输入是**宽表**：`factor_values[inst][ts]` 与 `prices[inst][ts]`。
所有统计量按 ts 横截面计算，缺品种不参与该 ts。

口径说明：
- IC 用 Pearson 相关，RankIC 先转平均排名再 Pearson；
- 分层默认 5 档，按因子值升序切分，`long_short = 最高档 - 最低档`；
- ICIR = IC 均值 / IC 样本标准差；标准差为 0（IC 恒定）时返回 None；
- 换手 = 相邻两个 ts 之间分档发生变化的比例；
- 指标本身是统计量，内部转 float 计算；**金额/价格不在这里落库**。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

DEFAULT_QUANTILES = 5
DEFAULT_DECAY_LAGS = 5
#: 相关性高于此值视为冗余因子（M5-11）
REDUNDANT_THRESHOLD = 0.8


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """单因子评价结果（所有序列按 ts 升序）。"""

    horizon: int
    ic: list[tuple[int, float | None]]
    rank_ic: list[tuple[int, float | None]]
    ic_mean: float | None
    rank_ic_mean: float | None
    icir: float | None
    quantile_returns: list[tuple[int, list[float | None]]]
    long_short: list[tuple[int, float | None]]
    decay: list[tuple[int, float | None]]
    turnover: float | None


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    """多因子相关性矩阵与冗余对。"""

    matrix: dict[tuple[str, str], float | None]
    redundant: list[tuple[str, str]]


# ---------------------------------------------------------------------------
# 基础统计
# ---------------------------------------------------------------------------


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson 相关；样本不足 3 或方差为 0 返回 None。"""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _rank(values: Sequence[float]) -> list[float]:
    """平均排名（并列取均值），用于 Spearman。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _stdev(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _icir(ic_values: Sequence[float]) -> float | None:
    if len(ic_values) < 2:
        return None
    mean = _mean(ic_values)
    stdev = _stdev(ic_values)
    if mean is None or stdev is None or stdev == 0:
        return None
    return mean / stdev


# ---------------------------------------------------------------------------
# 横截面标准化（M5-6）
# ---------------------------------------------------------------------------


def zscore(
    values: Mapping[str, Decimal | None],
) -> dict[str, Decimal | None]:
    """同一 ts 的横截面 z-score；缺失品种不参与统计也不输出。"""
    present = {k: float(v) for k, v in values.items() if v is not None}
    if len(present) < 2:
        return {k: None for k in values}
    mean = sum(present.values()) / len(present)
    variance = sum((v - mean) ** 2 for v in present.values()) / len(present)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return {k: Decimal("0") if k in present else None for k in values}
    # 用 present[k]（float）参与计算；直接用 v 会 Decimal - float 报错
    return {
        k: Decimal(str((present[k] - mean) / stdev)) if k in present else None
        for k in values
    }


def percentile(
    values: Mapping[str, Decimal | None],
) -> dict[str, Decimal | None]:
    """横截面百分位（0~1，平均排名处理并列）；缺失不参与。"""
    present = {k: float(v) for k, v in values.items() if v is not None}
    if not present:
        return {k: None for k in values}
    ranks = _rank(list(present.values()))
    size = len(present)
    result: dict[str, Decimal | None] = {k: None for k in values}
    for inst, rank in zip(present.keys(), ranks, strict=True):
        result[inst] = Decimal(str((rank - 1) / (size - 1) if size > 1 else 0))
    return result


# ---------------------------------------------------------------------------
# 评价主流程
# ---------------------------------------------------------------------------


def _forward_returns(
    prices: Mapping[str, Mapping[int, Decimal]], horizon: int
) -> dict[int, dict[str, float]]:
    """ts -> {inst: 未来 horizon 根收益}；尾部不足 horizon 的 ts 不出现。"""
    result: dict[int, dict[str, float]] = {}
    for inst, series in prices.items():
        items = sorted(series.items())
        for i in range(len(items) - horizon):
            ts, price = items[i]
            future = items[i + horizon][1]
            if price > 0:
                result.setdefault(ts, {})[inst] = float(future / price - 1)
    return result


def _bucket_of(
    xs: Mapping[str, float], quantiles: int
) -> dict[str, int]:
    """按因子值升序把品种分到 N 档（0=最低）。"""
    items = sorted(xs.items(), key=lambda kv: kv[1])
    size = len(items)
    return {
        inst: min(quantiles - 1, idx * quantiles // size)
        for idx, (inst, _) in enumerate(items)
    }


def _quantile_returns(
    xs: Mapping[str, float], ys: Mapping[str, float], quantiles: int
) -> list[float | None]:
    buckets: list[list[float]] = [[] for _ in range(quantiles)]
    for inst, bucket in _bucket_of(xs, quantiles).items():
        if inst in ys:
            buckets[bucket].append(ys[inst])
    return [sum(items) / len(items) if items else None for items in buckets]


def _turnover(
    buckets: Sequence[dict[str, int]],
) -> float | None:
    """相邻 ts 之间分档变化的比例（只统计两边都出现的品种）。"""
    if len(buckets) < 2:
        return None
    changes = 0
    total = 0
    # 相邻两两：长度天然差 1，所以 strict=False
    for prev, curr in zip(buckets, buckets[1:], strict=False):
        common = set(prev) & set(curr)
        changes += sum(1 for inst in common if prev[inst] != curr[inst])
        total += len(common)
    return changes / total if total else None


def _ic_for_horizon(
    factor_values: Mapping[str, Mapping[int, Decimal | None]],
    prices: Mapping[str, Mapping[int, Decimal]],
    horizon: int,
) -> list[tuple[int, float | None]]:
    fwd = _forward_returns(prices, horizon)
    series: list[tuple[int, float | None]] = []
    for ts in sorted(fwd):
        xs: list[float] = []
        ys: list[float] = []
        for inst, by_ts in factor_values.items():
            value = by_ts.get(ts)
            if value is None or inst not in fwd[ts]:
                continue
            xs.append(float(value))
            ys.append(fwd[ts][inst])
        series.append((ts, _pearson(xs, ys)))
    return series


def evaluate(
    factor_values: Mapping[str, Mapping[int, Decimal | None]],
    prices: Mapping[str, Mapping[int, Decimal]],
    *,
    horizon: int = 1,
    quantiles: int = DEFAULT_QUANTILES,
    decay_lags: int = DEFAULT_DECAY_LAGS,
) -> EvaluationResult:
    """单因子评价：IC/RankIC/ICIR、分层收益、多空、衰减、换手。"""
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    if quantiles < 2:
        raise ValueError("quantiles must be >= 2")

    fwd = _forward_returns(prices, horizon)
    ic_series: list[tuple[int, float | None]] = []
    rank_series: list[tuple[int, float | None]] = []
    quantile_rows: list[tuple[int, list[float | None]]] = []
    long_short: list[tuple[int, float | None]] = []
    bucket_rows: list[dict[str, int]] = []

    for ts in sorted(fwd):
        xs: dict[str, float] = {}
        ys: dict[str, float] = {}
        for inst, by_ts in factor_values.items():
            value = by_ts.get(ts)
            if value is None or inst not in fwd[ts]:
                continue
            xs[inst] = float(value)
            ys[inst] = fwd[ts][inst]

        common = sorted(xs)
        ic = _pearson(
            [xs[i] for i in common], [ys[i] for i in common]
        )
        rank_ic = _pearson(
            _rank([xs[i] for i in common]),
            _rank([ys[i] for i in common]),
        )
        ic_series.append((ts, ic))
        rank_series.append((ts, rank_ic))
        bucket_rows.append(_bucket_of(xs, quantiles))
        row = _quantile_returns(xs, ys, quantiles)
        quantile_rows.append((ts, row))
        long_short.append(
            (
                ts,
                row[-1] - row[0]
                if row[-1] is not None and row[0] is not None
                else None,
            )
        )

    ic_values = [value for _, value in ic_series if value is not None]
    rank_values = [
        value for _, value in rank_series if value is not None
    ]
    decay = [
        (
            lag,
            _mean(
                [
                    value
                    for _, value in _ic_for_horizon(
                        factor_values, prices, lag
                    )
                    if value is not None
                ]
            ),
        )
        for lag in range(1, decay_lags + 1)
    ]
    return EvaluationResult(
        horizon=horizon,
        ic=ic_series,
        rank_ic=rank_series,
        ic_mean=_mean(ic_values),
        rank_ic_mean=_mean(rank_values),
        icir=_icir(ic_values),
        quantile_returns=quantile_rows,
        long_short=long_short,
        decay=decay,
        turnover=_turnover(bucket_rows),
    )


def correlation_matrix(
    factor_series: Mapping[str, Sequence[Decimal | None]],
    *,
    threshold: float = REDUNDANT_THRESHOLD,
) -> CorrelationResult:
    """多因子相关性矩阵（要求各序列等长、按同一 ts 对齐）。

    `|corr| > threshold` 记入 `redundant`，提示合并（PRD M5-11）。
    """
    names = sorted(factor_series)
    matrix: dict[tuple[str, str], float | None] = {}
    redundant: list[tuple[str, str]] = []
    for i, left in enumerate(names):
        for right in names[i:]:
            pairs = [
                (float(a), float(b))
                for a, b in zip(
                    factor_series[left], factor_series[right], strict=False
                )
                if a is not None and b is not None
            ]
            corr = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
            matrix[(left, right)] = corr
            if left != right and corr is not None and abs(corr) > threshold:
                redundant.append((left, right))
    return CorrelationResult(matrix=matrix, redundant=redundant)


__all__ = [
    "DEFAULT_DECAY_LAGS",
    "DEFAULT_QUANTILES",
    "REDUNDANT_THRESHOLD",
    "CorrelationResult",
    "EvaluationResult",
    "correlation_matrix",
    "evaluate",
    "percentile",
    "zscore",
]
