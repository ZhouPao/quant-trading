import { describe, expect, it } from "vitest";
import { atr, boll, ema, kdj, macd, pivotLevels, rma, sma, supertrend } from "./indicators";
import { buildChartOption, defaultZoom, formatTime, toBars } from "./options";
import { mergeSettings, parsePeriods, type Bar } from "./types";
import { DEFAULT_SETTINGS } from "./settings";

function bar(close: number, index: number): Bar {
  return {
    ts: 1_700_000_000_000 + index * 3_600_000,
    open: close,
    high: close + 1,
    low: close - 1,
    close,
    vol: 100 + index,
  };
}

describe("sma / ema / rma", () => {
  it("sma 在窗口填满前返回 null", () => {
    expect(sma([1, 2, 3, 4, 5], 3)).toEqual([null, null, 2, 3, 4]);
  });

  it("ema 用前 period 个值的均值做种子", () => {
    // 种子 2，之后 0.5×4 + 0.5×2 = 3
    expect(ema([1, 2, 3, 4], 3)).toEqual([null, null, 2, 3]);
  });

  it("rma 按 Wilder 系数递推", () => {
    const values = rma([2, 4, 6, 8], 3);
    expect(values[2]).toBe(4);
    expect(values[3]).toBeCloseTo((4 * 2 + 8) / 3, 10);
  });
});

describe("boll", () => {
  it("常数序列的标准差为 0", () => {
    const closes = new Array(20).fill(10);
    const result = boll(closes, 20, 2);
    expect(result.mid[19]).toBe(10);
    expect(result.upper[19]).toBe(10);
    expect(result.lower[19]).toBe(10);
  });
});

describe("macd", () => {
  it("常数序列的 DIF/DEA/柱均为 0", () => {
    const closes = new Array(80).fill(50);
    const result = macd(closes, 12, 26, 9);
    expect(result.dif[79]).toBeCloseTo(0, 10);
    expect(result.dea[79]).toBeCloseTo(0, 10);
    expect(result.hist[79]).toBeCloseTo(0, 10);
  });
});

describe("kdj", () => {
  it("首个有效点出现在第 n-1 根，且 J = 3K - 2D", () => {
    const bars = Array.from({ length: 30 }, (_, i) => bar(100 + i, i));
    const result = kdj(bars, 9, 3, 3);
    expect(result.k[7]).toBeNull();
    expect(result.k[8]).not.toBeNull();
    const k = result.k[29] as number;
    const d = result.d[29] as number;
    const j = result.j[29] as number;
    expect(k).toBeGreaterThan(0);
    expect(k).toBeLessThanOrEqual(100);
    expect(j).toBeCloseTo(3 * k - 2 * d, 10);
  });
});

describe("supertrend", () => {
  it("下行序列最终为空头，且两条线互斥", () => {
    const bars = Array.from({ length: 60 }, (_, i) => bar(200 - i * 2, i));
    const result = supertrend(bars, 10, 3);
    const last = bars.length - 1;
    expect(result.dir[last]).toBe(-1);
    expect(result.up[last]).not.toBeNull();
    expect(result.down[last]).toBeNull();
  });

  it("atr 在数据不足时为 null", () => {
    const bars = Array.from({ length: 5 }, (_, i) => bar(100, i));
    const values = atr(bars, 10);
    expect(values.every((item) => item === null)).toBe(true);
  });
});

describe("pivotLevels", () => {
  it("同一价位的多个摆动点合并为一档并累计触碰次数", () => {
    const path = [100, 105, 110, 105, 100, 105, 110, 105, 100, 105, 95, 90];
    const bars = path.map((close, index) => bar(close, index));
    const levels = pivotLevels(bars, 2, 0.5, 5);

    expect(levels).toHaveLength(2);
    expect(levels[0].price).toBeCloseTo(111, 6);
    expect(levels[0].touches).toBe(2);
    expect(levels[0].kind).toBe("resistance");
    expect(levels[1].price).toBeCloseTo(99, 6);
  });

  it("数据不足时返回空数组", () => {
    expect(pivotLevels([], 3, 0.5, 5)).toEqual([]);
  });
});

