import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  ackAlert,
  age,
  clock,
  compactQty,
  fetchAll,
  fetchCandles,
  fetchDeadLetters,
  fetchEquity,
  fetchBindings,
  fetchParamChanges,
  fetchQuality,
  fetchQualityReport,
  fetchProcesses,
  fetchFreshness,
  fetchReports,
  fetchRiskLimits,
  fetchRiskOverrides,
  clearRiskOverride,
  setRiskOverride,
  resumeCircuit,
  revokeSilence,
  silenceAlerts,
  fetchStrategies,
  discardDeadLetter,
  flattenPositions,
  METRIC_LABELS,
  metricText,
  num,
  pct,
  reducePosition,
  replayDeadLetter,
  replayDeadLetters,
  runBacktest,
  runOptimize,
  setKillSwitch,
  stamp,
  updateBinding,
  updateStrategyParams,
  type AlertRow,
  type AlertSilence,
  type BacktestResponse,
  type CandlesResponse,
  type DeadLetterItem,
  type DeadLettersResponse,
  type EngineResponse,
  type EquityPoint,
  type EquityResponse,
  type FillRow,
  type FreshnessResponse,
  type KillSwitchState,
  type MetricMap,
  type OptimizeResponse,
  type OrderRow,
  type Overview,
  type ParamChange,
  type ParamSpec,
  type PnlResponse,
  type QualityItem,
  type QualityListResponse,
  type QualityReport,
  type ReportItem,
  type ReportsResponse,
  type PositionRow,
  type ProcessesResponse,
  type RiskLimitRule,
  type RiskLimitsResponse,
  type RiskOverridesResponse,
  type StrategyBinding,
  type StrategyInfo,
} from "./api";
import { CandleChart } from "./chart/CandleChart";
import { IndicatorBar } from "./chart/IndicatorBar";
import { loadSettings, saveSettings } from "./chart/settings";
import type { ChartSettings } from "./chart/types";
import "./styles.css";

const POLL_MS = 5000;
const BAR_OPTIONS = ["5m", "15m", "30m", "1H", "4H", "6H", "12H", "1D"];
// 与后端 /api/equity 的 hours 上限（720）保持一致
const EQUITY_RANGES = [
  { hours: 24, label: "24h" },
  { hours: 168, label: "7d" },
  { hours: 720, label: "30d" },
];
const METRIC_ORDER = [
  "total_return",
  "max_drawdown",
  "sharpe",
  "calmar",
  "sortino",
  "win_rate",
  "profit_factor",
  "trade_count",
  "exposure_ratio",
  "turnover",
  "max_consecutive_losses",
  "max_consecutive_loss_amount",
];
const OPTIMIZE_METRICS = ["total_return", "sharpe", "max_drawdown", "trade_count"];
const METRIC_SELECT = [
  "sharpe",
  "total_return",
  "max_drawdown",
  "calmar",
  "sortino",
  "win_rate",
  "profit_factor",
  "trade_count",
  "avg_holding_bars",
];
const METRIC_HINTS: Record<string, string> = {
  total_return: "区间累计收益",
  max_drawdown: "相对权益峰值",
  sharpe: "按日收益年化",
  calmar: "年化收益 / 最大回撤",
  sortino: "只惩罚下行波动",
  win_rate: "盈利平仓占比",
  profit_factor: "总盈利 / 总亏损",
  trade_count: "买卖合计笔数",
  exposure_ratio: "有持仓的 K 线占比",
  turnover: "成交名义 / 平均权益",
  max_consecutive_losses: "连续亏损最多几笔",
  max_consecutive_loss_amount: "单段连亏最大金额",
};
const ENGINE_STATUS: Record<string, string> = {
  signal: "已出信号",
  no_signal: "无信号",
  duplicate: "已处理",
  waiting_data: "等待行情",
  skipped: "已跳过",
  error: "出错",
};
const ALERT_LEVELS = [
  { key: "ALL", label: "全部" },
  { key: "ERROR", label: "ERROR" },
  { key: "WARNING", label: "WARN" },
  { key: "INFO", label: "INFO" },
];
const ALERT_LEVEL_RANK: Record<string, number> = {
  ERROR: 3,
  WARNING: 2,
  INFO: 1,
};

// 左侧导航即 PRD §2.2 的 10 个模块，顺序与第 4 章需求分节一一对应
const TABS = [
  { key: "overview", label: "首页总览", status: "live" },
  { key: "market", label: "市场监控", status: "live" },
  { key: "strategy", label: "策略研究", status: "live" },
  { key: "backtest", label: "回测分析", status: "live" },
  { key: "factors", label: "因子库", status: "todo" },
  { key: "portfolio", label: "组合管理", status: "todo" },
  { key: "risk", label: "风险控制", status: "live" },
  { key: "execution", label: "交易执行", status: "live" },
  { key: "logs", label: "日志告警", status: "live" },
  { key: "settings", label: "系统设置", status: "todo" },
] as const;

type TabKey = (typeof TABS)[number]["key"];
type PendingTab = "factors" | "portfolio" | "settings";

const TAB_KEY = "quant.dashboard.tab";

function loadTab(): TabKey {
  const raw = window.localStorage.getItem(TAB_KEY);
  return TABS.some((item) => item.key === raw) ? (raw as TabKey) : "overview";
}

// 尚未实现的模块只展示范围说明，避免空白页让人误以为功能已就绪
const PENDING_MODULES: Record<
  PendingTab,
  { title: string; code: string; detail: string; items: string[] }
> = {
  factors: {
    title: "因子库",
    code: "M5",
    detail: "因子注册、时序/横截面计算与缓存、有效性检验。",
    items: ["因子注册表与元数据", "时序 / 横截面双范围计算", "IC / 分层回测等有效性检验"],
  },
  portfolio: {
    title: "组合管理",
    code: "M6",
    detail: "多策略 × 多品种的权重、资金分配、再平衡与归因。",
    items: ["equal / fixed / inverse_vol / vol_target 四种分配法", "再平衡冷却期", "收益归因"],
  },
  settings: {
    title: "系统设置",
    code: "M10",
    detail: "配置只读与白名单热改、凭证健康、保留分档、进程控制、备份自检。",
    items: ["配置视图与白名单热改", "凭证健康检查", "数据保留与备份自检"],
  },
};

// 回测历史只存指标摘要（不存权益曲线与成交），避免撑爆 localStorage
const HISTORY_KEY = "quant.backtest.history.v1";
const HISTORY_LIMIT = 20;
const COMPARE_LIMIT = 4;
// 越小越好的指标，其余按越大越好比较
const LOWER_IS_BETTER = new Set(["max_drawdown"]);
// 交易次数没有优劣之分，不参与高亮
const NO_BEST = new Set(["trade_count"]);

type HistoryEntry = {
  id: string;
  saved_at: number;
  strategy: string;
  inst: string;
  bar: string;
  start: string;
  end: string;
  params: string;
  bar_count: number;
  trade_count: number;
  final_equity: string;
  metrics: MetricMap;
};

function historySignature(entry: HistoryEntry): string {
  return [entry.strategy, entry.inst, entry.bar, entry.start, entry.end, entry.params].join("|");
}

function loadHistory(): HistoryEntry[] {
  try {
    const raw = window.localStorage.getItem(HISTORY_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (item): item is HistoryEntry =>
          typeof item === "object" &&
          item !== null &&
          typeof (item as HistoryEntry).id === "string" &&
          typeof (item as HistoryEntry).metrics === "object",
      )
      .slice(0, HISTORY_LIMIT);
  } catch {
    return [];
  }
}

function persistHistory(entries: HistoryEntry[]): void {
  try {
    window.localStorage.setItem(HISTORY_KEY, JSON.stringify(entries));
  } catch {
    // 隐私模式或配额不足时静默降级：本次仍然可用，只是不持久化
  }
}

function bestValue(entries: HistoryEntry[], key: string): number | null {
  if (NO_BEST.has(key)) return null;
  const values = entries
    .map((entry) => entry.metrics[key])
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  if (values.length < 2) return null;
  return LOWER_IS_BETTER.has(key) ? Math.min(...values) : Math.max(...values);
}

type Bundle = Awaited<ReturnType<typeof fetchAll>>;

type Tone = "ok" | "warn" | "bad" | "idle";

