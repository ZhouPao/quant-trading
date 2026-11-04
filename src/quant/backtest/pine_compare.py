"""Pine 回测结果导入对比（M4-16）。

Pine 是**基线策略副本**，Python 是生产执行实现；两者参数与逻辑不镜像
（基准口径见 `strategies/pine/README.md`）。这里只做「导入 + 逐项算偏离度」，
用于验证 Python 最基础逻辑没有跑偏，不做 Pine 解析：TradingView 没有公开
API，用户手工导出/抄录 `metric,value` 两列 CSV 粘贴进来。

注意：只有「同一策略 + 同一份参数集」的导入结果才会被偏离门禁采纳；
生产参数集若从未做过交叉验证，门禁状态是「未覆盖」而非「通过」。

偏离度定义：`|pine - python| / |python|`。`python == 0` 时比例无意义，退化
为「两者都为 0 算 0，否则算超限且不给数值」——宁可标记为可疑，也不要因为
分母为 0 就判定一致。

指标名要求与内部字段同名（`total_return` / `sharpe` / `max_drawdown` ...），
对不上的名字不报错而是归入 `unmatched`：Pine 里常有我们没实现的统计量，
整份导入因此失败太粗暴；全部对不上才算输入错误。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: 超过该偏离度判定不一致（PRD：> 5% 标红并禁止该参数集上实盘）
DEFAULT_DEVIATION_LIMIT = Decimal("0.05")

#: `backtest_metrics` 里有对应 `pine_<metric>` 列的指标，导入后可持久化
PERSISTED_METRICS = ("total_return", "sharpe", "max_drawdown")

#: 比例类指标：TradingView 默认导出百分数（`8.32` 表示 8.32%），而内部口径是
#: 小数（0.0832）。不带 `%` 且 |值|>1 时几乎必然是百分数，静默接受会让偏离度
#: 放大 100 倍、门禁永久 blocked（P0）。宁可拒绝也不猜。
RATIO_METRICS = frozenset({"total_return", "max_drawdown"})


class PineCompareError(ValueError):
    """CSV 格式非法或无可比对项，消息可直接展示给页面。"""


@dataclass(frozen=True, slots=True)
class PineRow:
    """单指标对比结果。`deviation` 为 None 表示无法用比例表达（分母为 0）。"""

    metric: str
    pine: Decimal
    python: Decimal | None
    deviation: Decimal | None
    over_limit: bool


def parse_pine_csv(text: str) -> dict[str, Decimal]:
    """解析 `metric,value` 两列 CSV。

    - 表头可选（首行第一列不是数值即视为表头，含 `metric`/`指标` 等写法）
    - 空行与 `#` 开头行跳过
    - 同一指标重复出现以最后一次为准

    Raises:
        PineCompareError: 没有有效行 / 列数不对 / 数值非法
    """
    values: dict[str, Decimal] = {}
    first_data_line = True
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [item.strip() for item in line.split(",")]
        if len(parts) != 2:
            raise PineCompareError(
                f"第 {lineno} 行应为 metric,value 两列，实际 {len(parts)} 列：{line}"
            )
        name, raw_value = parts
        if not name:
            raise PineCompareError(f"第 {lineno} 行指标名为空")
        try:
            text_value = raw_value.strip()
            value = Decimal(text_value.rstrip("%"))
            if text_value.endswith("%"):
                # 内部指标一律存小数（0.05 表示 5%），带百分号导入要同口径
                value = value / Decimal("100")
        except InvalidOperation as e:
            # 只放过第一行的表头；后面的非法值是真写错了
            if first_data_line:
                first_data_line = False
                continue
            raise PineCompareError(
                f"第 {lineno} 行数值非法：{raw_value!r}"
            ) from e
        if name in RATIO_METRICS and abs(value) > 1:
            # TradingView 导出 `Max Drawdown,8.32`（百分数）：既不能静默当小数
            # 收下（偏离 100 倍），也不能替用户猜是 8.32% 还是 832%。
            raise PineCompareError(
                f"第 {lineno} 行 {name}={raw_value} 看起来是百分数："
                "请写成 `8.32%` 或小数 `0.0832`（内部统一用小数）"
            )
        first_data_line = False
        values[name] = value
    if not values:
        raise PineCompareError("没有解析到任何 `metric,value` 行")
    return values


def compare(
    pine: dict[str, Decimal],
    python_metrics: dict[str, object],
    limit: Decimal = DEFAULT_DEVIATION_LIMIT,
) -> dict[str, object]:
    """逐项对比，返回页面可直接渲染的结构。

    Args:
        pine: 导入的 Pine 指标
        python_metrics: 同一参数集 Python 回测的指标（值可能是 None/str）
        limit: 判定超限的偏离度阈值

    Raises:
        PineCompareError: 全部指标都无法与 Python 结果对上
    """
    rows: list[PineRow] = []
    unmatched: list[str] = []
    for metric in sorted(pine):
        actual = _as_decimal(python_metrics.get(metric))
        if actual is None:
            unmatched.append(metric)
            continue
        deviation, over = _deviation(pine[metric], actual, limit)
        rows.append(
            PineRow(
                metric=metric,
                pine=pine[metric],
                python=actual,
                deviation=deviation,
                over_limit=over,
            )
        )
    if not rows:
        raise PineCompareError(
            "导入的指标与本次回测没有同名项，无法对比；"
            f"可用指标：{', '.join(sorted(k for k in python_metrics if k))}"
        )

    known = [row.deviation for row in rows if row.deviation is not None]
    return {
        "limit": limit,
        "rows": rows,
        "unmatched": unmatched,
        "max_deviation": max(known) if known else None,
        "over_limit": any(row.over_limit for row in rows),
    }


def _deviation(
    pine: Decimal, python: Decimal, limit: Decimal
) -> tuple[Decimal | None, bool]:
    """返回 (偏离度, 是否超限)；分母为 0 时不产出数值但按内容判是否一致。"""
    if python == 0:
        if pine == 0:
            return Decimal("0"), False
        return None, True
    deviation = abs(pine - python) / abs(python)
    return deviation, deviation > limit


def _as_decimal(value: object) -> Decimal | None:
    """指标值转 Decimal；None、非数值字符串一律视为「无此项」。

    `backtest_metrics` 的自定义指标列是 TEXT，可能读到 `"0.123"` 这种字符串，
    也可能读到 `monthly_returns` 的 JSON 文本——后者解析失败即跳过。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


def to_metric_columns(pine: dict[str, Decimal], result: dict[str, object]) -> dict:
    """把导入结果转成 `backtest_metrics` 的 pine_* 列。

    只写有对应列的三个指标，其余指标不落库（表里没有列，也不为其做
    `ALTER TABLE`——迁移脚本必须可重放）。
    """
    columns: dict[str, str | None] = {
        f"pine_{metric}": str(pine[metric])
        for metric in PERSISTED_METRICS
        if metric in pine
    }
    max_dev = result.get("max_deviation")
    if max_dev is not None or result.get("over_limit"):
        # 超限但算不出比例（分母为 0）时存一个显式的大值，避免「超限却没记录」
        columns["pine_deviation"] = (
            str(max_dev) if max_dev is not None else "1"
        )
    return columns


__all__ = [
    "DEFAULT_DEVIATION_LIMIT",
    "PERSISTED_METRICS",
    "PineCompareError",
    "PineRow",
    "compare",
    "parse_pine_csv",
    "to_metric_columns",
]
