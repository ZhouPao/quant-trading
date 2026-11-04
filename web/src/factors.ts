/** 因子页可单测的纯函数：区间推导、校验、图表数据规整。 */
import type {
  FactorCorrelationsResponse,
  FactorEvaluationResponse,
  FactorMeta,
} from "./api";

export const DAY_MS = 86_400_000;

/** UTC 毫秒 -> YYYY-MM-DD。 */
export function isoDay(ts: number): string {
  return new Date(ts).toISOString().slice(0, 10);
}

/** 默认区间落在本地最新一根 K 线附近，而不是「今天往前 30 天」。 */
export function defaultRange(
  meta: FactorMeta | null,
  now: number,
  days = 30,
): { start: string; end: string } {
  const endTs = meta?.last_ts ?? now;
  return {
    start: isoDay(endTs - days * DAY_MS),
    end: isoDay(endTs),
  };
}

/** 返回错误文案；null 表示区间合法。 */
export function validateRange(start: string, end: string): string | null {
  if (!start || !end) return "请填写起始与结束日期";
  const pattern = /^\d{4}-\d{2}-\d{2}$/;
  if (!pattern.test(start) || !pattern.test(end)) {
    return "日期格式应为 YYYY-MM-DD";
  }
  const from = Date.parse(`${start}T00:00:00Z`);
  const to = Date.parse(`${end}T00:00:00Z`);
  if (!Number.isFinite(from) || !Number.isFinite(to)) {
    return "日期不合法";
  }
  if (from > to) return "起始日期不能晚于结束日期";
  if ((to - from) / DAY_MS > 366) return "查询区间不能超过 366 天";
  return null;
}

/** 各档未来收益取各期均值；某一档全期无样本则为 null。 */
export function meanQuantileReturns(
  rows: FactorEvaluationResponse["quantile_returns"],
): (number | null)[] {
  if (rows.length === 0) return [];
  const width = rows[0].values.length;
  const result: (number | null)[] = [];
  for (let col = 0; col < width; col += 1) {
    const samples = rows
      .map((row) => row.values[col])
      .filter((value): value is number => value !== null);
    result.push(
      samples.length
        ? samples.reduce((total, value) => total + value, 0) / samples.length
        : null,
    );
  }
  return result;
}

/** 时间轴标签：带小时，避免 1H 数据同一天出现 24 个相同刻度。 */
export function axisTime(ts: number): string {
  return new Date(ts).toISOString().slice(5, 16).replace("T", " ");
}

/** 热力图数据点：过滤 null（不显示 "null" 标签），保留两位小数。 */
export function heatPoints(
  matrix: FactorCorrelationsResponse["matrix"],
  ids: string[],
): [number, number, number][] {
  return matrix
    .filter(
      (cell) =>
        cell.corr !== null &&
        Number.isFinite(cell.corr) &&
        ids.includes(cell.a) &&
        ids.includes(cell.b),
    )
    .map((cell) => [
      ids.indexOf(cell.a),
      ids.indexOf(cell.b),
      Number((cell.corr as number).toFixed(2)),
    ]);
}

/** 因子值是否有可画的点（全为预热 None 也视为无数据）。 */
export function hasFactorValues(
  values: { value: string | null }[],
): boolean {
  return values.some((item) => item.value !== null);
}

/** IC 时序是否有有效样本。 */
export function hasIcPoints(
  points: { value: number | null }[],
): boolean {
  return points.some((item) => item.value !== null);
}