export default function App() {
  const [data, setData] = useState<Bundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [pulse, setPulse] = useState(0);
  const [equityHours, setEquityHours] = useState(24);
  const [equity, setEquity] = useState<EquityResponse | null>(null);
  const [reports, setReports] = useState<ReportsResponse | null>(null);
  const [quality, setQuality] = useState<QualityListResponse | null>(null);
  const [risk, setRisk] = useState<RiskLimitsResponse | null>(null);
  const [riskError, setRiskError] = useState<string | null>(null);
  // 人工恢复熔断后立即重取，不必等下一次轮询
  const [riskRefresh, setRiskRefresh] = useState(0);
  const [tab, setTab] = useState<TabKey>(loadTab);
  // M1-5 进程健康：进程心跳变化比行情慢，15s 一拉即可
  const [processes, setProcesses] = useState<ProcessesResponse | null>(null);
  // M1-7 数据新鲜度与进程健康同频，都是分钟级信号
  const [freshness, setFreshness] = useState<FreshnessResponse | null>(null);
  const timer = useRef<number | null>(null);

  const selectTab = useCallback((next: TabKey) => {
    setTab(next);
    try {
      window.localStorage.setItem(TAB_KEY, next);
    } catch {
      // 隐私模式下不持久化，切换仍然生效
    }
  }, []);

  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const bundle = await fetchAll(signal);
      setData(bundle);
      setError(null);
      setUpdatedAt(Date.now());
      setPulse((n) => n + 1);
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError((err as Error).message);
    }
  }, []);

  // 告警已读/静音是写操作，成功后立刻重取，不等下一轮轮询
  const refresh = useCallback(async () => {
    await load(new AbortController().signal);
  }, [load]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    timer.current = window.setInterval(() => {
      void load(controller.signal);
    }, POLL_MS);
    return () => {
      controller.abort();
      if (timer.current !== null) window.clearInterval(timer.current);
    };
  }, [load]);

  // 权益曲线按所选区间单独轮询，切换区间立即重拉
  useEffect(() => {
    const controller = new AbortController();
    const pull = () => {
      fetchEquity(controller.signal, equityHours)
        .then(setEquity)
        .catch((err) => {
          if ((err as Error).name === "AbortError") return;
        });
    };
    pull();
    const id = window.setInterval(pull, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(id);
    };
  }, [equityHours]);

  // 日报每天一份，低频拉取即可；切回页面时也不必立刻重拉
  useEffect(() => {
    const controller = new AbortController();
    const pull = () => {
      fetchReports(controller.signal)
        .then(setReports)
        .catch((err) => {
          if ((err as Error).name === "AbortError") return;
        });
    };
    pull();
    const id = window.setInterval(pull, 60_000);
    return () => {
      controller.abort();
      window.clearInterval(id);
    };
  }, []);

  // 数据质量报告跟着 candles 每轮刷新，页面每分钟拉一次摘要即可
  useEffect(() => {
    const controller = new AbortController();
    const pull = () => {
      fetchQuality(controller.signal)
        .then(setQuality)
        .catch((err) => {
          if ((err as Error).name === "AbortError") return;
        });
    };
    pull();
    const id = window.setInterval(pull, 60_000);
    return () => {
      controller.abort();
      window.clearInterval(id);
    };
  }, []);

  // 风控限额单独轮询：接口不存在（后端未重启）时只让这张卡降级，
  // 不能因为一个 404 让 fetchAll 的 Promise.all 整体失败、整页失联
  useEffect(() => {
    const controller = new AbortController();
    const pull = () => {
      fetchRiskLimits(controller.signal)
        .then((value) => {
          setRisk(value);
          setRiskError(null);
        })
        .catch((err) => {
          if ((err as Error).name === "AbortError") return;
          setRisk(null);
          setRiskError((err as Error).message);
        });
    };
    pull();
    const id = window.setInterval(pull, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(id);
    };
  }, [riskRefresh]);

  // M1-5 进程心跳与 M1-7 数据新鲜度同频；两者互不拖累，各自失败各自置空
  useEffect(() => {
    const controller = new AbortController();
    const pull = () => {
      fetchProcesses(controller.signal)
        .then(setProcesses)
        .catch((err) => {
          if ((err as Error).name === "AbortError") return;
          setProcesses(null);
        });
      fetchFreshness(controller.signal)
        .then(setFreshness)
        .catch((err) => {
          if ((err as Error).name === "AbortError") return;
          setFreshness(null);
        });
    };
    pull();
    const id = window.setInterval(pull, 15_000);
    return () => {
      controller.abort();
      window.clearInterval(id);
    };
  }, []);

  const overview = data?.overview;
  const positions = data?.positions.positions ?? [];
  const orders = data?.orders.orders ?? [];
  const fills = data?.fills.fills ?? [];
  const alerts = data?.alerts.alerts ?? [];
  const equityPoints = equity?.points ?? [];
  const equityRange =
    EQUITY_RANGES.find((item) => item.hours === equityHours) ?? EQUITY_RANGES[0];
  const degraded = useMemo(
    () =>
      new Set([
        ...(overview?.degraded ?? []),
        ...(data?.alerts.degraded ?? []),
        ...(data?.pnl.degraded ?? []),
        ...(risk?.degraded ?? []),
        ...(reports?.degraded ?? []),
        ...(quality?.degraded ?? []),
        ...(equity?.degraded ?? []),
      ]),
    [overview, data, risk, equity, reports, quality],
  );
  const alertStats = useMemo(() => {
    const byLevel: Record<string, number> = {};
    const byRule = new Map<string, number>();
    for (const alert of alerts) {
      const level = alert.level.toUpperCase();
      byLevel[level] = (byLevel[level] ?? 0) + 1;
      byRule.set(alert.rule, (byRule.get(alert.rule) ?? 0) + 1);
    }
    const rules = [...byRule.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
    return {
      total: alerts.length,
      error: byLevel.ERROR ?? 0,
      byLevel,
      rules,
    };
  }, [alerts]);

  const stale =
    overview !== undefined &&
    (overview.snapshot_age_seconds === null || overview.snapshot_age_seconds > 180);

  return (
    <div className="shell">
      <div className="grain" aria-hidden="true" />
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">Q</span>
          <div className="brand-text">
            <span className="brand-title">QUANT / OPS</span>
            <span className="brand-sub">模拟盘只读终端</span>
          </div>
        </div>
        <div className="topmeta">
          <span className={`pulse ${error ? "pulse-bad" : ""}`} key={pulse} />
          <span className="topmeta-label">{error ? "连接中断" : "轮询中"}</span>
          <span className="topmeta-time">{clock(updatedAt ?? undefined)}</span>
        </div>
      </header>

      <div className="rail" role="status" aria-live="polite">
        <Chip
          label="KILL SWITCH"
          value={
            !overview
              ? "—"
              : !overview.kill_switch.known
                ? "未知"
                : overview.kill_switch.active
                  ? "已激活"
                  : "未激活"
          }
          tone={
            !overview || !overview.kill_switch.known
              ? "idle"
              : overview.kill_switch.active
                ? "bad"
                : "ok"
          }
          note={overview?.kill_switch.reason ?? undefined}
        />
        <Chip
          label="WORKER"
          value={overview?.worker.online ? "在线" : "离线"}
          tone={overview ? (overview.worker.online ? "ok" : "bad") : "idle"}
          note={heartbeatNote(overview?.worker.age_seconds)}
        />
        <Chip
          label="MONITOR"
          value={overview?.monitor.online ? "在线" : "离线"}
          tone={overview ? (overview.monitor.online ? "ok" : "bad") : "idle"}
          note={heartbeatNote(overview?.monitor.age_seconds)}
        />
        <Chip
          label="QUEUE"
          value={overview ? String(overview.queue.pending ?? "—") : "—"}
          tone={
            overview && (overview.queue.pending ?? 0) > 50 ? "warn" : "idle"
          }
          note={overview ? `inflight ${overview.queue.inflight ?? "—"}` : undefined}
        />
        <Chip
          label="DEAD"
          value={overview ? String(overview.queue.dead ?? "—") : "—"}
          tone={overview && (overview.queue.dead ?? 0) > 0 ? "warn" : "ok"}
          note="死信队列"
        />
        <Chip
          label="ALERTS"
          value={alertStats.error > 0 ? `${alertStats.error} ERROR` : "无 ERROR"}
          tone={alertStats.error > 0 ? "bad" : "ok"}
          note={`共 ${alertStats.total} 条`}
        />
        <Chip
          label="SOURCES"
          value={degraded.size > 0 ? "降级" : "正常"}
          tone={degraded.size > 0 ? "warn" : "ok"}
          note={degraded.size > 0 ? [...degraded].join(" / ") : "redis + sqlite"}
        />
      </div>

      {error && (
        <div className="banner banner-bad">
          接口异常：{error}。请确认 <code>quant dashboard</code> 是否在运行。
        </div>
      )}
      {stale && (
        <div className="banner banner-warn">
          monitor 快照已过期或缺失。账户/指标数据停更，订单与成交仍来自本地 SQLite。
        </div>
      )}

      <div className="layout">
        <aside className="sidebar">
          <nav aria-label="模块导航">
            <span className="nav-title">模块</span>
            <ul className="nav-list">
              {TABS.map((item, index) => (
                <li key={item.key}>
                  <button
                    type="button"
                    className={`nav-item ${tab === item.key ? "nav-on" : ""}`}
                    aria-current={tab === item.key ? "page" : undefined}
                    onClick={() => selectTab(item.key)}
                  >
                    <span className="nav-idx">{String(index + 1).padStart(2, "0")}</span>
                    <span className="nav-label">{item.label}</span>
                    {item.status === "todo" && <span className="nav-tag">未开始</span>}
                  </button>
                </li>
              ))}
            </ul>
          </nav>
        </aside>

      <main className="grid">
        {tab === "overview" && (
          <>
        <section className="card hero reveal" style={{ animationDelay: "40ms" }}>
          <div className="card-head">
            <h2>账户权益</h2>
            <div className="chart-tools">
              <div className="seg" role="group" aria-label="权益曲线时间范围">
                {EQUITY_RANGES.map((item) => (
                  <button
                    key={item.hours}
                    type="button"
                    className={`seg-btn ${equityHours === item.hours ? "seg-on" : ""}`}
                    onClick={() => setEquityHours(item.hours)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <div className="equity">
            <span className="equity-value">
              {num(overview?.account?.total_equity, 2)}
            </span>
            <span className="equity-unit">USDT</span>
          </div>
          <EquityCurve points={equityPoints} label={equityRange.label} />
          <dl className="kv">
            <div>
              <dt>可用</dt>
              <dd>{num(overview?.account?.avail_usdt, 2)}</dd>
            </div>
            <div>
              <dt>冻结</dt>
              <dd>{num(overview?.account?.frozen_usdt, 2)}</dd>
            </div>
            <div>
              <dt>未实现盈亏</dt>
              <dd className={sign(overview?.account?.unrealized_pnl)}>
                {num(overview?.account?.unrealized_pnl, 2)}
              </dd>
            </div>
            <div>
              <dt>保证金率</dt>
              <dd>{overview?.account?.margin_ratio ? pct(overview.account.margin_ratio) : "—"}</dd>
            </div>
          </dl>
          <div className="orders-line">
            {["filled", "pending", "canceled"].map((status) => (
              <span key={status} className="orders-pill">
                <em>{status}</em>
                <b>{overview?.orders[status] ?? 0}</b>
              </span>
            ))}
            <span className="orders-pill">
              <em>持仓数</em>
              <b>{overview?.positions_count ?? positions.length}</b>
            </span>
          </div>
        </section>

        <section className="card metrics reveal" style={{ animationDelay: "90ms" }}>
          <div className="card-head">
            <h2>风险指标</h2>
            <span className="card-note">
              {overview?.snapshot_age_seconds !== null &&
              overview?.snapshot_age_seconds !== undefined
                ? `${age(overview.snapshot_age_seconds)} 前刷新`
                : "等待 monitor"}
            </span>
          </div>
          <div className="metric-grid">
            <Metric
              label="最大回撤"
              value={pct(overview?.metrics?.max_drawdown)}
              hint="相对历史峰值权益"
              tone={drawdownTone(overview?.metrics?.max_drawdown)}
            />
            <Metric
              label="撤单率"
              value={overview?.metrics?.cancel_rate ? pct(overview.metrics.cancel_rate) : "—"}
              hint="撤销 / (成交 + 撤销)"
              tone={cancelTone(overview?.metrics?.cancel_rate)}
            />
            <Metric
              label="最小强平距离"
              value={
                overview?.metrics?.min_liq_distance
                  ? pct(overview.metrics.min_liq_distance)
                  : "—"
              }
              hint="越接近 0 越危险"
              tone={liqTone(overview?.metrics?.min_liq_distance)}
            />
            <Metric
              label="挂单 / 处理中"
              value={`${overview?.queue.pending ?? "—"} / ${overview?.queue.inflight ?? "—"}`}
              hint="Redis 信号队列"
              tone="neutral"
            />
          </div>
        </section>

        <section className="card pos-summary reveal" style={{ animationDelay: "105ms" }}>
          <div className="card-head">
            <h2>持仓摘要</h2>
            <button
              type="button"
              className="link-btn"
              onClick={() => selectTab("execution")}
            >
              去交易执行 {positions.length} 个品种 →
            </button>
          </div>
          <Table
            empty={positions.length === 0}
            emptyHint="当前无持仓"
            head={["品种", "方向", "数量", "开仓均价", "未实现盈亏", "杠杆"]}
            rows={positions.map((row) => positionSummaryCells(row))}
            onRowClick={() => selectTab("execution")}
          />
        </section>

        <section className="card alert-summary reveal" style={{ animationDelay: "115ms" }}>
          <div className="card-head">
            <h2>告警摘要</h2>
            <span className="card-note">最近 {alerts.length} 条 · 已读/静音在日志告警页</span>
            <button
              type="button"
              className="link-btn"
              onClick={() => selectTab("logs")}
            >
              查看全部 →
            </button>
          </div>
          <div className="metric-grid">
            <Metric
              label="ERROR"
              value={String(alertStats.byLevel.ERROR ?? 0)}
              hint="需立即处理"
              tone={(alertStats.byLevel.ERROR ?? 0) > 0 ? "bad" : "good"}
            />
            <Metric
              label="WARNING"
              value={String(alertStats.byLevel.WARNING ?? 0)}
              hint="待观察"
              tone={(alertStats.byLevel.WARNING ?? 0) > 0 ? "warn" : "good"}
            />
            <Metric
              label="INFO"
              value={String(alertStats.byLevel.INFO ?? 0)}
              hint="仅记录"
              tone="neutral"
            />
          </div>
          {alertStats.rules.length > 0 && (
            <ul className="alert-rules">
              {alertStats.rules.map(([rule, count]) => (
                <li key={rule}>
                  <span className="mono">{rule}</span>
                  <b>{count}</b>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card freshness reveal" style={{ animationDelay: "120ms" }}>
          <div className="card-head">
            <h2>数据新鲜度</h2>
            <span className="card-note">
              {freshness
                ? `${freshness.stale_count > 0 ? `${freshness.stale_count} 项落后` : "全部新鲜"} · 阈值 ${freshness.threshold_bars} 根`
                : "接口不可用"}
            </span>
          </div>
          <FreshnessPanel freshness={freshness} />
        </section>

        <section className="card process-health reveal" style={{ animationDelay: "125ms" }}>
          <div className="card-head">
            <h2>进程健康</h2>
            <span className="card-note">
              {processes
                ? `${processes.online} / ${processes.total} 在线${
                    processes.offline.length > 0
                      ? ` · 离线 ${processes.offline.join(" / ")}`
                      : ""
                  }`
                : "接口不可用"}
            </span>
          </div>
          <ProcessGrid processes={processes} />
          {data?.engine && (
            <p className="proc-engine mono">
              engine 当前周期 {data.engine.strategy} · {data.engine.inst} ·{" "}
              {data.engine.bar}
              {data.engine.watermark_age_seconds === null
                ? " · 尚未评估"
                : ` · 最近评估 ${age(data.engine.watermark_age_seconds)} 前`}
              {data.engine.lag_bars !== null
                ? ` · 落后 ${data.engine.lag_bars} 根`
                : ""}
            </p>
          )}
        </section>
          </>
        )}

        {tab === "risk" && (
          <>
        <section className="card risk-limits reveal" style={{ animationDelay: "105ms" }}>
          <div className="card-head">
            <h2>风控限额</h2>
            <span className="card-note">
              {riskError
                ? "接口不可用"
                : risk?.known
                  ? `${risk.strategy ?? "—"}${
                      risk.paper_mode ? " · 模拟盘" : ""
                    } · 权益 ${num(risk.equity)} · UTC+8 00:00 重置`
                  : "等待 worker 发布生效值"}
            </span>
          </div>
          <RiskLimitsPanel
            risk={risk}
            error={riskError}
            onResumed={() => setRiskRefresh((n) => n + 1)}
          />
        </section>

        <section className="card kill reveal" style={{ animationDelay: "120ms" }}>
          <div className="card-head">
            <h2>紧急停止</h2>
            <span className="card-note">停止开新仓 · 撤销挂单 · 不停进程</span>
          </div>
          <KillSwitchPanel state={overview?.kill_switch} />
        </section>
          </>
        )}

        {tab === "strategy" && <EnginePanel engine={data?.engine} />}

        {tab === "market" && <ChartPanel />}

        {tab === "backtest" && <BacktestPanel />}

        {tab === "execution" && (
        <section className="card positions reveal" style={{ animationDelay: "140ms" }}>
          <PositionsPanel rows={positions} />
        </section>
        )}

        {tab === "logs" && (
        <section className="card alerts reveal" style={{ animationDelay: "190ms" }}>
          <div className="card-head">
            <h2>告警</h2>
            <span className="card-note">
              {data?.alerts.total ?? alerts.length} 条 · 最近在前
              {data?.alerts.degraded.length
                ? ` · 降级：${data.alerts.degraded.join(" / ")}`
                : ""}
            </span>
          </div>
          <AlertPanel
            alerts={alerts}
            silences={data?.alerts.silences ?? []}
            onChanged={refresh}
          />
        </section>
        )}

        {tab === "execution" && (
          <>
        <section className="card dead reveal" style={{ animationDelay: "215ms" }}>
          <DeadLettersPanel />
        </section>

        <section className="card pnl reveal" style={{ animationDelay: "240ms" }}>
          <div className="card-head">
            <h2>成交盈亏</h2>
            <span className="card-note">
              {data?.pnl
                ? `FIFO 回合 · ${data.pnl.round_trips} 个已平仓`
                : "等待数据"}
            </span>
          </div>
          <PnlPanel pnl={data?.pnl} />
        </section>
          </>
        )}

        {tab === "backtest" && (
        <section className="card reports reveal" style={{ animationDelay: "280ms" }}>
          <div className="card-head">
            <h2>模拟盘日报</h2>
            <span className="card-note">
              {reports
                ? `每日 08:00 · 偏离阈值 ${pct(String(reports.threshold))}`
                : "等待数据"}
            </span>
          </div>
          <ReportsPanel reports={reports} />
        </section>
        )}

        {tab === "market" && (
        <section className="card quality reveal" style={{ animationDelay: "300ms" }}>
          <div className="card-head">
            <h2>数据质量</h2>
            <span className="card-note">
              {quality
                ? `${quality.reports.length} 个品种 · 缺口不会自动消失`
                : "等待数据"}
            </span>
          </div>
          <QualityPanel quality={quality} />
        </section>
        )}

        {tab === "execution" && (
          <>
        <section className="card orders reveal" style={{ animationDelay: "280ms" }}>
          <div className="card-head">
            <h2>最近订单</h2>
            <span className="card-note">SQLite · 倒序</span>
          </div>
          <Table
            empty={orders.length === 0}
            emptyHint="暂无订单记录"
            head={["时间", "品种", "方向", "类型", "数量", "价格", "状态"]}
            rows={orders.map((row) => orderCells(row))}
          />
        </section>

        <section className="card fills reveal" style={{ animationDelay: "290ms" }}>
          <div className="card-head">
            <h2>最近成交</h2>
            <span className="card-note">
              累计手续费 {num(data?.fills.total_fee, 4)}
            </span>
          </div>
          <Table
            empty={fills.length === 0}
            emptyHint="暂无成交记录"
            head={["时间", "品种", "方向", "数量", "成交价", "名义", "手续费"]}
            rows={fills.map((row) => fillCells(row))}
          />
        </section>
          </>
        )}

        {(tab === "factors" || tab === "portfolio" || tab === "settings") && (
          <ModulePlaceholder tab={tab} />
        )}
      </main>
      </div>

      <footer className="footer">
        <span>只读终端 · 每 {POLL_MS / 1000}s 轮询</span>
        <span>
          Redis 快照 + SQLite 状态 · 最后更新 {clock(updatedAt ?? undefined)}
        </span>
      </footer>
    </div>
  );
}

/** 未开始模块的占位卡：只说明范围，不提供任何写操作。 */
function ModulePlaceholder({ tab }: { tab: PendingTab }) {
  const meta = PENDING_MODULES[tab];
  return (
    <section className="card placeholder reveal">
      <div className="card-head">
        <h2>{meta.title}</h2>
        <span className="card-note">PRD {meta.code} · 未开始</span>
      </div>
      <p className="placeholder-text">{meta.detail}</p>
      <ul className="placeholder-list">
        {meta.items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

/** 风控限额：上限是 worker 发布的生效值，已用额度由后端现算。 */
function RiskLimitsPanel({
  risk,
  error,
  onResumed,
}: {
  risk: RiskLimitsResponse | null;
  error: string | null;
  onResumed: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ bad: boolean; text: string } | null>(null);

  if (error) {
    return (
      <p className="risk-unknown">
        接口不可用（{error}）：看板后端还是旧进程，重启后该卡片才会生效；
        其余面板不受影响。
      </p>
    );
  }
  if (!risk?.known) {
    return (
      <p className="risk-unknown">
        未收到 worker 的生效风控参数：<code>quant run-worker</code> 启动后每
        60s 发布一次，停机 5 分钟后过期。
      </p>
    );
  }

  // 旧后端可能尚未返回 circuits，这里兜底为空数组
  const circuits = risk.circuits ?? [];

  const resume = async (instId: string) => {
    setBusy(instId);
    setMsg(null);
    try {
      const result = await resumeCircuit(
        instId,
        window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? "",
      );
      if (result.resumed) {
        setMsg({ bad: false, text: `${instId} 已恢复交易` });
        onResumed();
      } else if (result.reason === "cooldown not finished") {
        setMsg({ bad: true, text: `${instId} 冷却期未结束，暂不能恢复` });
      } else {
        setMsg({ bad: true, text: `${instId} 已不处于熔断状态` });
      }
    } catch (err) {
      setMsg({ bad: true, text: (err as Error).message });
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      {circuits.length > 0 && (
        <div className="risk-circuits">
          {circuits.map((item) => {
            const awaiting = item.status === "awaiting_resume";
            return (
              <div className="risk-circuit" key={item.inst_id}>
                <b>
                  {item.inst_id}{" "}
                  {awaiting ? "冷却结束 · 待确认恢复" : "熔断中"}
                </b>
                <span className="risk-circuit-reason">{item.reason}</span>
                <span>
                  {awaiting
                    ? `冷却已于 ${clock(item.resume_at)} 结束，需人工确认后才恢复交易`
                    : `预计 ${clock(item.resume_at)} 进入待确认`}
                  ；熔断期间该品种信号会被 worker 拒绝，其它品种不受影响
                </span>
                {awaiting && (
                  <button
                    type="button"
                    className="dl-btn"
                    disabled={busy === item.inst_id}
                    onClick={() => resume(item.inst_id)}
                  >
                    {busy === item.inst_id ? "恢复中…" : "确认恢复"}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
      {msg && (
        <p className={msg.bad ? "dl-msg dl-bad" : "dl-msg"}>{msg.text}</p>
      )}
      <div className="risk-grid">
        {risk.rules.map((rule) => (
          <RiskLimitRow key={rule.key} rule={rule} />
        ))}
      </div>
      <RiskOverridesEditor strategy={risk.strategy} />
    </>
  );
}

// 在线调整只暴露阈值字段；顺序与后端 LIMIT_FIELDS 一致
const RISK_PARAM_LABELS: Record<string, string> = {
  daily_loss_limit: "日亏损比例",
  consecutive_loss_limit: "连续亏损笔数",
  max_order_qty: "单笔最大名义 (USDT)",
  max_position_pct: "单品种仓位比例",
  max_total_pct: "总仓位比例",
};

const RISK_SOURCE_TEXT: Record<string, string> = {
  default: "默认",
  global: "全局",
  strategy: "策略",
  override: "覆盖",
};

/** 阈值在线调整（M7-8）+ 变更审计（M7-14）。 */
function RiskOverridesEditor({ strategy }: { strategy: string | null }) {
  const [data, setData] = useState<RiskOverridesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [param, setParam] = useState(Object.keys(RISK_PARAM_LABELS)[0]);
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ bad: boolean; text: string } | null>(null);

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const res = await fetchRiskOverrides(signal, strategy);
        setData(res);
        setError(null);
      } catch (err) {
        // 主动 abort 不算错误
        if ((err as Error).name === "AbortError") return;
        setError((err as Error).message);
      }
    },
    [strategy],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  /** 写操作后立即重取：不必等下一次页面轮询。 */
  const refresh = () => load(new AbortController().signal);

  if (!strategy) return null;

  const token = window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? "";

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await setRiskOverride(
        {
          strategy_id: strategy,
          param,
          value: value.trim(),
          reason: reason.trim(),
        },
        token,
      );
      setMsg({
        bad: false,
        text: `${RISK_PARAM_LABELS[param] ?? param} 已设为 ${value.trim()}，worker 下一轮心跳生效`,
      });
      setValue("");
      setReason("");
      await refresh();
    } catch (err) {
      setMsg({ bad: true, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const clear = async (name: string) => {
    setBusy(true);
    setMsg(null);
    try {
      const res = await clearRiskOverride(strategy, name, token);
      setMsg({
        bad: !res.cleared,
        text: res.cleared
          ? `${RISK_PARAM_LABELS[name] ?? name} 覆盖已清除，回落到配置层`
          : "该字段当前没有生效的覆盖",
      });
      await refresh();
    } catch (err) {
      setMsg({ bad: true, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const effective = data?.effective ?? {};

  return (
    <div className="risk-ov">
      <div className="risk-ov-head">在线调整阈值</div>
      <div className="risk-ov-form">
        <select
          value={param}
          onChange={(event) => setParam(event.target.value)}
        >
          {Object.entries(RISK_PARAM_LABELS).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
        <input
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder={effective[param] ?? "新值"}
          inputMode="decimal"
        />
        <input
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="变更原因（必填，写入审计）"
        />
        <button
          type="button"
          className="dl-btn"
          disabled={busy || !value.trim() || !reason.trim()}
          onClick={save}
        >
          {busy ? "提交中…" : "保存"}
        </button>
      </div>
      <p className="risk-ov-hint">
        覆盖值立即写 Redis，worker 下一轮心跳重读生效（最长 60s），不写回
        toml；清除后回落到全局/策略取严结果。每次调整都会记入
        <code>risk_overrides</code>。
      </p>
      {Object.keys(effective).length > 0 && (
        <div className="risk-ov-active">
          {Object.entries(effective).map(([name, val]) => (
            <span className="risk-ov-chip" key={name}>
              {RISK_PARAM_LABELS[name] ?? name} = <b>{val}</b>
              <button
                type="button"
                onClick={() => clear(name)}
                disabled={busy}
                title="清除覆盖，回落到配置值"
              >
                清除
              </button>
            </span>
          ))}
        </div>
      )}
      {msg && (
        <p className={msg.bad ? "dl-msg dl-bad" : "dl-msg"}>{msg.text}</p>
      )}
      {error && <p className="dl-msg dl-bad">读取覆盖失败：{error}</p>}
      {data && data.items.length > 0 && (
        <details className="risk-ov-history">
          <summary>变更历史（{data.total ?? data.items.length}）</summary>
          <ul>
            {data.items.map((item) => (
              <li key={item.id}>
                <span className="mono">{stamp(item.changed_at)}</span>
                <span>{RISK_PARAM_LABELS[item.param] ?? item.param}</span>
                <span className="mono">
                  {item.old_value ?? "配置值"} → {item.new_value}
                </span>
                <span>{item.changed_by}</span>
                <span className="risk-ov-reason">{item.reason}</span>
                {item.revoked_at ? (
                  <span className="risk-ov-revoked">已清除</span>
                ) : null}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function RiskLimitRow({ rule }: { rule: RiskLimitRule }) {
  const ratio = rule.ratio === null ? null : Number(rule.ratio);
  const limit = Number(rule.limit ?? 0);
  const used = Number(rule.used ?? 0);
  // 连亏用点阵比进度条更直观；异常上限不画，退回数值展示
  const dots =
    rule.key === "consecutive_loss" && Number.isFinite(limit) && limit > 0
      ? Math.min(Math.round(limit), 20)
      : 0;
  const width = ratio === null ? 0 : Math.min(ratio, 1) * 100;

  return (
    <div className={`risk-item risk-${rule.level}`}>
      <div className="risk-top">
        <span className="risk-label">
          {rule.label}
          {rule.source ? (
            <em className={`risk-src risk-src-${rule.source}`}>
              {RISK_SOURCE_TEXT[rule.source] ?? rule.source}
            </em>
          ) : null}
        </span>
        <span className="risk-value">
          {rule.used === null ? "参考" : num(rule.used)} / {num(rule.limit)}
        </span>
      </div>
      {dots > 0 ? (
        <>
          <div className="risk-dots" title={rule.detail}>
            {Array.from({ length: dots }, (_, index) => (
              <span
                key={index}
                className={index < used ? "risk-dot risk-dot-on" : "risk-dot"}
              />
            ))}
          </div>
          <div className="risk-foot">
            <span>{rule.detail}</span>
            <b>{pct(rule.ratio)}</b>
          </div>
        </>
      ) : ratio === null ? (
        <div className="risk-ref">{rule.detail}</div>
      ) : (
        <>
          <div className="risk-track" title={rule.detail}>
            <span className="risk-fill" style={{ width: `${width}%` }} />
            <span className="risk-tick risk-tick-warn" />
            <span className="risk-tick risk-tick-halt" />
          </div>
          <div className="risk-foot">
            <span>{rule.detail}</span>
            <b>{pct(rule.ratio)}</b>
          </div>
        </>
      )}
      <RiskTriggers rule={rule} />
    </div>
  );
}

/** 今日触发次数与最近一次详情（M7-9）。未触发时不占空间地说明一次。 */
function RiskTriggers({ rule }: { rule: RiskLimitRule }) {
  const triggers = rule.triggers ?? 0;
  const last = rule.last_event;
  if (!triggers || !last) {
    return <div className="risk-triggers">今日未触发</div>;
  }
  const decision =
    last.decision === "halted"
      ? "硬停止"
      : last.decision === "rejected"
        ? "拒单"
        : "减量";
  const qty =
    last.qty_before && last.qty_after
      ? ` ${num(last.qty_before)} → ${num(last.qty_after)}`
      : "";
  return (
    <div className="risk-triggers" title={last.reason ?? undefined}>
      <span className="risk-trigger-count">今日触发 {triggers} 次</span>
      <span>
        最近 {clock(last.ts).slice(0, 5)} {decision}
        {qty}
      </span>
    </div>
  );
}

const KILL_TOKEN_KEY = "quant.dashboard.token";

// 手续费率按回测默认 0.1% 估算，仅用于下单前预览
const FEE_RATE = 0.001;

function holdingOf(row: PositionRow): number {
  return Number(row.local_qty ?? row.qty ?? 0);
}

/** 持仓操作：选中一行后出现操作条，只允许减仓。 */
function PositionsPanel({ rows }: { rows: PositionRow[] }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [ratio, setRatio] = useState<number | null>(null);
  const [qty, setQty] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [flattenOpen, setFlattenOpen] = useState(false);
  const [flattenWord, setFlattenWord] = useState("");
  const [flattenReason, setFlattenReason] = useState("");
  const [flattenReport, setFlattenReport] = useState<string[]>([]);

  const row = rows.find((item) => item.inst_id === selected) ?? null;
  const holding = row ? holdingOf(row) : 0;
  const price = Number(
    row?.mark_price ?? row?.avg_price ?? row?.local_avg_price ?? 0
  );
  const previewQty = ratio !== null ? holding * ratio : Number(qty || 0);
  const previewValue = Number.isFinite(previewQty) ? previewQty * price : 0;

  const pick = (instId: string) => {
    setSelected(instId === selected ? null : instId);
    setRatio(null);
    setQty("");
    setError("");
    setNotice("");
  };

  const applyRatio = (value: number) => {
    setRatio(value);
    // 输入框跟着联动，用户改数字会取消高亮
    setQty(holding > 0 ? String(Number((holding * value).toFixed(8))) : "");
    setNotice("");
  };

  const submitReduce = async () => {
    if (!row) return;
    if (!reason.trim()) {
      setError("请填写操作原因");
      return;
    }
    if (previewQty <= 0) {
      setError("请选择比例或填写数量");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await reducePosition(
        row.inst_id,
        ratio !== null
          ? { ratio, reason: reason.trim() }
          : { qty, reason: reason.trim() },
        window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? ""
      );
      const filled = result.fill;
      setNotice(
        filled
          ? `已卖出 ${compactQty(filled.qty)} @ ${num(filled.price, 2)}，手续费 ${num(filled.fee, 4)}`
          : `已提交（${result.status ?? "pending"}），等待交易所成交`
      );
      setQty("");
      setRatio(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submitFlatten = async () => {
    if (flattenWord.trim().toUpperCase() !== "FLATTEN") {
      setError("确认词不匹配：请输入 FLATTEN");
      return;
    }
    if (!flattenReason.trim()) {
      setError("请填写操作原因");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    setFlattenReport([]);
    try {
      const result = await flattenPositions(
        flattenReason.trim(),
        window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? ""
      );
      setFlattenReport(
        result.results.map((item) => {
          const who = item.inst_id;
          if (!item.ok) return `${who}：失败 — ${item.error ?? "未知错误"}`;
          return item.fill
            ? `${who}：卖出 ${compactQty(item.fill.qty)} @ ${num(item.fill.price, 2)}`
            : `${who}：已提交，等待成交`;
        })
      );
      setNotice(
        result.total === 0
          ? "当前没有可平持仓"
          : `全平完成：成功 ${result.succeeded} / ${result.total}`
      );
      setFlattenOpen(false);
      setFlattenWord("");
      setFlattenReason("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="card-head pos-head">
        <h2>持仓</h2>
        <span className="card-note">
          {rows.length} 条 · 点选一行可减仓
        </span>
        <button
          className="pos-flat"
          onClick={() => {
            setFlattenOpen(!flattenOpen);
            setFlattenReport([]);
            setError("");
          }}
          disabled={rows.length === 0 || busy}
        >
          一键全平
        </button>
      </div>

      {flattenOpen && (
        <div className="pos-flat-bar">
          <span className="pos-flat-warn">
            将对全部多头持仓下市价卖单，且不可撤销。输入 FLATTEN 确认：
          </span>
          <input
            className="pos-input"
            value={flattenWord}
            placeholder="FLATTEN"
            onChange={(e) => setFlattenWord(e.target.value)}
          />
          <input
            className="pos-input"
            value={flattenReason}
            placeholder="原因（必填）"
            onChange={(e) => setFlattenReason(e.target.value)}
          />
          <button className="pos-danger" onClick={submitFlatten} disabled={busy}>
            {busy ? "提交中…" : "确认全平"}
          </button>
        </div>
      )}

      <Table
        empty={rows.length === 0}
        emptyHint="暂无持仓：本地无记录，monitor 也未回报合约持仓"
        head={[
          "品种",
          "账户数量",
          "账户均价",
          "策略持仓",
          "标记价",
          "账户盈亏",
          "策略盈亏",
          "杠杆",
          "强平价",
          "距离",
        ]}
        rows={rows.map((item) => positionCells(item))}
        activeIndex={
          selected === null
            ? -1
            : rows.findIndex((item) => item.inst_id === selected)
        }
        onRowClick={(index) => pick(rows[index].inst_id)}
      />

      {row && (
        <div className="pos-bar">
          <span className="pos-who">
            {row.inst_id} · 可减 {compactQty(String(holding))}
          </span>
          {[0.25, 0.5, 1].map((value) => (
            <button
              key={value}
              className={ratio === value ? "pos-chip pos-chip-on" : "pos-chip"}
              onClick={() => applyRatio(value)}
              disabled={busy || holding <= 0}
            >
              {value === 1 ? "平仓" : `${value * 100}%`}
            </button>
          ))}
          <input
            className="pos-input pos-input-qty"
            value={qty}
            placeholder="数量（基础币）"
            onChange={(e) => {
              setQty(e.target.value);
              setRatio(null);
            }}
          />
          <input
            className="pos-input pos-input-reason"
            value={reason}
            placeholder="原因（必填）"
            onChange={(e) => setReason(e.target.value)}
          />
          {previewQty > 0 && (
            <span className="pos-est">
              ≈ {num(String(previewValue), 2)} USDT · 手续费 ≈{" "}
              {num(String(previewValue * FEE_RATE), 2)}
            </span>
          )}
          <button
            className="pos-danger"
            onClick={submitReduce}
            disabled={busy || holding <= 0}
          >
            {busy ? "提交中…" : "减仓"}
          </button>
        </div>
      )}

      {(error || notice) && (
        <div className={error ? "pos-msg pos-msg-bad" : "pos-msg"}>
          {error || notice}
        </div>
      )}
      {flattenReport.length > 0 && (
        <ul className="pos-report">
          {flattenReport.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
    </>
  );
}

/** 紧急停止：两段式确认，避免误点。 */
function KillSwitchPanel({ state }: { state?: Overview["kill_switch"] }) {
  const [local, setLocal] = useState<KillSwitchState | null>(null);
  const [pending, setPending] = useState<"on" | "off" | null>(null);
  const [reason, setReason] = useState("");
  const [token, setToken] = useState(
    () => window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? ""
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [cancelOrders, setCancelOrders] = useState(true);
  const [cancelError, setCancelError] = useState("");

  // 服务端状态一变就丢弃本地结果，避免遮住风控的自动激活
  useEffect(() => {
    setLocal(null);
    setCancelError("");
  }, [state?.active, state?.reason]);

  const active = local?.active ?? state?.active ?? false;
  const authRequired = local?.auth_required ?? state?.auth_required ?? false;

  const submit = async (action: "on" | "off") => {
    setBusy(true);
    setError("");
    setCancelError("");
    try {
      const withCancel = action === "on" && cancelOrders;
      const result = await setKillSwitch(
        action,
        action === "on" ? reason.trim() : "",
        token,
        withCancel
      );
      setLocal(result);
      if (token) window.sessionStorage.setItem(KILL_TOKEN_KEY, token);
      setPending(null);
      setReason("");
      const cancel = result.cancel;
      if (withCancel && cancel) {
        if (cancel.ok) {
          setNotice(
            `已停止开新仓，撤销挂单 ${cancel.exchange_canceled ?? 0} 张` +
              ((cancel.local_marked ?? 0) > 0
                ? `，本地标记 ${cancel.local_marked} 条`
                : "")
          );
        } else {
          setNotice("已停止开新仓");
          setCancelError(cancel.error || "撤单失败，请到交易所手动确认挂单");
        }
      } else {
        setNotice(action === "on" ? "已停止开新仓（未撤挂单）" : "已恢复开仓");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const start = () => {
    setPending(active ? "off" : "on");
    setError("");
    setNotice("");
  };

  return (
    <div className="kill-body">
      <div className="kill-status">
        <span className={active ? "kill-badge kill-badge-on" : "kill-badge"}>
          {active ? "已激活" : "未激活"}
        </span>
        <span className="kill-detail">
          {active
            ? `原因：${state?.reason || local?.reason || "未填写"}`
            : "停止后 worker 拒绝新信号并写入死信队列，engine 本轮跳过"}
        </span>
      </div>

      {pending === null ? (
        <button
          type="button"
          className={active ? "kill-btn kill-btn-safe" : "kill-btn kill-btn-danger"}
          onClick={start}
        >
          {active ? "解除停止" : "停止开新仓"}
        </button>
      ) : (
        <div className="kill-confirm">
          {pending === "on" && (
            <input
              className="kill-input"
              placeholder="原因（写进告警，便于复盘）"
              maxLength={60}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          )}
          {pending === "on" && (
            <label className="kill-check">
              <input
                type="checkbox"
                checked={cancelOrders}
                onChange={(e) => setCancelOrders(e.target.checked)}
              />
              同时撤销全部挂单
            </label>
          )}
          {authRequired && (
            <input
              className="kill-input"
              type="password"
              placeholder="DASHBOARD_TOKEN"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          )}
          <button
            type="button"
            className="kill-btn kill-btn-danger"
            disabled={busy}
            onClick={() => submit(pending)}
          >
            {busy ? "提交中…" : pending === "on" ? "确认停止" : "确认解除"}
          </button>
          <button
            type="button"
            className="kill-btn"
            disabled={busy}
            onClick={() => setPending(null)}
          >
            取消
          </button>
        </div>
      )}

      {error && <div className="kill-msg kill-msg-bad">操作失败：{error}</div>}
      {!error && cancelError && (
        <div className="kill-msg kill-msg-bad">
          撤单失败：{cancelError}（停止标志已生效，请到交易所核对挂单）
        </div>
      )}
      {!error && notice && <div className="kill-msg">{notice}</div>}
      <p className="kill-note">
        激活时默认撤销交易所全部挂单（等价
        <code>quant kill-switch --on</code>
        ）；撤单失败不影响停止标志，需人工到交易所确认。
      </p>
    </div>
  );
}

function EnginePanel({ engine }: { engine?: EngineResponse }) {
  const [info, setInfo] = useState<StrategyInfo | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [token, setToken] = useState(
    () => window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? ""
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [changes, setChanges] = useState<ParamChange[]>([]);
  const [bindings, setBindings] = useState<StrategyBinding[]>([]);
  const [defaultBar, setDefaultBar] = useState("1H");
  const [multiBinding, setMultiBinding] = useState(true);
  const [newInst, setNewInst] = useState("");
  const [newBar, setNewBar] = useState("");
  const [bindingBusy, setBindingBusy] = useState("");
  const [bindingError, setBindingError] = useState("");
  const strategyId = engine?.strategy;

  const loadInfo = useCallback(
    async (signal?: AbortSignal) => {
      if (!strategyId) return;
      const res = await fetchStrategies(signal ?? new AbortController().signal);
      const hit = res.strategies.find((item) => item.id === strategyId);
      setInfo(hit ?? null);
    },
    [strategyId]
  );

  useEffect(() => {
    const controller = new AbortController();
    loadInfo(controller.signal).catch(() => setInfo(null));
    return () => controller.abort();
  }, [loadInfo]);

  // 展开编辑时拉最近变更；保存后 notice 变化会触发重新拉取
  useEffect(() => {
    if (!editing || !strategyId) return;
    const controller = new AbortController();
    fetchParamChanges(controller.signal, strategyId)
      .then((res) => setChanges(res.changes))
      .catch(() => setChanges([]));
    return () => controller.abort();
  }, [editing, strategyId, notice]);

  const loadBindings = useCallback(
    async (signal?: AbortSignal) => {
      if (!strategyId) return;
      const res = await fetchBindings(
        signal ?? new AbortController().signal,
        strategyId
      );
      setBindings(res.bindings);
      setDefaultBar(res.default_bar);
      setMultiBinding(res.multi_binding ?? true);
      setNewBar((prev) => prev || res.default_bar);
    },
    [strategyId]
  );

  useEffect(() => {
    const controller = new AbortController();
    loadBindings(controller.signal).catch(() => setBindings([]));
    return () => controller.abort();
  }, [loadBindings, notice]);

  const toggleBinding = async (row: StrategyBinding) => {
    if (!strategyId) return;
    setBindingBusy(row.inst_id);
    setBindingError("");
    try {
      await updateBinding(
        strategyId,
        row.inst_id,
        { enabled: !row.enabled },
        token
      );
      if (token) window.sessionStorage.setItem(KILL_TOKEN_KEY, token);
      await loadBindings();
    } catch (e) {
      setBindingError((e as Error).message);
    } finally {
      setBindingBusy("");
    }
  };

  const addBinding = async () => {
    if (!strategyId || !newInst.trim()) return;
    setBindingBusy(newInst.trim());
    setBindingError("");
    try {
      await updateBinding(
        strategyId,
        newInst.trim(),
        { bar: newBar.trim() || defaultBar, enabled: true },
        token
      );
      if (token) window.sessionStorage.setItem(KILL_TOKEN_KEY, token);
      setNewInst("");
      await loadBindings();
    } catch (e) {
      setBindingError((e as Error).message);
    } finally {
      setBindingBusy("");
    }
  };

  const specs: ParamSpec[] = info?.param_spec ?? [];
  const sources = info?.sources ?? {};

  const startEdit = () => {
    const next: Record<string, string> = {};
    for (const spec of specs) {
      next[spec.name] = String(info?.params[spec.name] ?? spec.default);
    }
    setDraft(next);
    setReason("");
    setError("");
    setNotice("");
    setEditing(true);
  };

  const save = async () => {
    if (!strategyId) return;
    setBusy(true);
    setError("");
    try {
      const values: Record<string, unknown> = {};
      for (const spec of specs) {
        const raw = draft[spec.name] ?? "";
        // 非 bool 一律传字符串，后端按 PARAM_SPEC 做类型转换与范围校验
        values[spec.name] = spec.type === "bool" ? raw === "true" : raw.trim();
      }
      const res = await updateStrategyParams(strategyId, values, reason.trim(), token);
      if (token) window.sessionStorage.setItem(KILL_TOKEN_KEY, token);
      setInfo(res);
      setNotice(
        res.changed.length > 0
          ? `已更新：${res.changed.join(", ")}（下一轮 tick 生效）`
          : "没有变化，未写入"
      );
      setReason("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const paramsText = info
    ? Object.entries(info.params)
        .map(([key, value]) => `${key}=${String(value)}`)
        .join(" · ")
    : "—";
  const online = engine?.online ?? false;
  const tone: Tone = !engine ? "idle" : online ? "ok" : "bad";
  const toneClass = tone === "idle" ? "tag-muted" : `tag-${tone}`;
  const statusText = !engine
    ? "—"
    : !engine.status
      ? "无记录"
      : online
        ? ENGINE_STATUS[engine.status] ?? engine.status
        : "离线";
  const lag = engine?.lag_bars;
  const lagTone =
    lag === null || lag === undefined
      ? "tag-muted"
      : lag === 0
        ? "tag-ok"
        : lag > (engine?.max_catchup_bars ?? 24)
          ? "tag-bad"
          : "tag-warn";
  return (
    <section className="card engine reveal" style={{ animationDelay: "120ms" }}>
      <div className="card-head">
        <h2>策略引擎</h2>
        <span className="card-note">
          {engine
            ? `${engine.strategy} · ${engine.inst} · ${engine.bar}`
            : "等待数据"}
        </span>
      </div>
      <dl className="kv">
        <div>
          <dt>状态</dt>
          <dd className={`engine-state ${toneClass}`}>
            {statusText}
            {engine?.reason && (
              <span className="engine-reason" title={engine.reason}>
                {engine.reason}
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt>最近评估 bar</dt>
          <dd>{stamp(engine?.last_bar_ts)}</dd>
        </div>
        <div>
          <dt>水位延迟</dt>
          <dd className={lagTone}>
            {lag === null || lag === undefined
              ? "—"
              : lag === 0
                ? "已追平"
                : `${lag} 根`}
          </dd>
        </div>
        <div>
          <dt>最近心跳</dt>
          <dd>{heartbeatNote(engine?.status_age_seconds)}</dd>
        </div>
        <div>
          <dt>策略参数</dt>
          <dd className="engine-params">{paramsText}</dd>
        </div>
        <div>
          <dt>参数来源</dt>
          <dd className="engine-params">
            {Object.keys(sources).length === 0
              ? "—"
              : Object.entries(sources)
                  .map(([key, source]) =>
                    source === "db" ? `${key}(覆盖)` : key
                  )
                  .join(" · ")}
          </dd>
        </div>
      </dl>
      <div className="param-panel">
        <div className="param-head">
          <span className="card-note">
            {specs.length > 0
              ? `${specs.length} 个可在线调整的参数，其余只读`
              : "该策略未声明 PARAM_SPEC，参数只读"}
          </span>
          <button
            type="button"
            className="kill-btn"
            disabled={specs.length === 0}
            onClick={() => (editing ? setEditing(false) : startEdit())}
          >
            {editing ? "收起" : "调整参数"}
          </button>
        </div>
        {editing && (
          <div className="param-editor">
            {specs.map((spec) => (
              <label className="param-row" key={spec.name}>
                <span className="param-name">
                  {spec.name}
                  {spec.unit ? ` (${spec.unit})` : ""}
                </span>
                {spec.type === "bool" ? (
                  <input
                    type="checkbox"
                    checked={(draft[spec.name] ?? "false") === "true"}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        [spec.name]: e.target.checked ? "true" : "false",
                      })
                    }
                  />
                ) : spec.choices ? (
                  <select
                    className="kill-input"
                    value={draft[spec.name] ?? ""}
                    onChange={(e) =>
                      setDraft({ ...draft, [spec.name]: e.target.value })
                    }
                  >
                    {spec.choices.map((choice) => (
                      <option key={choice} value={choice}>
                        {choice}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    className="kill-input"
                    inputMode={
                      spec.type === "str" ? "text" : "decimal"
                    }
                    value={draft[spec.name] ?? ""}
                    onChange={(e) =>
                      setDraft({ ...draft, [spec.name]: e.target.value })
                    }
                  />
                )}
                <span className="param-hint">
                  {spec.description}
                  {spec.min !== null || spec.max !== null
                    ? `（${spec.min ?? "-∞"} ~ ${spec.max ?? "+∞"}）`
                    : ""}
                </span>
              </label>
            ))}
            <input
              className="kill-input"
              placeholder="变更原因（写入审计）"
              maxLength={60}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
            <input
              className="kill-input"
              type="password"
              placeholder="DASHBOARD_TOKEN（如已配置）"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
            <button
              type="button"
              className="kill-btn kill-btn-danger"
              disabled={busy}
              onClick={() => void save()}
            >
              {busy ? "保存中…" : "保存覆盖"}
            </button>
            {error && <div className="kill-msg kill-msg-bad">保存失败：{error}</div>}
            {!error && notice && <div className="kill-msg">{notice}</div>}
            <p className="kill-note">
              保存后写入 strategy_params 并记变更历史，下一轮引擎 tick 生效，
              无需重启。
            </p>
            {changes.length > 0 && (
              <ul className="param-history">
                {changes.map((row) => (
                  <li key={row.id}>
                    <span>{clock(row.changed_at)}</span>
                    <span>
                      {row.param}：{row.old_value ?? "—"} → {row.new_value}
                    </span>
                    <span>{row.changed_by}</span>
                    {row.reason && <span className="param-reason">{row.reason}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
      <div className="param-panel">
        <div className="param-head">
          <span className="card-note">
            启停矩阵：停用只停止新开仓，已有持仓保留
          </span>
        </div>
        {!multiBinding && (
          <div className="kill-msg kill-msg-bad">
            已开启单组合回滚（multi_binding=false），引擎只跑配置里的主组合，
            此矩阵不生效
          </div>
        )}
        {bindings.length === 0 ? (
          <p className="kill-note">当前没有绑定，引擎不会评估该策略。</p>
        ) : (
          <ul className="param-history">
            {bindings.map((row) => (
              <li key={row.inst_id}>
                <span className="mono">{row.inst_id}</span>
                <span>{row.bar}</span>
                <span className={row.enabled ? "tag-ok" : "tag-muted"}>
                  {row.enabled ? "启用" : "已停用"}
                </span>
                <button
                  type="button"
                  className="kill-btn"
                  disabled={bindingBusy === row.inst_id}
                  onClick={() => void toggleBinding(row)}
                >
                  {row.enabled ? "停用" : "启用"}
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="param-head">
          <input
            className="kill-input"
            placeholder="品种，如 ETH-USDT"
            value={newInst}
            onChange={(e) => setNewInst(e.target.value)}
          />
          <input
            className="kill-input"
            placeholder={`周期（默认 ${defaultBar}）`}
            value={newBar}
            onChange={(e) => setNewBar(e.target.value)}
          />
          <button
            type="button"
            className="kill-btn"
            disabled={!newInst.trim() || bindingBusy === newInst.trim()}
            onClick={() => void addBinding()}
          >
            新增绑定
          </button>
        </div>
        <input
          className="kill-input"
          type="password"
          placeholder="DASHBOARD_TOKEN（如已配置）"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        {bindingError && (
          <div className="kill-msg kill-msg-bad">绑定失败：{bindingError}</div>
        )}
        <p className="kill-note">
          写入后同步 SQLite 与 Redis 缓存，引擎下一轮（≤ 轮询周期）生效，
          无需重启；同一组合改周期须先解绑。
        </p>
      </div>
      <div className="orders-line">
        <span className="orders-pill">
          <em>本轮补评估</em>
          <b>{engine?.catch_up ?? 0}</b>
        </span>
        <span className="orders-pill">
          <em>累计补评估</em>
          <b>{engine?.catch_up_total ?? 0}</b>
        </span>
        <span className="orders-pill">
          <em>跳过 tick</em>
          <b>{engine?.skipped_total ?? 0}</b>
        </span>
        <span className="orders-pill">
          <em>累计信号</em>
          <b>{engine?.signals_total ?? 0}</b>
        </span>
        <span className="orders-pill">
          <em>本地最新 bar</em>
          <b>{stamp(engine?.local_bar_ts)}</b>
        </span>
      </div>
    </section>
  );
}

function ChartPanel() {
  const [bar, setBar] = useState("1H");
  const [data, setData] = useState<CandlesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [settings, setSettings] = useState<ChartSettings>(loadSettings);

  const updateSettings = useCallback((next: ChartSettings) => {
    setSettings(next);
    saveSettings(next);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      try {
        const next = await fetchCandles(controller.signal, bar);
        setData(next);
        setError(null);
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        setError((err as Error).message);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [bar]);

  const empty = data !== null && data.bars.length === 0;
  const staleBars = data?.degraded.includes("candles") ?? false;
  const options = useMemo(() => {
    const available = data?.available_bars ?? [];
    if (available.length === 0) return BAR_OPTIONS;
    return available.includes(bar) ? available : [...available, bar];
  }, [data, bar]);

  return (
    <section className="card chart reveal" style={{ animationDelay: "115ms" }}>
      <div className="card-head">
        <h2>
          {data?.inst ?? "—"} <span className="chart-bar">{data?.bar ?? bar}</span>
        </h2>
        <div className="chart-tools">
          {data && data.fills.length > 0 && (
            <span className="card-note">{data.fills.length} 笔本地成交已标注</span>
          )}
          {data?.position && (
            <span className="card-note">
              持仓 {compactQty(data.position.qty)} @ {num(data.position.avg_price, 2)}
            </span>
          )}
          <div className="seg" role="group" aria-label="K 线周期">
            {options.map((option) => (
              <button
                key={option}
                type="button"
                className={option === bar ? "seg-btn seg-on" : "seg-btn"}
                onClick={() => setBar(option)}
              >
                {option}
              </button>
            ))}
          </div>
        </div>
      </div>
      <IndicatorBar settings={settings} onChange={updateSettings} />
      <div className="chart-box">
        <CandleChart data={data} settings={settings} />
        {empty && (
          <div className="chart-empty">
            {staleBars
              ? "本地无该周期 K 线：先运行 quant sync-candles 同步行情"
              : "暂无 K 线数据"}
          </div>
        )}
        {error && <div className="chart-empty">接口异常：{error}</div>}
      </div>
      <div className="chart-foot">
        <span>数据来源：本地 parquet（{data?.bars.length ?? 0} 根）</span>
        <span>成交点来自 SQLite fills</span>
        <span>滚轮缩放，底部滑块拖动可见区间</span>
      </div>
    </section>
  );
}

function BacktestPanel() {
  const [mode, setMode] = useState<"backtest" | "optimize">("backtest");
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [strategy, setStrategy] = useState("sma_cross");
  const [inst, setInst] = useState("BTC-USDT");
  const [bar, setBar] = useState("1H");
  const [start, setStart] = useState(() => dateOffset(-30));
  const [end, setEnd] = useState(() => dateOffset(0));
  const [warmup, setWarmup] = useState("200");
  const [params, setParams] = useState("");
  const [grid, setGrid] = useState("fast_len=5,10,20;slow_len=20,50,80");
  const [metric, setMetric] = useState("sharpe");
  const [top, setTop] = useState("10");
  const [jobs, setJobs] = useState("1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [backtest, setBacktest] = useState<BacktestResponse | null>(null);
  const [optimize, setOptimize] = useState<OptimizeResponse | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>(() => loadHistory());
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const pending = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchStrategies(controller.signal)
      .then((res) => {
        setStrategies(res.strategies);
        if (res.strategies.length === 0) return;
        // 保持当前选择；配置里没有时才退回第一个
        setStrategy((current) =>
          res.strategies.some((item) => item.id === current)
            ? current
            : res.strategies[0].id,
        );
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => () => pending.current?.abort(), []);

  const current = strategies.find((item) => item.id === strategy);
  const configHint = current ? formatParams(current.params) : "";

  const recordEntry = (result: BacktestResponse) => {
    const entry: HistoryEntry = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      saved_at: Date.now(),
      strategy: result.strategy,
      inst: result.inst,
      bar: result.bar,
      start: result.start,
      end: result.end,
      params: formatParams(result.params),
      bar_count: result.bar_count,
      trade_count: result.trade_count,
      final_equity: result.final_equity,
      metrics: result.metrics,
    };
    setHistory((items) => {
      // 同一配置重跑时替换旧记录，避免历史里堆满重复项
      const rest = items.filter(
        (item) => historySignature(item) !== historySignature(entry),
      );
      const next = [entry, ...rest].slice(0, HISTORY_LIMIT);
      persistHistory(next);
      return next;
    });
  };

  const removeEntry = (id: string) => {
    setHistory((items) => {
      const next = items.filter((item) => item.id !== id);
      persistHistory(next);
      return next;
    });
    setPicked((current) => {
      if (!current.has(id)) return current;
      const next = new Set(current);
      next.delete(id);
      return next;
    });
  };

  const clearHistory = () => {
    setHistory([]);
    setPicked(new Set());
    persistHistory([]);
  };

  const togglePick = (id: string) => {
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else if (next.size < COMPARE_LIMIT) next.add(id);
      return next;
    });
  };

  const submit = async () => {
    const warmupBars = Number(warmup);
    if (!Number.isInteger(warmupBars) || warmupBars < 0) {
      setError("预热根数必须是非负整数");
      return;
    }
    setBusy(true);
    setError(null);
    const controller = new AbortController();
    pending.current = controller;
    const body = {
      strategy,
      inst,
      bar,
      start,
      end,
      params,
      warmup_bars: warmupBars,
    };
    try {
      if (mode === "backtest") {
        setOptimize(null);
        const result = await runBacktest(controller.signal, body);
        setBacktest(result);
        recordEntry(result);
      } else {
        setBacktest(null);
        setOptimize(
          await runOptimize(controller.signal, {
            ...body,
            grid,
            metric,
            top: Number(top) || 10,
            n_jobs: Math.min(Math.max(Number(jobs) || 1, 1), 8),
          }),
        );
      }
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card backtest reveal" style={{ animationDelay: "120ms" }}>
      <div className="card-head">
        <h2>策略回测</h2>
        <div className="chart-tools">
          <span className="card-note">本地 parquet · 只计算不落盘</span>
          <div className="seg" role="group" aria-label="回测模式">
            <button
              type="button"
              className={mode === "backtest" ? "seg-btn seg-on" : "seg-btn"}
              onClick={() => setMode("backtest")}
            >
              单次回测
            </button>
            <button
              type="button"
              className={mode === "optimize" ? "seg-btn seg-on" : "seg-btn"}
              onClick={() => setMode("optimize")}
            >
              参数寻优
            </button>
          </div>
        </div>
      </div>

      <div className="bt-form">
        <label className="bt-field">
          <span>策略</span>
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            {strategies.length === 0 && <option value={strategy}>{strategy}</option>}
            {strategies.map((item) => (
              <option key={item.id} value={item.id}>
                {item.id}
              </option>
            ))}
          </select>
        </label>
        <label className="bt-field">
          <span>品种</span>
          <input value={inst} onChange={(e) => setInst(e.target.value)} />
        </label>
        <label className="bt-field">
          <span>周期</span>
          <select value={bar} onChange={(e) => setBar(e.target.value)}>
            {BAR_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <label className="bt-field">
          <span>开始</span>
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="bt-field">
          <span>结束</span>
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        <label className="bt-field">
          <span>预热根数</span>
          <input
            type="number"
            min={0}
            value={warmup}
            onChange={(e) => setWarmup(e.target.value)}
          />
        </label>
      </div>

      {mode === "backtest" ? (
        <div className="bt-form">
          <label className="bt-field bt-span">
            <span>参数覆盖（留空沿用策略配置）</span>
            <input
              value={params}
              placeholder={configHint || "fast_len=10;slow_len=50"}
              onChange={(e) => setParams(e.target.value)}
            />
          </label>
        </div>
      ) : (
        <div className="bt-form">
          <label className="bt-field bt-span">
            <span>参数网格</span>
            <input
              value={grid}
              placeholder="fast_len=5,10;slow_len=20,50"
              onChange={(e) => setGrid(e.target.value)}
            />
          </label>
          <label className="bt-field">
            <span>排序指标</span>
            <select value={metric} onChange={(e) => setMetric(e.target.value)}>
              {METRIC_SELECT.map((key) => (
                <option key={key} value={key}>
                  {METRIC_LABELS[key]}
                </option>
              ))}
            </select>
          </label>
          <label className="bt-field">
            <span>Top N</span>
            <input
              type="number"
              min={1}
              max={50}
              value={top}
              onChange={(e) => setTop(e.target.value)}
            />
          </label>
          <label className="bt-field">
            <span>并行进程</span>
            <input
              type="number"
              min={1}
              max={8}
              value={jobs}
              onChange={(e) => setJobs(e.target.value)}
            />
          </label>
        </div>
      )}

      <div className="bt-actions">
        <button type="button" className="bt-run" onClick={() => void submit()} disabled={busy}>
          {busy ? "计算中…" : mode === "backtest" ? "运行回测" : "开始寻优"}
        </button>
        <span className="card-note">
          {mode === "backtest"
            ? "区间前仅预热：不成交、不计入指标"
            : "组合上限取配置 backtest.max_combos，非法组合自动跳过"}
        </span>
      </div>

      {error && <div className="bt-error">{error}</div>}

      {mode === "backtest" ? (
        backtest ? (
          <BacktestResult data={backtest} />
        ) : (
          <div className="empty">填好区间后点「运行回测」，指标与权益曲线会显示在这里</div>
        )
      ) : optimize ? (
        <OptimizeResult data={optimize} />
      ) : (
        <div className="empty">填好网格后点「开始寻优」，参数排名会显示在这里</div>
      )}

      {mode === "backtest" && (
        <BacktestHistory
          entries={history}
          picked={picked}
          onToggle={togglePick}
          onRemove={removeEntry}
          onClear={clearHistory}
        />
      )}
    </section>
  );
}

function BacktestResult({ data }: { data: BacktestResponse }) {
  const values = data.equity
    .map((point) => Number(point.equity))
    .filter((value) => Number.isFinite(value));
  return (
    <>
      <div className="bt-sub">
        <span>
          {data.strategy} · {data.inst} · {data.bar} · {data.start} ~ {data.end}
        </span>
        <span>
          {data.bar_count} 根 · 预热 {data.warmup_count} · 期末权益{" "}
          {num(data.final_equity, 2)} · {data.elapsed_ms} ms
        </span>
      </div>
      <div className="metric-grid bt-metrics">
        {METRIC_ORDER.map((key) => (
          <Metric
            key={key}
            label={METRIC_LABELS[key]}
            value={metricText(key, data.metrics[key])}
            hint={METRIC_HINTS[key] ?? ""}
            tone={metricTone(key, data.metrics[key])}
          />
        ))}
      </div>
      <Curve values={values} emptyHint="回测区间内权益数据不足" />
      <div className="bt-sub">
        <span>实际参数 {formatParams(data.params)}</span>
        <span>
          {data.trade_count} 笔成交
          {data.unfilled_signals > 0
            ? ` · ${data.unfilled_signals} 个信号未成交`
            : ""}
        </span>
      </div>
      <Table
        empty={data.trades.length === 0}
        emptyHint="区间内没有成交"
        head={["时间", "方向", "数量", "成交价", "手续费"]}
        rows={data.trades.map((trade) => [
          stamp(trade.ts),
          <span className={trade.side === "buy" ? "positive" : "negative"}>
            {trade.side === "buy" ? "买入" : "卖出"}
          </span>,
          compactQty(trade.qty),
          num(trade.price, 2),
          num(trade.fee, 4),
        ])}
      />
      {data.trades_truncated && (
        <div className="bt-note">
          仅显示最近 {data.trades.length} 笔，区间内共 {data.trade_count} 笔
        </div>
      )}
    </>
  );
}

function BacktestHistory({
  entries,
  picked,
  onToggle,
  onRemove,
  onClear,
}: {
  entries: HistoryEntry[];
  picked: Set<string>;
  onToggle: (id: string) => void;
  onRemove: (id: string) => void;
  onClear: () => void;
}) {
  const runs = entries.filter((entry) => picked.has(entry.id));

  return (
    <div className="bt-history">
      <div className="bt-history-head">
        <h3>历史对比</h3>
        <span className="card-note">
          {entries.length === 0
            ? "每次回测自动保存，最多 20 条"
            : `已存 ${entries.length} 条 · 勾选 2–${COMPARE_LIMIT} 条对比`}
        </span>
        {entries.length > 0 && (
          <button type="button" className="bt-clear" onClick={onClear}>
            清空
          </button>
        )}
      </div>

      {entries.length === 0 ? (
        <div className="empty">运行一次回测后会自动记入历史，刷新页面也不会丢</div>
      ) : (
        <ul className="hist-list">
          {entries.map((entry) => {
            const ret = entry.metrics.total_return;
            return (
              <li
                key={entry.id}
                className={picked.has(entry.id) ? "hist-item hist-on" : "hist-item"}
              >
                <label className="hist-check">
                  <input
                    type="checkbox"
                    checked={picked.has(entry.id)}
                    onChange={() => onToggle(entry.id)}
                  />
                  <span className="hist-main">
                    <span className="hist-title">
                      {entry.strategy} · {entry.inst} · {entry.bar}
                    </span>
                    <span className="hist-range">
                      {entry.start} ~ {entry.end} · {entry.bar_count} 根 ·{" "}
                      {entry.trade_count} 笔
                    </span>
                    <span className="hist-params mono">{entry.params}</span>
                  </span>
                  <span className="hist-metrics">
                    <span
                      className={
                        typeof ret === "number" ? (ret > 0 ? "positive" : ret < 0 ? "negative" : "") : ""
                      }
                    >
                      收益 {metricText("total_return", ret)}
                    </span>
                    <span>回撤 {metricText("max_drawdown", entry.metrics.max_drawdown)}</span>
                    <span>夏普 {metricText("sharpe", entry.metrics.sharpe)}</span>
                  </span>
                  <span className="hist-time mono">{stamp(entry.saved_at)}</span>
                </label>
                <button
                  type="button"
                  className="hist-del"
                  aria-label="删除这条历史"
                  onClick={() => onRemove(entry.id)}
                >
                  ×
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {runs.length >= 2 && <CompareTable runs={runs} />}
    </div>
  );
}

/** 多条回测横向对比：每个指标高亮最优值（回撤取最小，其余取最大）。 */
function CompareTable({ runs }: { runs: HistoryEntry[] }) {
  const head = [
    "指标",
    ...runs.map((run, index) => `#${index + 1} ${clock(run.saved_at).slice(0, 5)}`),
  ];
  const rows: ReactNode[][] = [
    ["区间", ...runs.map((run) => `${run.start} ~ ${run.end}`)],
    ["配置", ...runs.map((run) => `${run.strategy} · ${run.inst} · ${run.bar}`)],
    [
      "参数",
      ...runs.map((run) => <span className="mono">{run.params}</span>),
    ],
  ];

  for (const key of METRIC_ORDER) {
    const best = bestValue(runs, key);
    rows.push([
      METRIC_LABELS[key] ?? key,
      ...runs.map((run) => {
        const value = run.metrics[key];
        const isBest = best !== null && typeof value === "number" && value === best;
        return (
          <span key={run.id} className={isBest ? "bt-best" : "mono"}>
            {metricText(key, value)}
          </span>
        );
      }),
    ]);
  }

  rows.push([
    "期末权益",
    ...runs.map((run) => <span className="mono">{num(run.final_equity, 2)}</span>),
  ]);

  return (
    <>
      <div className="bt-sub">
        <span>横向对比（{runs.length} 条）</span>
        <span>高亮为该指标最优值</span>
      </div>
      <Table head={head} rows={rows} empty={false} emptyHint="" />
    </>
  );
}

function OptimizeResult({ data }: { data: OptimizeResponse }) {
  const names = Object.keys(data.grid);
  const extras = OPTIMIZE_METRICS.filter((key) => key !== data.metric);
  return (
    <>
      <div className="bt-sub">
        <span>
          {data.strategy} · {data.inst} · {data.bar} · {data.start} ~ {data.end}
        </span>
        <span>
          {data.combo_count} 组组合 · 跳过 {data.skipped_count} 组非法参数 ·{" "}
          {data.elapsed_ms} ms
        </span>
      </div>
      <Table
        empty={data.results.length === 0}
        emptyHint="没有可用的参数组合"
        head={[
          "#",
          ...names,
          METRIC_LABELS[data.metric] ?? data.metric,
          ...extras.map((key) => METRIC_LABELS[key] ?? key),
        ]}
        rows={data.results.map((row) => [
          String(row.rank),
          ...names.map((name) => row.params[name] ?? "—"),
          <b className="bt-best">{metricText(data.metric, row.metrics[data.metric])}</b>,
          ...extras.map((key) => metricText(key, row.metrics[key])),
        ])}
      />
      <div className="bt-note">
        按「{METRIC_LABELS[data.metric] ?? data.metric}」
        {data.metric === "max_drawdown" ||
        data.metric === "avg_holding_bars" ||
        data.metric === "max_consecutive_losses" ||
        data.metric === "max_consecutive_loss_amount"
          ? "从小到大"
          : "从大到小"}
        排序
      </div>
    </>
  );
}


function EquityCurve({ points, label }: { points: EquityPoint[]; label: string }) {
  const values = points
    .map((p) => Number(p.total_equity))
    .filter((v) => Number.isFinite(v));

  // 区间最大回撤：相对区间内历史峰值的最大跌幅
  let peak = values[0] ?? 0;
  let drawdown = 0;
  for (const value of values) {
    if (value > peak) peak = value;
    if (peak > 0) drawdown = Math.max(drawdown, (peak - value) / peak);
  }

  return (
    <Curve
      values={values}
      label={label}
      drawdown={drawdown}
      emptyHint="曲线数据不足（monitor 至少运行两轮后才有趋势）"
    />
  );
}

function Curve({
  values,
  emptyHint,
  label,
  drawdown,
}: {
  values: number[];
  emptyHint: string;
  label?: string;
  drawdown?: number;
}) {
  if (values.length < 2) {
    return <div className="curve-empty">{emptyHint}</div>;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const W = 100;
  const H = 32;
  const step = W / (values.length - 1);
  const coords = values.map((v, i) => {
    const x = (i * step).toFixed(2);
    const y = (H - ((v - min) / span) * H).toFixed(2);
    return `${x},${y}`;
  });
  const line = `M${coords.join(" L")}`;
  const first = values[0];
  const last = values[values.length - 1];
  const change = first === 0 ? 0 : (last - first) / first;

  return (
    <div className="curve">
      <svg
        className="curve-svg"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <path className="curve-area" d={`${line} L${W},${H} L0,${H} Z`} />
        <path
          className="curve-line"
          d={line}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <div className="curve-foot">
        <span>
          {label ? `${label} · ` : ""}低 {num(min, 2)}
        </span>
        <span className={change >= 0 ? "positive" : "negative"}>
          {label ? "收益 " : ""}
          {change >= 0 ? "+" : ""}
          {(change * 100).toFixed(2)}%
        </span>
        {drawdown !== undefined && (
          <span className={drawdown > 0.1 ? "negative" : "dim"}>
            回撤 {(drawdown * 100).toFixed(2)}%
          </span>
        )}
        <span>高 {num(max, 2)}</span>
      </div>
    </div>
  );
}

function dateOffset(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() + days);
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function formatParams(params: Record<string, unknown>): string {
  const entries = Object.entries(params);
  if (entries.length === 0) return "—";
  return entries.map(([key, value]) => `${key}=${String(value)}`).join("; ");
}

function metricTone(
  key: string,
  value: number | null | undefined,
): "good" | "warn" | "bad" | "neutral" {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "neutral";
  }
  switch (key) {
    case "total_return":
    case "annual_return":
    case "sharpe":
    case "calmar":
    case "sortino":
      return value > 0 ? "good" : value < 0 ? "bad" : "neutral";
    case "max_drawdown":
      return value < 0.1 ? "good" : value < 0.25 ? "warn" : "bad";
    case "win_rate":
      return value >= 0.5 ? "good" : value >= 0.35 ? "warn" : "bad";
    case "profit_factor":
      return value >= 1.5 ? "good" : value >= 1 ? "warn" : "bad";
    case "max_consecutive_losses":
    case "max_consecutive_loss_amount":
      return value > 0 ? "warn" : "good";
    default:
      return "neutral";
  }
}

function Chip({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: string;
  note?: string;
  tone: Tone;
}) {
  return (
    <div className={`chip chip-${tone}`}>
      <span className="chip-label">{label}</span>
      <span className="chip-value">{value}</span>
      {note && <span className="chip-note">{note}</span>}
    </div>
  );
}

function Metric({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint: string;
  tone: "good" | "warn" | "bad" | "neutral";
}) {
  return (
    <div className={`metric metric-${tone}`}>
      <span className="metric-label">{label}</span>
      <span className="metric-value">{value}</span>
      <span className="metric-hint">{hint}</span>
    </div>
  );
}

function Table({
  head,
  rows,
  empty,
  emptyHint,
  activeIndex = -1,
  onRowClick,
}: {
  head: string[];
  rows: ReactNode[][];
  empty: boolean;
  emptyHint: string;
  activeIndex?: number;
  onRowClick?: (index: number) => void;
}) {
  if (empty) {
    return <div className="empty">{emptyHint}</div>;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {head.map((title) => (
              <th key={title}>{title}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((cells, index) => (
            <tr
              key={index}
              className={
                index === activeIndex
                  ? "row-active"
                  : onRowClick
                    ? "row-clickable"
                    : undefined
              }
              onClick={onRowClick ? () => onRowClick(index) : undefined}
            >
              {cells.map((cell, cellIndex) => (
                <td key={cellIndex}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 成交盈亏汇总：FIFO 回合统计，口径与风控/回测一致（含手续费）。 */
function PnlPanel({ pnl }: { pnl?: PnlResponse }) {
  if (!pnl || pnl.fill_count === 0) {
    return (
      <div className="empty">
        暂无成交，无法统计盈亏。engine 出信号并成交后，这里会显示回合胜率与盈亏比。
      </div>
    );
  }

  const pnlTone =
    sign(pnl.total_pnl) === "positive"
      ? "good"
      : sign(pnl.total_pnl) === "negative"
        ? "bad"
        : "neutral";
  const winRate = pnl.win_rate === null ? null : Number(pnl.win_rate);
  const winsHint =
    pnl.round_trips > 0
      ? `${pnl.wins} 胜 / ${pnl.losses} 负`
      : "尚无已平仓回合";

  return (
    <>
      <div className="metric-grid pnl-grid">
        <Metric
          label="已实现盈亏"
          value={num(pnl.total_pnl, 2)}
          hint={winsHint}
          tone={pnlTone}
        />
        <Metric
          label="胜率"
          value={pnl.win_rate === null ? "—" : pct(pnl.win_rate)}
          hint={`${pnl.round_trips} 个回合`}
          tone={
            winRate === null
              ? "neutral"
              : winRate >= 0.5
                ? "good"
                : winRate >= 0.35
                  ? "warn"
                  : "bad"
          }
        />
        <Metric
          label="盈亏比"
          value={num(pnl.profit_factor, 2)}
          hint="总盈利 / 总亏损"
          tone={
            pnl.profit_factor === null
              ? "neutral"
              : Number(pnl.profit_factor) >= 1.5
                ? "good"
                : Number(pnl.profit_factor) >= 1
                  ? "warn"
                  : "bad"
          }
        />
        <Metric
          label="平均盈利"
          value={num(pnl.avg_win, 2)}
          hint="单回合均值"
          tone="neutral"
        />
        <Metric
          label="平均亏损"
          value={num(pnl.avg_loss, 2)}
          hint="单回合均值"
          tone="neutral"
        />
        <Metric
          label="累计手续费"
          value={num(pnl.fee, 4)}
          hint={`名义成交 ${num(pnl.turnover, 0)}`}
          tone="neutral"
        />
      </div>
      <div className="pnl-foot">
        <span>{pnl.fill_count} 笔成交</span>
        <span>
          最好 {num(pnl.best, 2)} · 最差 {num(pnl.worst, 2)}
        </span>
        <span>
          {stamp(pnl.first_ts)} → {stamp(pnl.last_ts)}
        </span>
        <span>未平仓部分不计入回合</span>
      </div>
    </>
  );
}

const REPORT_METRICS: Record<string, string> = {
  trade_count: "成交笔数",
  qty: "成交量",
  notional: "名义金额",
  fee: "手续费",
  side_sequence: "方向序列",
};

/** 模拟盘日报：每天 08:00 生成的「模拟盘 vs 回测」偏离比对。 */
function ReportsPanel({ reports }: { reports?: ReportsResponse | null }) {
  const [open, setOpen] = useState<string | null>(null);

  if (!reports) {
    return <div className="empty">读取日报中…</div>;
  }
  const items = reports.reports;
  if (items.length === 0) {
    return (
      <div className="empty">
        暂无日报。monitor 每天 08:00 生成一份，且当日需要有模拟盘成交才会产出；
        文件写在 {reports.out_dir}
      </div>
    );
  }

  return (
    <>
      <ul className="rpt-list">
        {items.map((item) => {
          const expanded = open === item.name;
          return (
            <li key={item.name} className={expanded ? "rpt-item rpt-on" : "rpt-item"}>
              <button
                type="button"
                className="rpt-head"
                onClick={() => setOpen(expanded ? null : item.name)}
              >
                <span
                  className={
                    item.status === "PASS" ? "rpt-badge rpt-pass" : "rpt-badge rpt-fail"
                  }
                >
                  {item.status}
                </span>
                <span className="rpt-date mono">{item.date}</span>
                <span className="rpt-config mono">
                  {item.strategy} · {item.inst}
                </span>
                <span className="rpt-dev mono">
                  最大偏离 {(item.max_deviation * 100).toFixed(2)}%
                </span>
                <span className="rpt-time mono">{stamp(item.mtime)}</span>
                <span className="rpt-caret">{expanded ? "−" : "+"}</span>
              </button>
              {expanded && <ReportTable item={item} threshold={reports.threshold} />}
            </li>
          );
        })}
      </ul>
      <div className="rpt-foot">
        <span>{items.length} 份日报</span>
        <span>阈值 {pct(String(reports.threshold))}，超出即 FAIL</span>
        <span>数据来自 reports/daily 下的 CSV</span>
      </div>
    </>
  );
}

function ReportTable({
  item,
  threshold,
}: {
  item: ReportItem;
  threshold: number;
}) {
  const head = ["指标", "模拟盘", "回测", "偏离"];
  const rows = item.rows.map((row) => {
    const side = row.metric === "side_sequence";
    const dev = Number(row.deviation);
    const over = !side && Number.isFinite(dev) && dev > threshold;
    return [
      REPORT_METRICS[row.metric] ?? row.metric,
      <span className="mono">{row.paper}</span>,
      <span className="mono">{row.backtest}</span>,
      <span className={side ? (row.paper === "match" ? "positive" : "negative") : over ? "negative" : "mono"}>
        {side ? (row.paper === "match" ? "一致" : "不一致") : `${(dev * 100).toFixed(2)}%`}
      </span>,
    ];
  });

  return (
    <div className="rpt-table">
      <Table head={head} rows={rows} empty={false} emptyHint="" />
    </div>
  );
}

function QualityPanel({ quality }: { quality?: QualityListResponse | null }) {
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, QualityReport | null>>({});
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  if (!quality) {
    return <div className="empty">读取质量报告中…</div>;
  }
  const items = quality.reports;
  if (items.length === 0) {
    return (
      <div className="empty">
        暂无质量报告。candles 每轮同步后自动生成，也可手动跑 quant check-data
      </div>
    );
  }

  const toggle = (item: QualityItem) => {
    const key = `${item.inst_id}:${item.bar_size}`;
    if (open === key) {
      setOpen(null);
      return;
    }
    setOpen(key);
    if (key in detail) return;
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    fetchQualityReport(next.signal, item.inst_id, item.bar_size)
      .then((res) => {
        setDetail((prev) => ({ ...prev, [key]: res.report }));
      })
      .catch((err) => {
        if ((err as Error).name === "AbortError") return;
        setError((err as Error).message);
      });
  };

  return (
    <>
      <ul className="rpt-list">
        {items.map((item) => {
          const key = `${item.inst_id}:${item.bar_size}`;
          const expanded = open === key;
          const gapsOpen = item.summary.gaps_open ?? 0;
          const gapsFixed = item.summary.gaps_repaired ?? 0;
          const expected = item.summary.expected;
          const present = item.summary.present;
          return (
            <li key={key} className={expanded ? "rpt-item rpt-on" : "rpt-item"}>
              <button
                type="button"
                className="rpt-head"
                onClick={() => toggle(item)}
              >
                <span
                  className={
                    gapsOpen > 0 ? "rpt-badge rpt-fail" : "rpt-badge rpt-pass"
                  }
                >
                  {gapsOpen > 0 ? "GAP" : "OK"}
                </span>
                <span className="rpt-date mono">
                  {item.inst_id} · {item.bar_size}
                </span>
                <span className="rpt-config mono">
                  {gapsOpen > 0 ? `${gapsOpen} 处未修复` : "无未修复缺口"}
                  {gapsFixed > 0 ? ` · ${gapsFixed} 处已补齐` : ""}
                </span>
                <span className="rpt-dev mono">
                  {expected !== undefined && present !== undefined
                    ? `${present}/${expected} 根`
                    : "—"}
                </span>
                <span className="rpt-time mono">{stamp(item.generated_at)}</span>
                <span className="rpt-caret">{expanded ? "−" : "+"}</span>
              </button>
              {expanded && (
                <div className="quality-detail">
                  {key in detail ? (
                    detail[key] === null ? (
                      <div className="empty">报告不存在或已被清理</div>
                    ) : (
                      <QualityDetail report={detail[key] as QualityReport} />
                    )
                  ) : (
                    <div className="empty">读取明细中…</div>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {error && <div className="quality-error">明细读取失败：{error}</div>}
      <div className="rpt-foot">
        <span>{items.length} 个品种 · 缺口不会自动消失</span>
        <span>OPEN 红色 = 至今未补齐</span>
        <span>数据来自 data/quality 下的 latest.json</span>
      </div>
    </>
  );
}

function QualityDetail({ report }: { report: QualityReport }) {
  const gaps = report.gaps ?? [];
  const invalid = report.invalid_ohlc ?? [];
  const missing = Math.max(0, report.summary.expected - report.summary.present);
  return (
    <>
      <div className="quality-range mono">
        校验区间 {stamp(report.checked_range?.[0])} →{" "}
        {stamp(report.checked_range?.[1])} · 缺失 {missing} 根
      </div>
      {gaps.length === 0 ? (
        <div className="empty">窗口内没有缺口</div>
      ) : (
        <ul className="quality-list">
          {gaps.map((gap) => (
            <li
              key={gap.from}
              className={
                gap.status === "open"
                  ? "quality-gap quality-open"
                  : "quality-gap"
              }
            >
              <span className="quality-tag">
                {gap.status === "open" ? "OPEN" : "FIXED"}
              </span>
              <span className="mono">
                {stamp(gap.from)} → {stamp(gap.to)}
              </span>
              <span className="mono">{gap.bars} 根</span>
              {gap.repaired_at ? (
                <span className="quality-fixed mono">
                  {stamp(gap.repaired_at)} 补齐
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {invalid.length > 0 && (
        <>
          <div className="quality-sub">异常 OHLC</div>
          <ul className="quality-list">
            {invalid.map((item) => (
              <li key={item.ts} className="quality-gap quality-open">
                <span className="mono">{stamp(item.ts)}</span>
                <span>{item.reason}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

type AlertGroup = {
  rule: string;
  level: string;
  count: number;
  latest: AlertRow;
  items: AlertRow[];
};

const DEAD_RESULT_TEXT: Record<string, string> = {
  requeued: "已重新入队",
  already_processed: "信号已处理，跳过",
  already_replayed: "已重放过",
  discarded: "已丢弃",
  not_replayable: "解析死信无 Signal，只能核对后丢弃",
  not_found: "记录不存在",
  failed: "入队失败，可重试",
};

/** 死信面板：按阶段筛选、查看原始 payload、单条/批量重放或丢弃（MS4-B §10）。 */
function DeadLettersPanel() {
  const [stage, setStage] = useState("");
  const [pendingOnly, setPendingOnly] = useState(true);
  const [data, setData] = useState<DeadLettersResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);

  const pull = useCallback(async () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    try {
      const res = await fetchDeadLetters(next.signal, {
        stage: stage || undefined,
        pendingOnly,
      });
      setData(res);
      setError(null);
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError((err as Error).message);
    }
  }, [stage, pendingOnly]);

  useEffect(() => {
    void pull();
    const id = window.setInterval(() => void pull(), 30_000);
    return () => {
      controller.current?.abort();
      window.clearInterval(id);
    };
  }, [pull]);

  const token = () => window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? "";

  const act = async (kind: "replay" | "discard", item: DeadLetterItem) => {
    setBusy(item.dead_id);
    setNotice(null);
    try {
      const res =
        kind === "replay"
          ? await replayDeadLetter(item.dead_id, token())
          : await discardDeadLetter(item.dead_id, token());
      const text = DEAD_RESULT_TEXT[res.result] ?? res.result;
      setNotice(`${item.dead_id.slice(0, 8)} · ${text}`);
      await pull();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const replayAll = async () => {
    const ids = (data?.items ?? [])
      .filter((item) => !item.replayed_at && item.stage !== "parse")
      .map((item) => item.dead_id);
    if (ids.length === 0) return;
    setBusy("batch");
    setNotice(null);
    try {
      const res = await replayDeadLetters(ids, token());
      const done = res.results.filter((r) => r.result === "requeued").length;
      setNotice(`批量重放 ${res.results.length} 条，成功入队 ${done} 条`);
      await pull();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const items = data?.items ?? [];
  const pending = items.filter((item) => !item.replayed_at).length;

  return (
    <>
      <div className="card-head">
        <h2>死信</h2>
        <span className="card-note">
          {data?.total === null
            ? "SQLite 不可读"
            : `${data?.total ?? "…"} 条 · 当前 ${pending} 条未处理`}
        </span>
      </div>

      <div className="dl-tools">
        <select
          value={stage}
          onChange={(e) => setStage(e.target.value)}
          aria-label="按阶段筛选死信"
        >
          <option value="">全部阶段</option>
          <option value="parse">parse · 解析失败</option>
          <option value="submit">submit · 下单失败</option>
          <option value="reconcile">reconcile · 对账异常</option>
        </select>
        <label className="dl-check">
          <input
            type="checkbox"
            checked={pendingOnly}
            onChange={(e) => setPendingOnly(e.target.checked)}
          />
          只看未处理
        </label>
        <button
          type="button"
          className="dl-btn"
          onClick={replayAll}
          disabled={busy === "batch" || pending === 0}
        >
          {busy === "batch" ? "重放中…" : "批量重放"}
        </button>
        {(notice || error) && (
          <span className={error ? "dl-msg dl-bad" : "dl-msg"}>
            {error ?? notice}
          </span>
        )}
      </div>

      {items.length === 0 ? (
        <div className="empty">
          {pendingOnly
            ? "没有待处理的死信。信号失败时会自动记录在这里。"
            : "没有死信记录。"}
        </div>
      ) : (
        <ul className="dl-list">
          {items.map((item) => {
            const settled = Boolean(item.replayed_at);
            const expanded = open === item.dead_id;
            return (
              <li
                key={item.dead_id}
                className={settled ? "dl-item dl-done" : "dl-item"}
              >
                <div className="dl-row">
                  <span className={`dl-stage dl-${item.stage}`}>
                    {item.stage}
                  </span>
                  <span className="dl-signal mono">
                    {item.signal_id ?? "—"}
                  </span>
                  <span className="dl-reason" title={item.reason}>
                    {item.reason}
                  </span>
                  <span className="dl-time mono">
                    {stamp(item.created_at)}
                  </span>
                  {settled ? (
                    <span className="dl-result mono">
                      {DEAD_RESULT_TEXT[item.replay_result ?? ""] ??
                        item.replay_result ??
                        "已处理"}
                    </span>
                  ) : (
                    <span className="dl-actions">
                      <button
                        type="button"
                        className="dl-btn"
                        onClick={() => void act("replay", item)}
                        disabled={
                          busy === item.dead_id || item.stage === "parse"
                        }
                        title={
                          item.stage === "parse"
                            ? "解析死信只有原始 body，无法还原成信号"
                            : "放回 pending 队列重新执行"
                        }
                      >
                        重放
                      </button>
                      <button
                        type="button"
                        className="dl-btn dl-ghost"
                        onClick={() => void act("discard", item)}
                        disabled={busy === item.dead_id}
                      >
                        丢弃
                      </button>
                    </span>
                  )}
                  <button
                    type="button"
                    className="dl-caret"
                    onClick={() =>
                      setOpen(expanded ? null : item.dead_id)
                    }
                  >
                    {expanded ? "收起" : "详情"}
                  </button>
                </div>
                {expanded && (
                  <pre className="dl-payload mono">
                    {item.payload}
                    {item.payload_truncated ? "\n…（已截断）" : ""}
                  </pre>
                )}
              </li>
            );
          })}
        </ul>
      )}
      <div className="rpt-foot">
        <span>重放会回到 pending，由 worker 重新走风控与下单</span>
        <span>已 executed/rejected 的信号不会重复下单</span>
        <span>记录来自 SQLite dead_letters</span>
      </div>
    </>
  );
}

/** 按规则聚合的告警面板：级别筛选 + 规则筛选 + 展开查看明细。 */
/** 告警时间：SQLite 权威行用 created_at，Redis 实时缓存行用 ts。 */
function alertTs(alert: AlertRow): number {
  return alert.created_at ?? alert.ts ?? 0;
}

const ALERT_STATE_TEXT: Record<string, string> = {
  open: "未读",
  acked: "已读",
  resolved: "已解决",
  silenced: "静音中",
};

function AlertPanel({
  alerts,
  silences,
  onChanged,
}: {
  alerts: AlertRow[];
  silences: AlertSilence[];
  onChanged: () => Promise<void>;
}) {
  const [level, setLevel] = useState("ALL");
  const [rule, setRule] = useState("ALL");
  const [stateFilter, setStateFilter] = useState("ALL");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // 静音表单：rule 为空表示还没选，避免误把全部规则静音
  const [silenceRule, setSilenceRule] = useState("");
  const [silenceInst, setSilenceInst] = useState("");
  const [silenceHours, setSilenceHours] = useState("4");
  const [silenceReason, setSilenceReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ bad: boolean; text: string } | null>(null);

  const token = window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? "";

  const counts = useMemo<Record<string, number>>(() => {
    const byLevel: Record<string, number> = { ERROR: 0, WARNING: 0, INFO: 0 };
    for (const alert of alerts) {
      const key = alert.level.toUpperCase();
      if (key in byLevel) byLevel[key] += 1;
    }
    return { ...byLevel, ALL: alerts.length };
  }, [alerts]);

  const ruleOptions = useMemo(() => {
    const seen = new Map<string, number>();
    for (const alert of alerts) {
      seen.set(alert.rule, (seen.get(alert.rule) ?? 0) + 1);
    }
    return [...seen.entries()].sort((a, b) => b[1] - a[1]);
  }, [alerts]);

  const groups = useMemo<AlertGroup[]>(() => {
    const buckets = new Map<string, AlertRow[]>();
    for (const alert of alerts) {
      if (level !== "ALL" && alert.level.toUpperCase() !== level) continue;
      if (rule !== "ALL" && alert.rule !== rule) continue;
      // Redis 回落行没有 state 字段，按未读展示
      if (stateFilter !== "ALL" && (alert.state ?? "open") !== stateFilter) continue;
      const bucket = buckets.get(alert.rule);
      if (bucket) bucket.push(alert);
      else buckets.set(alert.rule, [alert]);
    }
    return [...buckets.entries()]
      .map(([name, items]) => ({
        rule: name,
        level: items.reduce(
          (worst, item) =>
            (ALERT_LEVEL_RANK[item.level.toUpperCase()] ?? 0) >
            (ALERT_LEVEL_RANK[worst] ?? 0)
              ? item.level.toUpperCase()
              : worst,
          items[0].level.toUpperCase(),
        ),
        count: items.length,
        latest: items[0],
        items,
      }))
      .sort((a, b) => alertTs(b.latest) - alertTs(a.latest));
  }, [alerts, level, rule, stateFilter]);

  const toggle = (name: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const run = async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    setMsg(null);
    try {
      await action();
      await onChanged();
    } catch (err) {
      setMsg({ bad: true, text: (err as Error).message });
    } finally {
      setBusy(null);
    }
  };

  const handleAck = (alert: AlertRow) => {
    if (!alert.alert_id) {
      setMsg({ bad: true, text: "该行来自 Redis 缓存，缺少 alert_id，无法标记已读" });
      return;
    }
    void run(`ack:${alert.alert_id}`, async () => {
      const result = await ackAlert(alert.alert_id as string, token);
      if (!result.acked) throw new Error("告警不存在或已被清理");
    });
  };

  const handleSilence = () => {
    if (!silenceRule) {
      setMsg({ bad: true, text: "请先选择要静音的规则" });
      return;
    }
    const hours = Number(silenceHours);
    if (!Number.isFinite(hours) || hours <= 0) {
      setMsg({ bad: true, text: "静音时长必须是正数小时" });
      return;
    }
    void run("silence", async () => {
      await silenceAlerts(
        {
          rule: silenceRule,
          inst_id: silenceInst.trim() || null,
          hours,
          reason: silenceReason.trim(),
        },
        token,
      );
      setSilenceReason("");
      setSilenceInst("");
    });
  };

  const handleRevoke = (silenceId: string) => {
    void run(`revoke:${silenceId}`, async () => {
      const result = await revokeSilence(silenceId, token);
      if (!result.revoked) throw new Error("静音记录不存在或已解除");
    });
  };

  return (
    <>
      <div className="alert-tools">
        <div className="alert-levels">
          {ALERT_LEVELS.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`alert-filter ${level === item.key ? "is-active" : ""}`}
              onClick={() => setLevel(item.key)}
            >
              {item.label}
              <b>{counts[item.key] ?? 0}</b>
            </button>
          ))}
        </div>
        <select
          className="alert-rule-select"
          value={rule}
          onChange={(event) => setRule(event.target.value)}
          aria-label="按规则筛选告警"
        >
          <option value="ALL">全部规则（{alerts.length}）</option>
          {ruleOptions.map(([name, count]) => (
            <option key={name} value={name}>
              {name}（{count}）
            </option>
          ))}
        </select>
        <select
          className="alert-rule-select"
          value={stateFilter}
          onChange={(event) => setStateFilter(event.target.value)}
          aria-label="按状态筛选告警"
        >
          <option value="ALL">全部状态</option>
          <option value="open">未读</option>
          <option value="acked">已读</option>
          <option value="resolved">已解决</option>
        </select>
      </div>

      {msg && (
        <p className={`form-msg ${msg.bad ? "form-bad" : "form-good"}`}>
          {msg.text}
        </p>
      )}

      <div className="alert-silence">
        <div className="alert-silence-head">
          <h3>静音</h3>
          <span className="card-note">
            {silences.length > 0
              ? `${silences.length} 条生效中 · 命中期间仍入库，只是不发邮件`
              : "当前无静音"}
          </span>
        </div>
        {silences.length > 0 && (
          <ul className="silence-list">
            {silences.map((item) => (
              <li key={item.silence_id} className="silence-item">
                <span className="mono strong">{item.rule}</span>
                <span className="mono">{item.inst_id ?? "全部品种"}</span>
                <span className="mono">至 {clock(item.until_ts)}</span>
                {item.reason && <span className="silence-reason">{item.reason}</span>}
                <button
                  type="button"
                  className="link-btn"
                  disabled={busy === `revoke:${item.silence_id}`}
                  onClick={() => handleRevoke(item.silence_id)}
                >
                  {busy === `revoke:${item.silence_id}` ? "解除中…" : "解除"}
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="silence-form">
          <select
            className="alert-rule-select"
            value={silenceRule}
            onChange={(event) => setSilenceRule(event.target.value)}
            aria-label="选择要静音的规则"
          >
            <option value="">选择规则…</option>
            {ruleOptions.map(([name]) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <input
            className="text-input"
            value={silenceInst}
            onChange={(event) => setSilenceInst(event.target.value)}
            placeholder="品种（留空=全部）"
            aria-label="静音品种"
          />
          <input
            className="text-input narrow"
            value={silenceHours}
            onChange={(event) => setSilenceHours(event.target.value)}
            placeholder="小时"
            aria-label="静音时长小时"
          />
          <input
            className="text-input"
            value={silenceReason}
            onChange={(event) => setSilenceReason(event.target.value)}
            placeholder="原因（可选）"
            aria-label="静音原因"
          />
          <button
            type="button"
            className="btn-ghost"
            disabled={busy === "silence"}
            onClick={handleSilence}
          >
            {busy === "silence" ? "提交中…" : "静音"}
          </button>
        </div>
      </div>

      {groups.length === 0 ? (
        <div className="empty">
          {alerts.length === 0
            ? "暂无告警。monitor 命中规则后会在此累积。"
            : "当前筛选条件下没有告警。"}
        </div>
      ) : (
        <ul className="alert-list">
          {groups.map((group) => {
            const tone =
              group.level === "ERROR" ? "bad" : group.level === "INFO" ? "ok" : "warn";
            const open = rule !== "ALL" || expanded.has(group.rule);
            return (
              <li key={group.rule} className="alert-group">
                <button
                  type="button"
                  className="alert-group-head"
                  onClick={() => toggle(group.rule)}
                  aria-expanded={open}
                >
                  <span className={`alert-badge alert-badge-${tone}`}>
                    {group.level}
                  </span>
                  <span className="alert-rule">{group.rule}</span>
                  <span className="alert-count">×{group.count}</span>
                  <span className="alert-msg">{group.latest.message}</span>
                  <span className="alert-time">{clock(alertTs(group.latest))}</span>
                  <span className="alert-caret">{open ? "−" : "+"}</span>
                </button>
                {open && (
                  <ul className="alert-items">
                    {group.items.map((alert, index) => (
                      <AlertItem
                        key={`${alert.alert_id ?? alertTs(alert)}-${index}`}
                        alert={alert}
                        busy={busy === `ack:${alert.alert_id ?? ""}`}
                        onAck={handleAck}
                      />
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

function AlertItem({
  alert,
  busy,
  onAck,
}: {
  alert: AlertRow;
  busy: boolean;
  onAck: (alert: AlertRow) => void;
}) {
  const level = alert.level.toLowerCase();
  const tone = level === "error" ? "bad" : level === "info" ? "ok" : "warn";
  const state = alert.state ?? "open";
  const ackable = state === "open" && Boolean(alert.alert_id);
  return (
    <li className={`alert alert-${tone}`}>
      <div className="alert-top">
        <span className="alert-rule">{alert.rule}</span>
        {alert.inst_id && <span className="alert-obj mono">{alert.inst_id}</span>}
        {alert.strategy_id && (
          <span className="alert-obj mono">{alert.strategy_id}</span>
        )}
        <span className={`alert-state alert-state-${state}`}>
          {ALERT_STATE_TEXT[state] ?? state}
        </span>
        <span className="alert-time">{clock(alertTs(alert))}</span>
        {ackable && (
          <button
            type="button"
            className="link-btn"
            disabled={busy}
            onClick={() => onAck(alert)}
          >
            {busy ? "标记中…" : "标记已读"}
          </button>
        )}
      </div>
      <p className="alert-msg">{alert.message}</p>
      {alert.acked_at && (
        <p className="alert-ack-note mono">
          {alert.acked_by ?? "owner"} 于 {clock(alert.acked_at)} 确认
        </p>
      )}
    </li>
  );
}

/** M1-2 持仓摘要：只读，跳转交易执行做实际减仓/平仓。 */
function positionSummaryCells(row: PositionRow): ReactNode[] {
  const qty = Number(row.local_qty ?? row.qty ?? 0);
  const pnl = row.unrealized_pnl ?? row.strategy_unrealized_pnl;
  return [
    <span key="inst" className="mono strong">{row.inst_id}</span>,
    <span key="side" className={qty >= 0 ? "long" : "short"}>
      {qty >= 0 ? "多头" : "空头"}
    </span>,
    <span key="qty" className="mono">{compactQty(row.local_qty ?? row.qty)}</span>,
    <span key="avg" className="mono">{num(row.local_avg_price ?? row.avg_price, 2)}</span>,
    <span key="upl" className={`mono ${sign(pnl)}`}>{num(pnl, 2)}</span>,
    <span key="lev" className="mono">
      {row.leverage ? `${num(row.leverage, 0)}x` : "—"}
    </span>,
  ];
}

function positionCells(row: PositionRow): ReactNode[] {
  return [
    <span key="inst" className="mono strong">{row.inst_id}</span>,
    // 账户列只认交易所快照：本地数量的口径不同（只含策略成交），
    // 拿来顶替会让「0.001 策略仓」看起来像账户全部余额
    <span key="qty" className="mono">{compactQty(row.qty)}</span>,
    <span key="avg" className="mono">{num(row.avg_price, 2)}</span>,
    <span key="strat" className="mono">
      {row.local_qty == null
        ? "—"
        : `${compactQty(row.local_qty)} @ ${num(row.local_avg_price, 2)}`}
    </span>,
    <span key="mark" className="mono">{num(row.mark_price, 2)}</span>,
    <span key="upl" className={`mono ${sign(row.unrealized_pnl)}`}>
      {num(row.unrealized_pnl, 2)}
    </span>,
    <span key="supl" className={`mono ${sign(row.strategy_unrealized_pnl)}`}>
      {num(row.strategy_unrealized_pnl, 2)}
    </span>,
    <span key="lev" className="mono">{row.leverage ? `${num(row.leverage, 0)}x` : "—"}</span>,
    <span key="liq" className="mono">{num(row.liq_price, 2)}</span>,
    <span key="dist" className="mono">{pct(row.liq_distance)}</span>,
  ];
}

function orderCells(row: OrderRow): ReactNode[] {
  return [
    <span key="ts" className="mono dim">{clock(row.created_ts)}</span>,
    <span key="inst" className="mono strong">{row.inst_id ?? "—"}</span>,
    <span key="side" className={row.side === "buy" ? "long" : "short"}>
      {row.side === "buy" ? "买入" : row.side === "sell" ? "卖出" : "—"}
    </span>,
    <span key="type" className="dim">{row.order_type ?? "—"}</span>,
    <span key="qty" className="mono">{compactQty(row.qty)}</span>,
    <span key="px" className="mono">
      {num(row.filled_price ?? row.price, 2)}
    </span>,
    <StatusTag key="status" status={row.status} />,
  ];
}

function fillCells(row: FillRow): ReactNode[] {
  return [
    <span key="ts" className="mono dim">{clock(row.ts)}</span>,
    <span key="inst" className="mono strong">{row.inst_id}</span>,
    <span key="side" className={row.side === "buy" ? "long" : "short"}>
      {row.side === "buy" ? "买入" : "卖出"}
    </span>,
    <span key="qty" className="mono">{compactQty(row.qty)}</span>,
    <span key="px" className="mono">{num(row.price, 2)}</span>,
    <span key="notional" className="mono">{num(row.notional, 2)}</span>,
    <span key="fee" className="mono dim">{num(row.fee, 4)}</span>,
  ];
}

function StatusTag({ status }: { status: string }) {
  const map: Record<string, string> = {
    pending: "tag-warn",
    filled: "tag-ok",
    canceled: "tag-muted",
    rejected: "tag-bad",
  };
  return <span className={`tag ${map[status] ?? "tag-muted"}`}>{status}</span>;
}

/** M1-7 数据新鲜度：区分「引擎落后」与「本地数据本身缺根」。 */
function FreshnessPanel({ freshness }: { freshness: FreshnessResponse | null }) {
  if (!freshness) {
    return <div className="empty">/api/freshness 不可用，无法判断数据是否最新</div>;
  }
  if (freshness.items.length === 0) {
    return <div className="empty">尚未配置引擎消费的品种与周期</div>;
  }
  return (
    <ul className="fresh-list">
      {freshness.items.map((item) => {
        const notes: string[] = [];
        if (
          item.lag_bars !== null &&
          item.lag_bars >= freshness.threshold_bars
        ) {
          notes.push(`engine 落后 ${item.lag_bars} 根`);
        }
        if (
          item.missing_bars !== null &&
          item.missing_bars >= freshness.threshold_bars
        ) {
          notes.push(`本地缺 ${item.missing_bars} 根`);
        }
        return (
          <li
            key={`${item.inst_id}-${item.bar}`}
            className={`fresh-item ${item.stale ? "fresh-bad" : "fresh-ok"}`}
          >
            <div className="fresh-top">
              <span className="mono strong">{item.inst_id}</span>
              <span className="mono">{item.bar}</span>
              <span className="fresh-state">{item.stale ? "落后" : "新鲜"}</span>
            </div>
            <div className="fresh-meta mono">
              <span>
                最新已收盘 {item.last_bar_ts ? clock(item.last_bar_ts) : "—"}
                {item.lag_seconds !== null ? ` · ${age(item.lag_seconds)} 前` : ""}
              </span>
              <span>
                engine 水位{" "}
                {item.engine_watermark_ts ? clock(item.engine_watermark_ts) : "—"}
              </span>
            </div>
            {notes.length > 0 && <p className="fresh-note">{notes.join(" · ")}</p>}
            {item.degraded.length > 0 && (
              <p className="fresh-note">降级：{item.degraded.join(" / ")}</p>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** M1-5 进程健康：判活与 TTL 全在后端，前端只负责呈现。 */
function ProcessGrid({ processes }: { processes: ProcessesResponse | null }) {
  if (!processes) {
    return <div className="empty">/api/processes 不可用，无法判断进程状态</div>;
  }
  return (
    <div className="proc-grid">
      {processes.processes.map((item) => (
        <div
          key={item.name}
          className={`proc-item ${item.online ? "proc-on" : "proc-off"}`}
        >
          <div className="proc-top">
            <span className="proc-dot" />
            <span className="proc-name mono">{item.name}</span>
            <span className="proc-state">{item.online ? "在线" : "离线"}</span>
          </div>
          <span className="proc-meta mono">
            {item.online
              ? item.age_seconds === null
                ? "刚启动"
                : `心跳 ${age(item.age_seconds)} 前`
              : item.reason === "not_managed"
                ? "未托管"
                : "无心跳"}
          </span>
          <span className="proc-meta mono">
            pid {item.pid ?? "—"}
            {item.restarts !== null ? ` · 重启 ${item.restarts}` : ""}
          </span>
        </div>
      ))}
    </div>
  );
}

function heartbeatNote(seconds: number | null | undefined): string {
  return seconds === null || seconds === undefined
    ? "无心跳记录"
    : `心跳 ${age(seconds)} 前`;
}

function sign(value: string | null | undefined): string {
  if (value === null || value === undefined) return "";
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed === 0) return "";
  return parsed > 0 ? "positive" : "negative";
}

function drawdownTone(value: string | null | undefined): "good" | "warn" | "bad" | "neutral" {
  if (value === null || value === undefined) return "neutral";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "neutral";
  if (parsed >= 0.2) return "bad";
  if (parsed >= 0.1) return "warn";
  return "good";
}

function cancelTone(value: string | null | undefined): "good" | "warn" | "bad" | "neutral" {
  if (value === null || value === undefined) return "neutral";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "neutral";
  if (parsed >= 0.5) return "bad";
  if (parsed >= 0.2) return "warn";
  return "good";
}

function liqTone(value: string | null | undefined): "good" | "warn" | "bad" | "neutral" {
  if (value === null || value === undefined) return "neutral";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "neutral";
  if (parsed <= 0.1) return "bad";
  if (parsed <= 0.2) return "warn";
  return "good";
}
