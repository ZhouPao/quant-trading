import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchAll } from "../api";

// 后端契约用 mock 兜住前端消费侧：本文件只测「失败降级」这类纯前端可验证的行为，
// 不依赖真实服务（真实链路覆盖见 tests/test_optimize_integration.py）。
const okResponse = (data: unknown) =>
  ({
    ok: true,
    status: 200,
    json: async () => data,
  }) as Response;

const failResponse = () =>
  ({ ok: false, status: 500, json: async () => ({}) }) as Response;

describe("fetchAll 失败降级", () => {
  const signal = new AbortController().signal;

  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("执行页专用接口失败时只降级该块，不把整页打成错误态", async () => {
    vi.mocked(fetch).mockImplementation((async (url: string) => {
      const path = String(url);
      if (
        path.startsWith("/api/orders") ||
        path.startsWith("/api/fills") ||
        path.startsWith("/api/pnl")
      ) {
        return failResponse();
      }
      return okResponse({});
    }) as never);

    const bundle = await fetchAll(signal);

    // 失败的三块降级为空数据并标注来源，而不是让 Promise.all 整体 reject
    expect(bundle.orders.orders).toEqual([]);
    expect(bundle.orders.degraded).toEqual(["orders"]);
    expect(bundle.fills.fills).toEqual([]);
    expect(bundle.fills.degraded).toEqual(["fills"]);
    expect(bundle.pnl.degraded).toEqual(["pnl"]);
    // 核心数据仍在
    expect(bundle.overview).toBeDefined();
    expect(bundle.positions).toBeDefined();
    expect(bundle.alerts).toBeDefined();
    expect(bundle.engine).toBeDefined();
  });

  it("核心接口失败时仍然整体 reject（不该被静默吞掉）", async () => {
    vi.mocked(fetch).mockImplementation((async (url: string) => {
      if (String(url).startsWith("/api/overview")) return failResponse();
      return okResponse({});
    }) as never);

    await expect(fetchAll(signal)).rejects.toThrow(/HTTP 500/);
  });
});
