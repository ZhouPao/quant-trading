import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getOptimizeJob, submitOptimize, type OptimizeJobResponse } from "../api";

// 用 mock 模拟后端，校验前端(P4)消费侧契约与服务端(P2/P3)实际返回一致：
// - submitOptimize 走 POST /api/optimize，读取 {job_id,status,total}
// - getOptimizeJob 走 GET /api/optimize/{id}（对 jobId 做 encodeURIComponent），
//   返回体逐字段覆盖 OptimizeJobResponse（这正是 App.tsx 进度条/结果渲染依赖的结构）
const okResponse = (data: unknown) =>
  ({
    ok: true,
    status: 200,
    json: async () => data,
  }) as Response;

describe("optimize API（P2/P3 ↔ P4 契约）", () => {
  const signal = new AbortController().signal;

  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("submitOptimize：POST /api/optimize 并返回 job_id 信封", async () => {
    vi.mocked(fetch).mockResolvedValue(
      okResponse({ job_id: "abc", status: "running", total: 4 }) as never,
    );
    const res = await submitOptimize(signal, {
      strategy: "sma_cross",
      grid: "fast_len=3,5",
      metric: "sharpe",
      top: 2,
      n_jobs: 1,
    } as never);

    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/optimize");
    expect(init.method).toBe("POST");
    expect(res.job_id).toBe("abc");
    expect(res.status).toBe("running");
    expect(res.total).toBe(4);
  });

  it("getOptimizeJob：GET 且对 jobId 做 encode，返回体覆盖 OptimizeJobResponse 全部字段", async () => {
    const job: OptimizeJobResponse = {
      job_id: "abc",
      status: "done",
      total: 4,
      completed: 4,
      metric: "sharpe",
      grid: { fast_len: [3, 5], slow_len: [20, 30] },
      n_jobs: 1,
      created_at: 1,
      finished_at: 2,
      result: {
        kind: "optimize",
        strategy: "sma_cross",
        inst: "TEST",
        bar: "1H",
        start: "2026-09-05",
        end: "2026-09-10",
        grid: { fast_len: [3, 5], slow_len: [20, 30] },
        metric: "sharpe",
        bar_count: 10,
        warmup_count: 5,
        combo_count: 4,
        skipped_count: 0,
        results: [],
        elapsed_ms: 1,
      },
      error: null,
    };
    vi.mocked(fetch).mockResolvedValue(okResponse(job) as never);

    const res = await getOptimizeJob(signal, "abc def"); // 含空格，验证 encodeURIComponent
    const [url] = vi.mocked(fetch).mock.calls[0] as [string];
    expect(url).toBe("/api/optimize/abc%20def");
    expect(res).toEqual(job);
  });

  it("getOptimizeJob：终端 failed 时携带 error、result 为 null", async () => {
    const job: OptimizeJobResponse = {
      job_id: "xyz",
      status: "failed",
      total: 4,
      completed: 2,
      metric: "sharpe",
      grid: { fast_len: [3, 5], slow_len: [20, 30] },
      n_jobs: 1,
      created_at: 1,
      finished_at: 2,
      result: null,
      error: "boom",
    };
    vi.mocked(fetch).mockResolvedValue(okResponse(job) as never);
    const res = await getOptimizeJob(signal, "xyz");
    expect(res.status).toBe("failed");
    expect(res.error).toBe("boom");
    expect(res.result).toBeNull();
  });
});
