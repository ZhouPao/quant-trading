import { describe, expect, it } from "vitest";
import type { FactorMeta } from "./api";
import {
  axisTime,
  defaultRange,
  hasFactorValues,
  hasIcPoints,
  heatPoints,
  meanQuantileReturns,
  validateRange,
} from "./factors";

function meta(overrides: Partial<FactorMeta> = {}): FactorMeta {
  return {
    insts: ["BTC-USDT", "ETH-USDT"],
    bars: ["1H", "4H"],
    inst: "BTC-USDT",
    bar: "1H",
    first_ts: Date.UTC(2026, 8, 1),
    last_ts: Date.UTC(2026, 8, 30),
    count: 100,
    reason: null,
    ...overrides,
  };
}

describe("defaultRange", () => {
  it("落在本地最新 K 线附近，而不是今天", () => {
    const range = defaultRange(meta(), Date.UTC(2026, 10, 4), 30);

    expect(range.end).toBe("2026-09-30");
    expect(range.start).toBe("2026-08-31");
  });

  it("无数据时退回当前时间", () => {
    const range = defaultRange(
      meta({ last_ts: null }),
      Date.UTC(2026, 10, 4),
      30,
    );

    expect(range.end).toBe("2026-11-04");
    expect(range.start).toBe("2026-10-05");
  });
});

describe("validateRange", () => {
  it("拒绝空值、非法格式、倒序与超长区间", () => {
    expect(validateRange("", "")).toContain("填写");
    expect(validateRange("2026/09/01", "2026-09-02")).toContain("YYYY-MM-DD");
    expect(validateRange("2026-09-10", "2026-09-01")).toContain("不能晚于");
    expect(validateRange("2024-01-01", "2026-01-01")).toContain("366");
  });

  it("接受合法区间", () => {
    expect(validateRange("2026-09-01", "2026-09-30")).toBeNull();
  });
});

describe("meanQuantileReturns", () => {
  it("各档取各期均值，全期无样本的档为 null", () => {
    const result = meanQuantileReturns([
      { ts: 1, values: [0.1, null, 0.3] },
      { ts: 2, values: [0.3, null, 0.5] },
    ]);

    expect(result[0]).toBeCloseTo(0.2);
    expect(result[1]).toBeNull();
    expect(result[2]).toBeCloseTo(0.4);
  });

  it("空输入返回空数组", () => {
    expect(meanQuantileReturns([])).toEqual([]);
  });
});

describe("图表数据规整", () => {
  it("时间轴标签带小时，同一天不同 bar 不重复", () => {
    expect(axisTime(Date.UTC(2026, 8, 1, 8))).toBe("09-01 08:00");
    expect(axisTime(Date.UTC(2026, 8, 1, 9))).toBe("09-01 09:00");
  });

  it("热力点过滤 null 并保留两位小数", () => {
    const points = heatPoints(
      [
        { a: "x", b: "x", corr: 1 },
        { a: "x", b: "y", corr: 0.8234 },
        { a: "y", b: "x", corr: 0.8234 },
        { a: "y", b: "y", corr: null },
      ],
      ["x", "y"],
    );

    expect(points).toEqual([
      [0, 0, 1],
      [0, 1, 0.82],
      [1, 0, 0.82],
    ]);
  });

  it("全为预热 None 时判定为无数据", () => {
    expect(hasFactorValues([{ value: null }])).toBe(false);
    expect(hasFactorValues([{ value: null }, { value: "0.1" }])).toBe(true);
    expect(hasIcPoints([{ value: null }])).toBe(false);
    expect(hasIcPoints([{ value: null }, { value: 0.5 }])).toBe(true);
  });
});
