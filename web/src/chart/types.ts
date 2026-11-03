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

/** 与 localStorage 中的旧值合并：逐字段按默认值类型校验，坏字段回落默认。 */
export function mergeSettings(defaults: ChartSettings, saved: unknown): ChartSettings {
  if (!saved || typeof saved !== "object") return defaults;
  const raw = saved as Record<string, unknown>;
  // localStorage 里可能是旧版本结构或被手改坏（如 periods 从数组变成数字）。
  // 只补「缺失字段」不够——类型错的字段照样会让渲染期 .map 抛 TypeError 整页
  // 白屏（无 ErrorBoundary 时代），所以逐字段按默认值类型校验（P0 防白屏）。
  const pick = <S extends object>(key: keyof ChartSettings, base: S): S => {
    const value = raw[key];
    if (!value || typeof value !== "object" || Array.isArray(value)) return base;
    const incoming = value as Partial<Record<keyof S, unknown>>;
    const out = { ...base };
    for (const field of Object.keys(base) as (keyof S & string)[]) {
      (out as Record<string, unknown>)[field] = pickField(
        base[field],
        incoming[field],
      );
    }
    return out;
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

/** 按默认值的类型校验单个字段：数组过滤非法元素，标量类型不符回落默认。 */
function pickField(base: unknown, value: unknown): unknown {
  if (Array.isArray(base)) {
    if (!Array.isArray(value)) return base;
    return value.filter(
      (item) => typeof item === "number" && Number.isFinite(item) && item > 0,
    );
  }
  if (typeof base === "boolean") {
    return typeof value === "boolean" ? value : base;
  }
  if (typeof base === "number") {
    return typeof value === "number" && Number.isFinite(value) ? value : base;
  }
  return base;
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
