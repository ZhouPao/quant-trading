import { describe, expect, it } from "vitest";
import type { BacktestRun } from "./api";
import { bestValue, formatParams, sortCompareVersions } from "./compare";

const run = (
  id: string,
  metrics: Record<string, number | null>,
): BacktestRun => ({
  run_id: id,
  strategy: "sma_cross",
  inst: "BTC-USDT",
  bar: "1H",
  start: "2026-01-01",
  end: "2026-02-01",
  params: {},
  warmup_bars: 50,
  kind: "backtest",
  status: "done",
  created_at: 1,
  started_at: null,
  finished_at: null,
  error: null,
  metrics,
});

describe("M3-18 参数版本对比纯函数", () => {
  it("formatParams 键序无关：同一组参数不能被当成两个版本（P1）", () => {
    expect(formatParams({ fast_len: 8, slow_len: 30 })).toBe(
      formatParams({ slow_len: 30, fast_len: 8 }),
    );
    expect(formatParams({})).toBe("—");
  });

  it("formatParams 嵌套对象序列化，不显示 [object Object]", () => {
    const text = formatParams({ grid: { a: 1 }, fast_len: 8 });
    expect(text).toContain('grid={"a":1}');
    expect(text).toContain("fast_len=8");
  });

  it("formatParams 嵌套键序也稳定（walk-forward 的 base/grid，P2）", () => {
    expect(formatParams({ grid: { b: 2, a: 1 } })).toBe(
      formatParams({ grid: { a: 1, b: 2 } }),
    );
    expect(formatParams({ grid: [{ b: 2, a: 1 }] })).toBe(
      formatParams({ grid: [{ a: 1, b: 2 }] }),
    );
  });

  it("bestValue 按口径取最优，trade_count 不参与，单值/NaN 不高亮", () => {
    const runs = [
      run("a", { total_return: 0.1, max_drawdown: 0.2 }),
      run("b", { total_return: 0.3, max_drawdown: 0.05 }),
    ];
    expect(bestValue(runs, "total_return")).toBe(0.3);
    expect(bestValue(runs, "max_drawdown")).toBe(0.05);
    expect(bestValue(runs, "trade_count")).toBeNull();
    expect(bestValue([runs[0]], "total_return")).toBeNull();
    expect(
      bestValue([run("c", { total_return: Number.NaN })], "total_return"),
    ).toBeNull();
  });

  it("sortCompareVersions 缺失值排末尾、回撤按小到大、不改原数组", () => {
    const versions = [
      {
        signature: "s1",
        run: run("a", { total_return: 0.1, max_drawdown: 0.3 }),
      },
      {
        signature: "s2",
        run: run("b", { total_return: null, max_drawdown: null }),
      },
      {
        signature: "s3",
        run: run("c", { total_return: 0.4, max_drawdown: 0.1 }),
      },
    ];

    expect(
      sortCompareVersions(versions, "total_return").map((v) => v.signature),
    ).toEqual(["s3", "s1", "s2"]);
    expect(
      sortCompareVersions(versions, "max_drawdown").map((v) => v.signature),
    ).toEqual(["s3", "s1", "s2"]);
    expect(versions[0].signature).toBe("s1");
  });
});
