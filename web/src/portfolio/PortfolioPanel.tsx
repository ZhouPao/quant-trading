import { useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";
import {
  fetchPortfolio,
  type PortfolioResponse,
} from "../api";
import { EChart } from "../chart/EChart";

const POLL_MS = 30_000;

function pct(value: string): number {
  const n = Number(value);
  return Number.isFinite(n) ? n * 100 : 0;
}

function amount(value: string): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function stamp(ts: number): string {
  return new Date(ts).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 无数据时不画空图，给明确的补数据命令（AGENTS #7：不静默渲染 0）。 */
function ChartBlock({
  title,
  note,
  hasData,
  hint,
  option,
  height = 220,
}: {
  title: string;
  note?: string;
  hasData: boolean;
  hint: string;
  option: EChartsOption;
  height?: number;
}) {
  return (
    <section className="card pf-card">
      <div className="card-head">
        <h3>{title}</h3>
        {note && <span className="pf-note">{note}</span>}
      </div>
      {hasData ? (
        <EChart option={option} height={height} className="pf-chart" />
      ) : (
        <p className="pf-empty">{hint}</p>
      )}
    </section>
  );
}

export function PortfolioPanel() {
  const [data, setData] = useState<PortfolioResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    const pull = async () => {
      try {
        const res = await fetchPortfolio(controller.signal);
        if (alive) {
          setData(res);
          setError(null);
        }
      } catch (err) {
        if (alive && (err as Error).name !== "AbortError") {
          setError((err as Error).message);
        }
      }
    };
    void pull();
    const timer = window.setInterval(() => void pull(), POLL_MS);
    return () => {
      alive = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  const weightOption = useMemo<EChartsOption>(() => {
    const rows = (data?.units ?? [])
      .filter((u) => u.enabled && pct(u.weight) > 0)
      .map((u) => ({ name: u.unit_key, value: pct(u.weight) }));
    return {
      tooltip: { trigger: "item", formatter: "{b}: {c}%" },
      legend: { bottom: 0, textStyle: { color: "#8ba39a", fontSize: 10 } },
      series: [
        {
          type: "pie" as const,
          radius: ["42%", "68%"],
          center: ["50%", "45%"],
          label: { formatter: "{b}\n{c}%", fontSize: 10 },
          data: rows,
        },
      ],
    };
  }, [data]);

  const equityOption = useMemo<EChartsOption>(() => {
    const snaps = data?.snapshots ?? [];
    return {
      tooltip: { trigger: "axis" },
      legend: {
        bottom: 0,
        textStyle: { color: "#8ba39a", fontSize: 10 },
        data: ["组合权益", "回撤"],
      },
      grid: { left: 68, right: 52, top: 18, bottom: 40 },
      xAxis: {
        type: "category" as const,
        data: snaps.map((s) => stamp(s.ts)),
        axisLabel: { fontSize: 10, hideOverlap: true },
      },
      yAxis: [
        { type: "value" as const, scale: true, name: "权益" },
        { type: "value" as const, min: 0, name: "回撤%" },
      ],
      series: [
        {
          name: "组合权益",
          type: "line" as const,
          showSymbol: false,
          data: snaps.map((s) => amount(s.equity)),
        },
        {
          name: "回撤",
          type: "line" as const,
          yAxisIndex: 1,
          showSymbol: false,
          areaStyle: { opacity: 0.15 },
          data: snaps.map((s) => pct(s.drawdown)),
        },
      ],
    };
  }, [data]);

  /** 贡献瀑布用堆叠柱近似：权重配置贡献 + 单元自身收益贡献，总贡献单独标点。 */
  const attributionOption = useMemo<EChartsOption>(() => {
    const rows = data?.attribution ?? [];
    return {
      tooltip: { trigger: "axis" },
      legend: {
        bottom: 0,
        textStyle: { color: "#8ba39a", fontSize: 10 },
        data: ["权重配置", "单元自身", "合计"],
      },
      grid: { left: 130, right: 24, top: 18, bottom: 40 },
      xAxis: { type: "value" as const, name: "%" },
      yAxis: {
        type: "category" as const,
        data: rows.map((r) => r.unit_key),
        axisLabel: { fontSize: 10 },
      },
      series: [
        {
          name: "权重配置",
          type: "bar" as const,
          stack: "contrib",
          data: rows.map((r) => pct(r.allocation)),
        },
        {
          name: "单元自身",
          type: "bar" as const,
          stack: "contrib",
          data: rows.map((r) => pct(r.selection)),
        },
        {
          name: "合计",
          type: "scatter" as const,
          data: rows.map((r, i) => [pct(r.total), i]),
          symbolSize: 9,
        },
      ],
    };
  }, [data]);

  const units = data?.units ?? [];
  const snaps = data?.snapshots ?? [];
  const attempts = data?.rebalance_attempts ?? [];
  const attribution = data?.attribution ?? [];

  return (
    <div className="pf-page">
      <section className="card pf-card pf-head">
        <div className="card-head">
          <h2>组合管理</h2>
          <span className="pf-note">
            {data
              ? `${data.portfolio_id ?? "—"} · ${data.allocator ?? "—"} 分配 · ${units.length} 个单元`
              : "加载中…"}
          </span>
        </div>
        {error && <p className="pf-alert">接口不可用：{error}</p>}
        {data?.degraded && data.degraded.length > 0 && (
          <p className="pf-alert">降级来源：{data.degraded.join("、")}（部分数据不可用，未按 0 展示）</p>
        )}
        {data?.reason && <p className="pf-reason">{data.reason}</p>}
        <div className="pf-kv">
          <span>组合开关</span>
          <strong className={data?.enabled ? "pf-ok" : "pf-off"}>
            {data?.enabled ? "已启用" : "未启用"}
          </strong>
          <span>再平衡</span>
          <strong>{data?.rebalance?.mode ?? "—"}</strong>
          <span>快照</span>
          <strong>{snaps.length} 条</strong>
          <span>组合回撤上限</span>
          <strong>{data?.risk ? `${pct(data.risk.max_drawdown_pct).toFixed(1)}%` : "—"}</strong>
        </div>
      </section>

      <div className="pf-grid">
        <ChartBlock
          title="目标权重"
          note="按配置分配法计算"
          hasData={units.some((u) => u.enabled && pct(u.weight) > 0)}
          hint="暂无启用单元，检查 config/portfolios/<id>.toml"
          option={weightOption}
        />
        <section className="card pf-card">
          <div className="card-head">
            <h3>单元清单</h3>
            <span className="pf-note">停用单元保留持仓，仅停止新信号</span>
          </div>
          {units.length === 0 ? (
            <p className="pf-empty">未配置组合单元</p>
          ) : (
            <table className="pf-table">
              <thead>
                <tr><th>单元</th><th>策略</th><th>品种</th><th>目标权重</th><th>状态</th></tr>
              </thead>
              <tbody>
                {units.map((u) => (
                  <tr key={u.unit_key}>
                    <td className="mono">{u.unit_key}</td>
                    <td>{u.strategy_id}</td>
                    <td>{u.inst_id}</td>
                    <td>{pct(u.weight).toFixed(2)}%</td>
                    <td className={u.enabled ? "pf-ok" : "pf-off"}>
                      {u.enabled ? "启用" : "停用"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <ChartBlock
          title="组合权益与回撤"
          note="来自 portfolio_snapshots"
          hasData={snaps.length >= 2}
          hint="暂无快照：运行 quant portfolio-tick --equity 10000 写第一条（需至少 2 条才能画曲线）"
          option={equityOption}
          height={240}
        />
        <ChartBlock
          title="各单元贡献"
          note="权重配置 vs 单元自身收益"
          hasData={attribution.length > 0}
          hint="快照不足或归因不可用：至少 2 条组合快照后才会计算"
          option={attributionOption}
          height={240}
        />

        <section className="card pf-card pf-wide">
          <div className="card-head">
            <h3>再平衡记录</h3>
            <span className="pf-note">调仓单只经 M8 队列，失败不追单</span>
          </div>
          {attempts.length === 0 ? (
            <p className="pf-empty">暂无调仓记录：运行 quant portfolio-tick --execute 才会提交调仓单</p>
          ) : (
            <table className="pf-table">
              <thead>
                <tr><th>时间</th><th>计划</th><th>单元</th><th>状态</th><th>说明</th></tr>
              </thead>
              <tbody>
                {attempts.map((row) => (
                  <tr key={`${row.plan_ts}-${row.unit_key}`}>
                    <td className="mono">{stamp(row.created_ts)}</td>
                    <td className="mono">{row.plan_ts}</td>
                    <td className="mono">{row.unit_key}</td>
                    <td className={row.status === "failed" ? "pf-off" : "pf-ok"}>
                      {row.status}
                    </td>
                    <td>{row.reason ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  );
}