describe("设置解析", () => {
  it("parsePeriods 过滤非法值、去重并排序", () => {
    expect(parsePeriods("20,5,10,10,abc,0,600, 20")).toEqual([5, 10, 20]);
  });

  it("mergeSettings 只覆盖传入的字段", () => {
    const merged = mergeSettings(DEFAULT_SETTINGS, { ma: { enabled: false } });
    expect(merged.ma.enabled).toBe(false);
    expect(merged.ma.periods).toEqual(DEFAULT_SETTINGS.ma.periods);
    expect(merged.macd).toEqual(DEFAULT_SETTINGS.macd);
  });

  it("mergeSettings 忽略非对象输入", () => {
    expect(mergeSettings(DEFAULT_SETTINGS, null)).toBe(DEFAULT_SETTINGS);
  });

  it("mergeSettings 坏类型字段回落默认，不让渲染期抛错（P0 防白屏）", () => {
    const merged = mergeSettings(DEFAULT_SETTINGS, {
      ma: { enabled: true, periods: 5 }, // periods 应为数组
      boll: { enabled: "yes", period: null, mult: 2 }, // 类型错的回落、合法的保留
      vol: { maPeriods: null },
      macd: "fast", // 整组非对象 → 整组回落
      kdj: { n: [1, 2] }, // 数字字段给了数组
    });
    expect(merged.ma.periods).toEqual(DEFAULT_SETTINGS.ma.periods);
    expect(merged.boll.enabled).toBe(DEFAULT_SETTINGS.boll.enabled);
    expect(merged.boll.period).toBe(DEFAULT_SETTINGS.boll.period);
    expect(merged.boll.mult).toBe(2);
    expect(merged.vol.maPeriods).toEqual(DEFAULT_SETTINGS.vol.maPeriods);
    expect(merged.macd).toEqual(DEFAULT_SETTINGS.macd);
    expect(merged.kdj.n).toBe(DEFAULT_SETTINGS.kdj.n);

    // 数组内的非法元素被过滤而不是整体放弃
    const partial = mergeSettings(DEFAULT_SETTINGS, {
      ma: { periods: [5, "x", 20, 0, -1] },
    });
    expect(partial.ma.periods).toEqual([5, 20]);
  });
});

describe("图表时间轴与缩放", () => {
  it("formatTime 统一按 UTC+8（与全站 stamp 同口径，P1）", () => {
    // 2026-11-03 16:00:00 UTC == 2026-11-04 00:00 +08
    const ts = Date.UTC(2026, 10, 3, 16, 0, 0);
    expect(formatTime(ts, "1H")).toBe("11-04 00:00");
    expect(formatTime(ts, "1D")).toBe("11-04");
  });

  it("dataZoom 绑定全部网格且带 start/end（P1：原先被 spread 覆盖）", () => {
    const bars: Bar[] = [
      { ts: 0, open: 1, high: 2, low: 0.5, close: 1.5, vol: 10 },
      { ts: 3_600_000, open: 1.5, high: 2.5, low: 1, close: 2, vol: 12 },
    ];
    const option = buildChartOption({
      bars,
      settings: DEFAULT_SETTINGS,
      fills: [],
      position: null,
      bar: "1H",
      zoom: null,
    }) as unknown as {
      dataZoom: { xAxisIndex?: number[]; start?: number; end?: number }[];
    };

    // 默认设置开 VOL+MACD → 3 个网格，缩放必须绑全部
    const zoom = defaultZoom(bars.length);
    expect(option.dataZoom).toHaveLength(2);
    for (const dz of option.dataZoom) {
      expect(dz.xAxisIndex).toEqual([0, 1, 2]);
      expect(dz.start).toBeCloseTo(zoom.start);
      expect(dz.end).toBeCloseTo(zoom.end);
    }
  });

  it("toBars 剔除非有限值的脏 bar，阻断 NaN 污染链（P2）", () => {
    const rows = [
      { ts: 0, open: "1", high: "2", low: "0.5", close: "1.5", vol: "10" },
      { ts: 1, open: "x", high: "2", low: "0.5", close: "1.5", vol: "10" }, // 脏
      { ts: 2, open: "1.5", high: "2.5", low: "1", close: "2", vol: "12" },
    ];
    const bars = toBars(rows);
    expect(bars.map((b) => b.ts)).toEqual([0, 2]);
    expect(bars[1].close).toBe(2);
  });
});
