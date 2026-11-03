import { describe, expect, it } from "vitest";
import { num, signed } from "../api";

describe("金额格式化", () => {
  it("num 保留千分位与固定小数位", () => {
    expect(num("1234.5")).toBe("1,234.50");
    expect(num("-12")).toBe("-12.00");
    expect(num(null)).toBe("—");
  });

  it("signed 给正数补 +，负数/零/空值保持原样", () => {
    expect(signed("1234.5")).toBe("+1,234.50");
    expect(signed("-12")).toBe("-12.00");
    expect(signed("0")).toBe("0.00");
    expect(signed(null)).toBe("—");
    expect(signed(undefined)).toBe("—");
  });
});
