import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  deleteBinding,
  fetchBacktestRuns,
  fetchBindings,
  fetchParamChanges,
  fetchStrategies,
  fetchStrategyEvaluations,
} from "../api";

// 策略页此前五个接口都裸 getJson，缺字段会让渲染期 undefined.length 崩进
// ErrorBoundary（P1）。这里用 mock fetch 锁住兜底契约。
const okResponse = (data: unknown) =>
  ({
    ok: true,
    status: 200,
    json: async () => data,
  }) as Response;

const failResponse = (detail: unknown, status = 400) =>
  ({
    ok: false,
    status,
    json: async () => ({ detail }),
  }) as Response;

describe("策略页接口空值兜底", () => {
  const signal = new AbortController().signal;

  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("响应缺字段时五个接口都返回空集合（P1 防崩）", async () => {
    vi.mocked(fetch).mockImplementation((async () => okResponse({})) as never);

    expect((await fetchStrategies(signal)).strategies).toEqual([]);
    expect((await fetchParamChanges(signal, "sma_cross")).changes).toEqual([]);
    expect(
      (await fetchStrategyEvaluations(signal, "sma_cross")).items,
    ).toEqual([]);
    expect((await fetchBindings(signal, "sma_cross")).bindings).toEqual([]);
    expect((await fetchBacktestRuns(signal)).runs).toEqual([]);
  });

  it("正常数据原样保留", async () => {
    vi.mocked(fetch).mockImplementation(
      (async () => okResponse({ strategies: [{ id: "sma_cross" }] })) as never,
    );

    const data = await fetchStrategies(signal);
    expect(data.strategies[0].id).toBe("sma_cross");
  });

  it("deleteBinding 以 DELETE 打到解绑端点并带 token", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.mocked(fetch).mockImplementation(
      (async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return okResponse({ ok: true, strategy_id: "sma_cross", inst_id: "BTC-USDT" });
      }) as never,
    );

    const result = await deleteBinding("sma_cross", "BTC-USDT", "tk");

    expect(result.ok).toBe(true);
    expect(calls[0].init.method).toBe("DELETE");
    expect(calls[0].url).toContain("/bindings/BTC-USDT");
    expect((calls[0].init.headers as Record<string, string>)["X-Dashboard-Token"]).toBe("tk");
  });

  it("字符串 detail 原样透出（业务 400 的可读原因）", async () => {
    vi.mocked(fetch).mockImplementation(
      (async () =>
        failResponse("该组合已绑定 1H，请先解绑再改周期")) as never,
    );

    await expect(deleteBinding("sma_cross", "BTC-USDT")).rejects.toThrow(
      /请先解绑/,
    );
  });

  it("Pydantic 422 数组 detail 解析成字段级文案（P1）", async () => {
    vi.mocked(fetch).mockImplementation(
      (async () =>
        failResponse(
          [
            {
              loc: ["body", "reason"],
              msg: "String should have at most 200 characters",
              type: "string_too_long",
            },
          ],
          422,
        )) as never,
    );

    // 原先只认 string detail → 用户只看到「HTTP 422」，不知道哪个字段出错
    await expect(deleteBinding("sma_cross", "BTC-USDT")).rejects.toThrow(
      /reason: String should have at most 200 characters/,
    );
  });
});
