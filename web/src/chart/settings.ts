import { mergeSettings, type ChartSettings } from "./types";

const STORAGE_KEY = "qt.chart.indicators.v1";

export const DEFAULT_SETTINGS: ChartSettings = {
  ma: { enabled: true, periods: [5, 10, 20, 50, 200] },
  boll: { enabled: true, period: 20, mult: 2 },
  supertrend: { enabled: false, period: 10, mult: 3 },
  sr: { enabled: false, leftRight: 3, tolerancePct: 0.5, maxLevels: 5 },
  vol: { enabled: true, maPeriods: [5, 10] },
  macd: { enabled: true, fast: 12, slow: 26, signal: 9 },
  kdj: { enabled: false, n: 9, k: 3, d: 3 },
};

export function loadSettings(): ChartSettings {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return mergeSettings(DEFAULT_SETTINGS, raw ? JSON.parse(raw) : null);
  } catch {
    return DEFAULT_SETTINGS;
  }
}

export function saveSettings(settings: ChartSettings): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // 隐私模式下 localStorage 可能不可写，忽略即可，不影响图表渲染
  }
}
