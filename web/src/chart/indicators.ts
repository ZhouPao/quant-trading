import type { Bar } from "./types";

/** 指标序列：与 K 线等长，未形成周期处为 null。 */
export type Series = (number | null)[];

/** 简单移动平均。窗口不足时返回 null。 */
export function sma(values: number[], period: number): Series {
  const out: Series = new Array(values.length).fill(null);
  if (period <= 0) return out;
  let sum = 0;
  for (let i = 0; i < values.length; i += 1) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

/** 指数移动平均，用前 period 个值的均值作为种子。 */
export function ema(values: Series, period: number): Series {
  return smooth(values, period, 2 / (period + 1));
}

/** Wilder 平滑（RMA），用前 period 个值的均值作为种子。 */
export function rma(values: Series, period: number): Series {
  return smooth(values, period, 1 / period);
}

function smooth(values: Series, period: number, alpha: number): Series {
  const out: Series = new Array(values.length).fill(null);
  if (period <= 0) return out;
  let sum = 0;
  let count = 0;
  let prev: number | null = null;
  for (let i = 0; i < values.length; i += 1) {
    const value = values[i];
    if (value === null) continue;
    if (prev === null) {
      sum += value;
      count += 1;
      if (count === period) {
        prev = sum / period;
        out[i] = prev;
      }
      continue;
    }
    prev = alpha * value + (1 - alpha) * prev;
    out[i] = prev;
  }
  return out;
}

export type Boll = { upper: Series; mid: Series; lower: Series };

/** 布林带：中轨为 SMA，上下轨为中轨 ± mult 倍总体标准差。 */
export function boll(closes: number[], period: number, mult: number): Boll {
  const mid = sma(closes, period);
  const upper: Series = new Array(closes.length).fill(null);
  const lower: Series = new Array(closes.length).fill(null);
  for (let i = 0; i < closes.length; i += 1) {
    const mean = mid[i];
    if (mean === null) continue;
    let variance = 0;
    for (let j = i - period + 1; j <= i; j += 1) {
      variance += (closes[j] - mean) ** 2;
    }
    const sd = Math.sqrt(variance / period);
    upper[i] = mean + mult * sd;
    lower[i] = mean - mult * sd;
  }
  return { upper, mid, lower };
}

export type Macd = { dif: Series; dea: Series; hist: Series };

/** MACD：柱值取 2×(DIF−DEA)，与常见行情软件一致。 */
export function macd(closes: number[], fast: number, slow: number, signal: number): Macd {
  const fastEma = ema(closes, fast);
  const slowEma = ema(closes, slow);
  const dif: Series = closes.map((_, i) => {
    const a = fastEma[i];
    const b = slowEma[i];
    return a === null || b === null ? null : a - b;
  });
  const dea = ema(dif, signal);
  const hist: Series = dif.map((value, i) => {
    const d = dea[i];
    return value === null || d === null ? null : 2 * (value - d);
  });
  return { dif, dea, hist };
}

export type Kdj = { k: Series; d: Series; j: Series };

/** KDJ：RSV 的 n 日高低点口径，K/D 按平滑参数递推，初值取 50。 */
export function kdj(bars: Bar[], n: number, kSmooth: number, dSmooth: number): Kdj {
  const len = bars.length;
  const k: Series = new Array(len).fill(null);
  const d: Series = new Array(len).fill(null);
  const j: Series = new Array(len).fill(null);
  let prevK = 50;
  let prevD = 50;
  for (let i = 0; i < len; i += 1) {
    if (i < n - 1) continue;
    let high = -Infinity;
    let low = Infinity;
    for (let x = i - n + 1; x <= i; x += 1) {
      high = Math.max(high, bars[x].high);
      low = Math.min(low, bars[x].low);
    }
    const rsv = high === low ? 50 : ((bars[i].close - low) / (high - low)) * 100;
    const curK = ((kSmooth - 1) * prevK + rsv) / kSmooth;
    const curD = ((dSmooth - 1) * prevD + curK) / dSmooth;
    k[i] = curK;
    d[i] = curD;
    j[i] = 3 * curK - 2 * curD;
    prevK = curK;
    prevD = curD;
  }
  return { k, d, j };
}

/** 真实波幅的平均值（Wilder 平滑）。 */
export function atr(bars: Bar[], period: number): Series {
  const tr: Series = bars.map((bar, i) => {
    if (i === 0) return bar.high - bar.low;
    const prevClose = bars[i - 1].close;
    return Math.max(
      bar.high - bar.low,
      Math.abs(bar.high - prevClose),
      Math.abs(bar.low - prevClose),
    );
  });
  return rma(tr, period);
}

export type SuperTrend = { up: Series; down: Series; dir: (1 | -1 | null)[] };

/**
 * 超级趋势：ATR 带宽通道，趋势翻转时切换支撑/压力线。
 * 返回两条序列，同一时间点只有一条有值，便于分别着色。
 */
export function supertrend(bars: Bar[], period: number, mult: number): SuperTrend {
  const len = bars.length;
  const atrValues = atr(bars, period);
  const up: Series = new Array(len).fill(null);
  const down: Series = new Array(len).fill(null);
  const dir: (1 | -1 | null)[] = new Array(len).fill(null);

  let finalUpper: number | null = null;
  let finalLower: number | null = null;
  let trend: 1 | -1 = 1;

  for (let i = 0; i < len; i += 1) {
    const band = atrValues[i];
    if (band === null) continue;
    const mid = (bars[i].high + bars[i].low) / 2;
    const basicUpper = mid + mult * band;
    const basicLower = mid - mult * band;

    if (finalUpper === null || finalLower === null) {
      finalUpper = basicUpper;
      finalLower = basicLower;
    } else {
      const prevClose = bars[i - 1].close;
      finalUpper = basicUpper < finalUpper || prevClose > finalUpper ? basicUpper : finalUpper;
      finalLower = basicLower > finalLower || prevClose < finalLower ? basicLower : finalLower;
      if (bars[i].close > finalUpper) trend = 1;
      else if (bars[i].close < finalLower) trend = -1;
    }

    dir[i] = trend;
    if (trend === 1) down[i] = finalLower;
    else up[i] = finalUpper;
  }

  return { up, down, dir };
}

export type Level = {
  price: number;
  touches: number;
  kind: "support" | "resistance";
};

/**
 * 撑压线：先取左右各 leftRight 根确认的摆动高低点，
 * 再按价格相近程度聚类，保留触碰次数最多的若干条。
 */
export function pivotLevels(
  bars: Bar[],
  leftRight: number,
  tolerancePct: number,
  maxLevels: number,
  lastClose?: number,
): Level[] {
  if (bars.length === 0 || leftRight <= 0) return [];
  const prices: number[] = [];

  for (let i = leftRight; i < bars.length - leftRight; i += 1) {
    let isHigh = true;
    let isLow = true;
    for (let x = i - leftRight; x <= i + leftRight; x += 1) {
      if (x === i) continue;
      if (bars[x].high >= bars[i].high) isHigh = false;
      if (bars[x].low <= bars[i].low) isLow = false;
      if (!isHigh && !isLow) break;
    }
    if (isHigh) prices.push(bars[i].high);
    if (isLow) prices.push(bars[i].low);
  }

  if (prices.length === 0) return [];
  prices.sort((a, b) => a - b);

  const clusters: { sum: number; count: number; avg: number }[] = [];
  const tolerance = Math.max(tolerancePct, 0) / 100;
  for (const price of prices) {
    const last = clusters[clusters.length - 1];
    if (last && Math.abs(price - last.avg) / last.avg <= tolerance) {
      last.sum += price;
      last.count += 1;
      last.avg = last.sum / last.count;
    } else {
      clusters.push({ sum: price, count: 1, avg: price });
    }
  }

  const close = lastClose ?? bars[bars.length - 1].close;
  const picked = clusters
    .sort((a, b) => b.count - a.count)
    .slice(0, Math.max(maxLevels, 0))
    .map((cluster) => ({
      price: cluster.avg,
      touches: cluster.count,
      kind: cluster.avg >= close ? ("resistance" as const) : ("support" as const),
    }));

  return picked.sort((a, b) => b.price - a.price);
}
