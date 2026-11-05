import { describe, expect, it } from "vitest";
import { DEFAULT_OPEN, ENGINE_SECTIONS, type SectionId } from "./Section";

describe("策略研究分节", () => {
  it("6 个分节 id 与标题一一对应", () => {
    expect(Object.keys(ENGINE_SECTIONS)).toEqual([
      "params",
      "history",
      "bindings",
      "evaluations",
      "runs",
      "compare",
    ]);
    expect(new Set(Object.values(ENGINE_SECTIONS)).size).toBe(6);
  });

  it("默认只展开参数与启停，结果类默认收起", () => {
    const open = Object.keys(DEFAULT_OPEN).filter(
      (id) => DEFAULT_OPEN[id as SectionId],
    );
    expect(open).toEqual(["params", "bindings"]);
  });
});
