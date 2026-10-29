/** K 线图使用的行情行，价格已转为数值。 */
export type Bar = {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  vol: number;
};

/** 主图指标设置。 */
export type MainSettings = {
  ma: { enabled: boolean; periods: number[] };
  boll: { enabled: boolean; period: number; mult: number };
  supertrend: { enabled: boolean; period: number; mult: number };
  sr: { enabled: boolean; leftRight: number; tolerancePct: number; maxLevels: number };
};

/** 副图指标设置。 */
export type SubSettings = {
  vol: { enabled: boolean; maPeriods: number[] };
  macd: { enabled: boolean; fast: number; slow: number; signal: number };
  kdj: { enabled: boolean; n: number; k: number; d: number };
};

export type ChartSettings = MainSettings & SubSettings;

/** 与 localStorage 中的旧值合并，缺字段回落到默认值。 */
export function mergeSettings(defaults: ChartSettings, saved: unknown): ChartSettings {
  if (!saved || typeof saved !== "object") return defaults;
  const raw = saved as Record<string, unknown>;
  const pick = <T extends object>(key: keyof ChartSettings, base: T): T => {
    const value = raw[key];
    if (!value || typeof value !== "object") return base;
    return { ...base, ...(value as T) };
  };
  return {
    ma: pick("ma", defaults.ma),
    boll: pick("boll", defaults.boll),
    supertrend: pick("supertrend", defaults.supertrend),
    sr: pick("sr", defaults.sr),
    vol: pick("vol", defaults.vol),
    macd: pick("macd", defaults.macd),
    kdj: pick("kdj", defaults.kdj),
  };
}

/** 解析 "5,10,20" 形式的周期列表，丢弃非法值与重复项。 */
export function parsePeriods(text: string): number[] {
  const seen = new Set<number>();
  return text
    .split(/[,，\s]+/)
    .map((item) => Number(item))
    .filter((item) => Number.isInteger(item) && item > 0 && item <= 500)
    .filter((item) => {
      if (seen.has(item)) return false;
      seen.add(item);
      return true;
    })
    .sort((a, b) => a - b);
}
