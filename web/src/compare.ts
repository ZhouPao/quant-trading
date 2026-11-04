/**
 * M3-18 参数版本对比的纯函数（从 App.tsx 抽出，便于单测）。
 * 只做排序 / 签名 / 最优值计算，不依赖 React 或 DOM。
 */
import type { BacktestRun, MetricMap } from "./api";

/** 对比表排序键：点表头切换，名次与首行高亮随之重排 */
export type CompareSort = "total_return" | "max_drawdown" | "sharpe";

/** 越小越好的指标，其余按越大越好比较 */
export const LOWER_IS_BETTER = new Set(["max_drawdown"]);

/** 交易次数没有优劣之分，不参与高亮 */
export const NO_BEST = new Set(["trade_count"]);

export function bestValue(
  runs: { metrics: MetricMap }[],
  key: string,
): number | null {
  if (NO_BEST.has(key)) return null;
  const values = runs
    .map((run) => run.metrics[key])
    .filter(
      (value): value is number =>
        typeof value === "number" && Number.isFinite(value),
    );
  if (values.length < 2) return null;
  return LOWER_IS_BETTER.has(key) ? Math.min(...values) : Math.max(...values);
}

/**
 * 稳定序列化：对象键排序、数组保序。
 *
 * walk-forward / batch 的 params 含嵌套 `base` / `grid`，直接 `JSON.stringify`
 * 会受键序影响——同一组参数可能生成不同签名，对比表出现重复版本（P2）。
 */
function stableValue(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(stableValue).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(
      ([left], [right]) => left.localeCompare(right),
    );
    return `{${entries
      .map(([key, item]) => `${JSON.stringify(key)}:${stableValue(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

/**
 * 参数签名：递归键排序后拼接。
 *
 * 同一组参数若来自不同合并路径（toml / DB 覆盖 / 请求覆盖），对象键序可能
 * 不同；直接按 Object.entries 顺序拼接会把它们当成两个版本，对比表出现
 * 重复行、名次被稀释（P1）。嵌套对象同样排序，避免 walk-forward 的
 * base/grid 键序差异制造假版本（P2）。
 */
export function formatParams(params: Record<string, unknown>): string {
  const entries = Object.entries(params).sort(([left], [right]) =>
    left.localeCompare(right),
  );
  if (entries.length === 0) return "—";
  return entries
    .map(([key, value]) => {
      if (value !== null && typeof value === "object") {
        return `${key}=${stableValue(value)}`;
      }
      return `${key}=${String(value)}`;
    })
    .join("; ");
}

/** 对比表排序：按当前排序键从优到劣，缺失值排末尾。回撤越小越好，其余越大越好。 */
export function sortCompareVersions(
  versions: { signature: string; run: BacktestRun }[],
  key: CompareSort,
) {
  const higherBetter = !LOWER_IS_BETTER.has(key);
  return [...versions].sort((a, b) => {
    const left = a.run.metrics[key];
    const right = b.run.metrics[key];
    const leftValue =
      typeof left === "number" && Number.isFinite(left) ? left : null;
    const rightValue =
      typeof right === "number" && Number.isFinite(right) ? right : null;
    if (leftValue === rightValue) return 0;
    if (leftValue === null) return 1;
    if (rightValue === null) return -1;
    return higherBetter ? rightValue - leftValue : leftValue - rightValue;
  });
}
