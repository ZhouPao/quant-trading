import { describe, expect, it } from "vitest";
import { BAR_OPTIONS, barDataState } from "./bars";

describe("BAR_OPTIONS", () => {
  it("包含 PRD M2-12 要求的 9 档，且含 1m", () => {
    expect([...BAR_OPTIONS]).toEqual([
      "1m",
      "5m",
      "15m",
      "30m",
      "1H",
      "4H",
      "6H",
      "12H",
      "1D",
    ]);
  });
});

describe("barDataState", () => {
  it("后端返回该周期时为 ready", () => {
    expect(barDataState(["1H", "4H"], "4H")).toBe("ready");
  });

  it("后端没返回该周期时为 missing（按钮仍显示，只是标注）", () => {
    expect(barDataState(["1H"], "5m")).toBe("missing");
  });

  it("接口未返回或读取降级时为 unknown，不误标全部周期", () => {
    expect(barDataState(null, "1H")).toBe("unknown");
    expect(barDataState(["1H"], "1H", ["candles"])).toBe("unknown");
  });
});
