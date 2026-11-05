/** K 线周期选项与「本地是否有数据」判定（M2-12）。

 * 9 档与后端 `cli/__init__.py::BAR_CHOICES` 保持一致（PRD 4.2 M2-12）。
 * 周期按钮常驻显示，不再因本地缺某个周期的数据而消失。
 */
export const BAR_OPTIONS = [
  "1m",
  "5m",
  "15m",
  "30m",
  "1H",
  "4H",
  "6H",
  "12H",
  "1D",
] as const;

export type BarOption = (typeof BAR_OPTIONS)[number];
export type BarDataState = "ready" | "missing" | "unknown";

/** 少于这个根数时提示补历史：柱宽有上限，但指标仍需要足够样本。 */
export const SPARSE_BARS = 30;

/**
 * `available` 来自 `GET /api/candles` 的 `available_bars`（本地有 parquet 的周期）。
 * 接口还没返回、或读取降级时返回 unknown——不能把全部周期误标成「无数据」。
 */
export function barDataState(
  available: string[] | null | undefined,
  bar: string,
  degraded: string[] | null | undefined = [],
): BarDataState {
  if (available == null || (degraded ?? []).includes("candles")) {
    return "unknown";
  }
  return available.includes(bar) ? "ready" : "missing";
}
