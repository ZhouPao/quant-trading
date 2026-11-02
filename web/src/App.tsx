import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  ackAlert,
  age,
  amendManualOrder,
  cancelManualOrder,
  clock,
  compactQty,
  fetchAll,
  fetchCandles,
  fetchDeadLetters,
  fetchEquity,
  fetchTodayPnl,
  fetchBindings,
  fetchParamChanges,
  fetchStrategyEvaluations,
  rollbackParam,
  fetchQuality,
  fetchQualityReport,
  fetchReconciliations,
  fetchProcesses,
  fetchFreshness,
  fetchTimeline,
  fetchAlertStats,
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
  placeManualOrder,
  reducePosition,
  replayDeadLetter,
  replayDeadLetters,
  resolveReconciliation,
  runOptimize,
  submitBacktest,
  submitBatch,
  fetchBacktestRuns,
  fetchBacktestRun,
  fetchBacktestEquity,
  fetchBacktestTrades,
  fetchBacktestBatch,
  fetchBacktestCompare,
  cancelBacktest,
  rerunBacktest,
  importPine,
  setKillSwitch,
  stamp,
  updateBinding,
  updateStrategyParams,
  type AlertRow,
  type AlertRange,
  type AlertSilence,
  type AlertStatsResponse,
  type BacktestCompareResponse,
  type BacktestEquityPoint,
  type BacktestRun,
  type BacktestRunDetail,
  type BacktestTrade,
  type BatchCellRow,
  type MonthlyReturn,
  type PineCompareResponse,
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
  type TodayPnlResponse,
  type QualityItem,
  type QualityListResponse,
  type QualityReport,
  type ReconciliationRow,
  type ReconciliationsResponse,
  type ReportItem,
  type ReportsResponse,
  type PositionRow,
  type ProcessesResponse,
  type RiskLimitRule,
  type RiskLimitsResponse,
  type RiskOverridesResponse,
  type StrategyBinding,
  type StrategyEvaluation,
  type StrategyInfo,
  type TimelineItem,
  type TimelineResponse,
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
// M9-14 告警趋势的时间范围
const ALERT_TREND_DAYS = [
  { days: 7, label: "7 天" },
  { days: 30, label: "30 天" },
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
  "funding_cost",
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
  funding_cost: "净支出，正=付出 / 负=收取",
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

// M3-16 策略一键回测：详情页把当前组合与策略带进回测面板，
// M3-17 另可带 `runId` 直接打开一条历史结果；
// M3-18 带 `pick` 时把若干参数版本设为对比对象；
// nonce 每次自增，保证重复点击也能重新提交。
type BacktestPreset = {
  strategy: string;
  inst?: string;
  bar?: string;
  runId?: string;
  pick?: string[];
  nonce: number;
};

// M1-6 今日事件流：四类事件与筛选标签
const TIMELINE_KINDS = [
  { key: "signal", label: "信号" },
  { key: "order", label: "下单" },
  { key: "fill", label: "成交" },
  { key: "alert", label: "告警" },
] as const;

// 后端给的 module 是 spec 口径（alerts/strategies/execution），这里换成看板 tab key
const TIMELINE_TABS: Record<string, TabKey> = {
  alerts: "logs",
  strategies: "strategy",
  execution: "execution",
};

/** 事件类型 → 行尾强调色：告警看级别，订单看状态，信号/成交看方向。 */
function timelineTone(item: TimelineItem): string {
  if (item.kind === "alert") {
    return item.severity === "ERROR" ? "negative" : "dim";
  }
  if (item.kind === "order") {
    return item.severity === "filled" ? "positive" : "dim";
  }
  return item.severity === "buy" ? "positive" : "negative";
}

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

// 回测历史改由后端 backtest_runs 持久化（M4-9/M4-10），前端只做展示与筛选
const HISTORY_LIMIT = 20;
const COMPARE_LIMIT = 4;
// M3-17 详情页「最近回测」最多展示几条（数据源仍取 HISTORY_LIMIT 供参数对比）
const RECENT_RUN_LIMIT = 5;
// M3-18 曲线形态：展开分组时最多取几个版本、每条取多少个降采样点
const CURVE_VERSION_LIMIT = 3;
const CURVE_POINTS = 60;
// M3-18 对比表默认只列前 5 名，其余折叠，避免组内版本多时表格过长
const COMPARE_ROW_LIMIT = 5;
// 对比表排序键：点表头切换，名次与首行高亮随之重排
type CompareSort = "total_return" | "max_drawdown" | "sharpe";
// 越小越好的指标，其余按越大越好比较
const LOWER_IS_BETTER = new Set(["max_drawdown"]);
// 交易次数没有优劣之分，不参与高亮
const NO_BEST = new Set(["trade_count"]);

function bestValue(runs: { metrics: MetricMap }[], key: string): number | null {
  if (NO_BEST.has(key)) return null;
  const values = runs
    .map((run) => run.metrics[key])
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  if (values.length < 2) return null;
  return LOWER_IS_BETTER.has(key) ? Math.min(...values) : Math.max(...values);
}

// 任务状态文案：pending/running 会转圈，done/failed/canceled 是终点
const RUN_STATUS_TEXT: Record<string, string> = {
  pending: "排队中",
  running: "运行中",
  done: "已完成",
  failed: "失败",
  canceled: "已取消",
  canceling: "取消中",
};

function isTerminalStatus(status: string): boolean {
  return status === "done" || status === "failed" || status === "canceled";
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
  // M3-16：策略详情页跳回测时带过去的目标策略与组合
  const [backtestPreset, setBacktestPreset] = useState<BacktestPreset | null>(
    null
  );
  // M1-5 进程健康：进程心跳变化比行情慢，15s 一拉即可
  const [processes, setProcesses] = useState<ProcessesResponse | null>(null);
  // M1-7 数据新鲜度与进程健康同频，都是分钟级信号
  const [freshness, setFreshness] = useState<FreshnessResponse | null>(null);
  // M1-6 今日事件流：筛选变化立即重拉，其余时间跟行情同频
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [timelineKinds, setTimelineKinds] = useState<string[]>([]);
  // M9-5 告警时间区间（ms）；null 表示不限
  const [alertRange, setAlertRange] = useState<AlertRange>({
    since: null,
    until: null,
  });
  // M9-14 告警趋势变化慢，单独低频拉取
  const [alertTrend, setAlertTrend] = useState<AlertStatsResponse | null>(null);
  const [alertTrendDays, setAlertTrendDays] = useState(7);
  // M1-8 今日盈亏汇总：按品种拆分，变化频率与持仓相同
  const [todayPnl, setTodayPnl] = useState<TodayPnlResponse | null>(null);
  const timer = useRef<number | null>(null);

  const selectTab = useCallback((next: TabKey) => {
    setTab(next);
    try {
      window.localStorage.setItem(TAB_KEY, next);
    } catch {
      // 隐私模式下不持久化，切换仍然生效
    }
  }, []);

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const bundle = await fetchAll(signal, alertRange);
        setData(bundle);
        setError(null);
        setUpdatedAt(Date.now());
        setPulse((n) => n + 1);
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        setError((err as Error).message);
      }
    },
    [alertRange],
  );

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

  // M1-6 今日事件流：后端一次 UNION 查询，前端只负责筛选与跳转
  useEffect(() => {
    const controller = new AbortController();
    const pull = () => {
      fetchTimeline(controller.signal, timelineKinds)
        .then(setTimeline)
        .catch((err) => {
          if ((err as Error).name === "AbortError") return;
          setTimeline(null);
        });
    };
    pull();
    const id = window.setInterval(pull, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(id);
    };
  }, [timelineKinds]);

  // M9-14 告警趋势：切换 7/30 天立即重拉，其余时间 60s 一轮
  useEffect(() => {
    const controller = new AbortController();
    const pull = () => {
      fetchAlertStats(controller.signal, alertTrendDays)
        .then(setAlertTrend)
        .catch((err) => {
          if ((err as Error).name === "AbortError") return;
          setAlertTrend(null);
        });
    };
    pull();
    const id = window.setInterval(pull, 60_000);
    return () => {
      controller.abort();
      window.clearInterval(id);
    };
  }, [alertTrendDays]);

  // M1-8 今日盈亏汇总：按品种拆分，变化频率与持仓相同
  useEffect(() => {
    const controller = new AbortController();
    const pull = () => {
      fetchTodayPnl(controller.signal)
        .then(setTodayPnl)
        .catch((err) => {
          if ((err as Error).name === "AbortError") return;
          setTodayPnl(null);
        });
    };
    pull();
    const id = window.setInterval(pull, POLL_MS);
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
        ...(timeline?.degraded ?? []),
        ...(alertTrend?.degraded ?? []),
        ...(todayPnl?.degraded ?? []),
      ]),
    [overview, data, risk, equity, reports, quality, timeline, alertTrend, todayPnl],
  );
  const alertStats = useMemo(() => {
    const byLevel: Record<string, number> = {};
    const byRule = new Map<string, number>();
    let unreadCount = 0;
    for (const alert of alerts) {
      const state = alert.state ?? "open";
      // M1-9：只统计未读告警（open 或 silenced）
      if (state === "open" || state === "silenced") {
        unreadCount++;
        const level = alert.level.toUpperCase();
        byLevel[level] = (byLevel[level] ?? 0) + 1;
        byRule.set(alert.rule, (byRule.get(alert.rule) ?? 0) + 1);
      }
    }
    const rules = [...byRule.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
    return {
      total: unreadCount,
      error: byLevel.ERROR ?? 0,
      byLevel,
      rules,
    };
  }, [alerts]);

  const stale =
    overview !== undefined &&
    (overview.snapshot_age_seconds === null || overview.snapshot_age_seconds > 180);
  // 实盘/模拟盘来自配置（settings.okx.demo），与快照无关；拿不到时按未知处理
  const liveMode = overview?.trading_mode === "live";

  return (
    <div className="shell">
      <div className="grain" aria-hidden="true" />
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">Q</span>
          <div className="brand-text">
            <span className="brand-title">QUANT / OPS</span>
            <span className={liveMode ? "brand-sub brand-live" : "brand-sub"}>
              {overview === undefined
                ? "交易终端"
                : liveMode
                  ? "实盘 · 真实资金"
                  : "模拟盘终端"}
            </span>
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
          label="MODE"
          value={
            overview === undefined ? "—" : liveMode ? "实盘" : "模拟盘"
          }
          tone={overview === undefined ? "idle" : liveMode ? "bad" : "idle"}
          note={liveMode ? "真实资金" : "OKX 模拟盘"}
        />
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
      {degraded.size > 0 && (
        <div className="banner banner-warn">
          数据降级：{Array.from(degraded).join("、")}。部分数据可能不完整或延迟。
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
            <span className="card-note">
              {overview?.snapshot_ts
                ? `数据截至 ${clock(overview.snapshot_ts)}`
                : overview?.account
                  ? "快照时间未知"
                  : "等待 monitor"}
            </span>
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
              {overview?.snapshot_ts
                ? `数据截至 ${clock(overview.snapshot_ts)}`
                : overview?.metrics
                  ? "快照时间未知"
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

        <section className="card today-pnl reveal" style={{ animationDelay: "108ms" }}>
          <div className="card-head">
            <h2>今日盈亏</h2>
            <span className="card-note">
              {todayPnl
                ? `${todayPnl.instruments.length} 个品种 · 数据截至 ${clock(todayPnl.ts)}`
                : "等待数据"}
            </span>
          </div>
          {todayPnl && todayPnl.instruments.length > 0 ? (
            <>
              <dl className="kv">
                <div>
                  <dt>已实现</dt>
                  <dd className={sign(todayPnl.total_realized)}>
                    {num(todayPnl.total_realized, 2)}
                  </dd>
                </div>
                <div>
                  <dt>未实现</dt>
                  <dd className={sign(todayPnl.total_unrealized)}>
                    {num(todayPnl.total_unrealized, 2)}
                  </dd>
                </div>
                <div>
                  <dt>手续费</dt>
                  <dd>{num(todayPnl.total_fee, 2)}</dd>
                </div>
                <div>
                  <dt>总盈亏</dt>
                  <dd className={sign(todayPnl.total_pnl)}>
                    {num(todayPnl.total_pnl, 2)}
                  </dd>
                </div>
              </dl>
              <Table
                empty={false}
                emptyHint=""
                head={["品种", "已实现", "未实现", "手续费", "总盈亏"]}
                rows={todayPnl.instruments.map((inst) => [
                  inst.inst_id,
                  <span key="r" className={sign(inst.realized)}>{num(inst.realized, 2)}</span>,
                  <span key="u" className={sign(inst.unrealized)}>{num(inst.unrealized, 2)}</span>,
                  num(inst.fee, 2),
                  <span key="p" className={sign(inst.pnl)}>{num(inst.pnl, 2)}</span>,
                ])}
              />
            </>
          ) : (
            <div className="empty">今日暂无交易</div>
          )}
        </section>

        <section className="card pos-summary reveal" style={{ animationDelay: "105ms" }}>
          <div className="card-head">
            <h2>持仓摘要</h2>
            <span className="card-note">
              {overview?.snapshot_ts
                ? `数据截至 ${clock(overview.snapshot_ts)}`
                : positions.length > 0
                  ? "快照时间未知"
                  : "等待数据"}
            </span>
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
            <span className="card-note">未读 {alertStats.total} 条 · 共 {alerts.length} 条</span>
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

        <section className="card alert-trend reveal" style={{ animationDelay: "116ms" }}>
          <div className="card-head">
            <h2>告警趋势</h2>
            <span className="card-note">
              {alertTrend
                ? `近 ${alertTrend.days} 天 · ${alertTrend.total} 条`
                : "接口不可用"}
            </span>
            <div className="chart-tools">
              <div className="seg" role="group" aria-label="告警趋势时间范围">
                {ALERT_TREND_DAYS.map((item) => (
                  <button
                    key={item.days}
                    type="button"
                    className={`seg-btn ${alertTrendDays === item.days ? "seg-on" : ""}`}
                    onClick={() => setAlertTrendDays(item.days)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <AlertTrendPanel data={alertTrend} />
        </section>

        <section className="card timeline reveal" style={{ animationDelay: "118ms" }}>
          <div className="card-head">
            <h2>今日事件流</h2>
            <span className="card-note">
              {timeline
                ? `${timeline.items.length} 条 · 当日 UTC+8${
                    timeline.degraded.length > 0 ? " · 降级" : ""
                  }`
                : "接口不可用"}
            </span>
          </div>
          <TimelinePanel
            data={timeline}
            kinds={timelineKinds}
            onKinds={setTimelineKinds}
            onJump={(module) => selectTab(TIMELINE_TABS[module] ?? "overview")}
          />
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

        {tab === "strategy" && (
          <EnginePanel
            engine={data?.engine}
            onOpenBacktest={(preset) => {
              setBacktestPreset((prev) => ({
                ...preset,
                nonce: (prev?.nonce ?? 0) + 1,
              }));
              setTab("backtest");
            }}
          />
        )}

        {tab === "market" && <ChartPanel />}

        {tab === "backtest" && (
          <BacktestPanel
            preset={backtestPreset}
            onPresetConsumed={() => setBacktestPreset(null)}
          />
        )}

        {tab === "execution" && (
          <>
        <section className="card positions reveal" style={{ animationDelay: "130ms" }}>
          <div className="card-head">
            <h2>手工下单</h2>
            <span className="card-note">
              市价/限价 · 走风控与审计 · 需 monitor 在线
            </span>
          </div>
          <ManualOrderPanel positions={positions} onChanged={refresh} />
        </section>

        <section className="card positions reveal" style={{ animationDelay: "140ms" }}>
          <PositionsPanel rows={positions} />
        </section>
          </>
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
            range={alertRange}
            onRange={setAlertRange}
            disabledRules={data?.alerts.disabled_rules ?? []}
            onChanged={refresh}
          />
        </section>
        )}

        {tab === "execution" && (
          <>
        <section className="card dead reveal" style={{ animationDelay: "215ms" }}>
          <DeadLettersPanel />
        </section>

        <section className="card dead reveal" style={{ animationDelay: "225ms" }}>
          <ReconcilePanel />
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
            <span className="card-note">SQLite · 倒序 · 挂单可撤 · 限价可改</span>
          </div>
          <OrdersPanel rows={orders} onChanged={refresh} />
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

// 未终结订单：只有这些状态允许撤单/改单（后端会再校验一次）
const OPEN_ORDER_STATUSES = ["pending", "live", "partially_filled"];

function writeToken(): string {
  return window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? "";
}

/** 手工下单：市价/限价开仓，风控减量会在结果里明确提示。 */
function ManualOrderPanel({
  positions,
  onChanged,
}: {
  positions: PositionRow[];
  onChanged: () => Promise<void>;
}) {
  const [instId, setInstId] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [orderType, setOrderType] = useState<"market" | "limit">("market");
  const [qty, setQty] = useState("");
  const [price, setPrice] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const suggestions = Array.from(new Set(positions.map((row) => row.inst_id)));

  const submit = async () => {
    if (!instId.trim()) {
      setError("请填写品种，如 BTC-USDT");
      return;
    }
    if (!(Number(qty) > 0)) {
      setError("数量必须为正数（基础币口径）");
      return;
    }
    if (orderType === "limit" && !(Number(price) > 0)) {
      setError("限价单必须填写价格");
      return;
    }
    if (!reason.trim()) {
      setError("请填写操作原因");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await placeManualOrder(
        {
          instId: instId.trim().toUpperCase(),
          side,
          orderType,
          qty: qty.trim(),
          price: orderType === "limit" ? price.trim() : undefined,
          reason: reason.trim(),
        },
        writeToken()
      );
      const parts: string[] = [];
      if (result.adjusted) {
        parts.push(
          `风控减量 ${compactQty(result.requested_qty)} → ${compactQty(result.submitted_qty)}`
        );
      }
      parts.push(
        `已下单 ${compactQty(result.submitted_qty)}${
          result.contracts ? `（${result.contracts} 张）` : ""
        }`
      );
      parts.push(
        result.fill
          ? `成交 @ ${num(result.fill.price, 2)}，手续费 ${num(result.fill.fee, 4)}`
          : `状态 ${result.status}`
      );
      setNotice(parts.join(" · "));
      setQty("");
      setPrice("");
      setReason("");
      await onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="pos-bar">
        <input
          className="pos-input pos-input-qty"
          value={instId}
          list="manual-inst-options"
          placeholder="品种（如 BTC-USDT）"
          onChange={(e) => setInstId(e.target.value)}
        />
        <datalist id="manual-inst-options">
          {suggestions.map((item) => (
            <option key={item} value={item} />
          ))}
        </datalist>
        <select
          className="pos-input"
          value={side}
          onChange={(e) => setSide(e.target.value === "sell" ? "sell" : "buy")}
        >
          <option value="buy">买入</option>
          <option value="sell">卖出</option>
        </select>
        <select
          className="pos-input"
          value={orderType}
          onChange={(e) =>
            setOrderType(e.target.value === "limit" ? "limit" : "market")
          }
        >
          <option value="market">市价</option>
          <option value="limit">限价</option>
        </select>
        <input
          className="pos-input pos-input-qty"
          value={qty}
          placeholder="数量（基础币）"
          onChange={(e) => setQty(e.target.value)}
        />
        {orderType === "limit" && (
          <input
            className="pos-input"
            value={price}
            placeholder="价格"
            onChange={(e) => setPrice(e.target.value)}
          />
        )}
        <input
          className="pos-input pos-input-reason"
          value={reason}
          placeholder="原因（必填）"
          onChange={(e) => setReason(e.target.value)}
        />
        <button className="pos-danger" onClick={submit} disabled={busy}>
          {busy ? "提交中…" : side === "buy" ? "买入开仓" : "卖出开仓"}
        </button>
      </div>
      {(error || notice) && (
        <div className={error ? "pos-msg pos-msg-bad" : "pos-msg"}>
          {error || notice}
        </div>
      )}
    </>
  );
}

/** 最近订单：挂单可撤，限价单可改价/改量；撤单需二次确认 + 原因。 */
function OrdersPanel({
  rows,
  onChanged,
}: {
  rows: OrderRow[];
  onChanged: () => Promise<void>;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [mode, setMode] = useState<"cancel" | "amend" | null>(null);
  const [qty, setQty] = useState("");
  const [price, setPrice] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const row = rows.find((item) => item.order_id === selected) ?? null;

  const pick = (target: OrderRow, next: "cancel" | "amend") => {
    if (selected === target.order_id && mode === next) {
      setSelected(null);
      setMode(null);
    } else {
      setSelected(target.order_id);
      setMode(next);
      setQty(target.qty ?? "");
      setPrice(target.price ?? "");
    }
    setReason("");
    setError("");
    setNotice("");
  };

  const submit = async () => {
    if (!row || !mode) return;
    if (!reason.trim()) {
      setError("请填写操作原因");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (mode === "cancel") {
        const result = await cancelManualOrder(
          row.order_id,
          reason.trim(),
          writeToken()
        );
        setNotice(
          result.already
            ? `订单已是 ${result.status}，无需再撤`
            : `已撤单 ${row.order_id}`
        );
        setSelected(null);
        setMode(null);
      } else {
        const result = await amendManualOrder(
          row.order_id,
          {
            qty: qty.trim() || undefined,
            price: price.trim() || undefined,
            reason: reason.trim(),
          },
          writeToken()
        );
        setNotice(
          `已改单：${compactQty(result.submitted_qty)} @ ${num(result.price, 2)}`
        );
      }
      setReason("");
      await onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Table
        empty={rows.length === 0}
        emptyHint="暂无订单记录"
        head={["时间", "品种", "方向", "类型", "数量", "价格", "状态", "操作"]}
        rows={rows.map((item) => {
          const open = OPEN_ORDER_STATUSES.includes(item.status);
          return [
            ...orderCells(item),
            <div key="act" className="order-actions">
              {open ? (
                <>
                  <button
                    className={
                      selected === item.order_id && mode === "cancel"
                        ? "pos-chip pos-chip-on"
                        : "pos-chip"
                    }
                    onClick={() => pick(item, "cancel")}
                    disabled={busy}
                  >
                    撤单
                  </button>
                  {item.order_type === "limit" && (
                    <button
                      className={
                        selected === item.order_id && mode === "amend"
                          ? "pos-chip pos-chip-on"
                          : "pos-chip"
                      }
                      onClick={() => pick(item, "amend")}
                      disabled={busy}
                    >
                      改单
                    </button>
                  )}
                </>
              ) : (
                <span className="dim">—</span>
              )}
            </div>,
          ];
        })}
      />

      {row && mode && (
        <div className={mode === "cancel" ? "pos-bar pos-flat-bar" : "pos-bar"}>
          <span className="pos-who">
            {row.inst_id ?? "—"} · {row.order_id} ·{" "}
            {mode === "cancel" ? "撤单后不可恢复" : "仅限价单可改"}
          </span>
          {mode === "amend" && (
            <>
              <input
                className="pos-input pos-input-qty"
                value={qty}
                placeholder="数量（基础币）"
                onChange={(e) => setQty(e.target.value)}
              />
              <input
                className="pos-input"
                value={price}
                placeholder="价格"
                onChange={(e) => setPrice(e.target.value)}
              />
            </>
          )}
          <input
            className="pos-input pos-input-reason"
            value={reason}
            placeholder="原因（必填）"
            onChange={(e) => setReason(e.target.value)}
          />
          <button className="pos-danger" onClick={submit} disabled={busy}>
            {busy ? "提交中…" : mode === "cancel" ? "确认撤单" : "确认改单"}
          </button>
          <button
            className="pos-chip"
            onClick={() => {
              setSelected(null);
              setMode(null);
              setError("");
            }}
            disabled={busy}
          >
            取消
          </button>
        </div>
      )}

      {(error || notice) && (
        <div className={error ? "pos-msg pos-msg-bad" : "pos-msg"}>
          {error || notice}
        </div>
      )}
    </>
  );
}

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

function EnginePanel({
  engine,
  onOpenBacktest,
}: {
  engine?: EngineResponse;
  onOpenBacktest?: (preset: {
    strategy: string;
    inst?: string;
    bar?: string;
    runId?: string;
    pick?: string[];
  }) => void;
}) {
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
  const [rollbackBusy, setRollbackBusy] = useState<number | null>(null);
  const [evaluations, setEvaluations] = useState<StrategyEvaluation[]>([]);
  const [recentRuns, setRecentRuns] = useState<BacktestRun[]>([]);
  // M3-18 曲线形态：只对展开的那一组懒加载，最多取前 3 个版本
  const [curveGroup, setCurveGroup] = useState<string | null>(null);
  const [curves, setCurves] = useState<Record<string, number[] | null>>({});
  const [curveLoading, setCurveLoading] = useState(false);
  // M3-18 对比表：排序键与「展开全部名次」的分组集合
  const [compareSort, setCompareSort] = useState<CompareSort>("total_return");
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(
    () => new Set()
  );
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

  // 变更历史常驻展示；保存或回滚后 notice 变化会触发重新拉取
  useEffect(() => {
    if (!strategyId) return;
    const controller = new AbortController();
    fetchParamChanges(controller.signal, strategyId)
      .then((res) => setChanges(res.changes))
      .catch(() => setChanges([]));
    return () => controller.abort();
  }, [strategyId, notice]);

  // M3-15 最近评估：每轮刷新，保存/回滚后 notice 变化也会立即重拉
  useEffect(() => {
    if (!strategyId) return;
    const controller = new AbortController();
    const load = () => {
      fetchStrategyEvaluations(controller.signal, strategyId)
        .then((res) => setEvaluations(res.items))
        .catch(() => undefined);
    };
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [strategyId, notice]);

  // M3-17 最近回测 / M3-18 参数版本对比共用一份数据：
  // 多取一些，才能在同一区间下凑齐多个参数版本。
  useEffect(() => {
    if (!strategyId) return;
    const controller = new AbortController();
    const load = () => {
      fetchBacktestRuns(controller.signal, {
        strategy: strategyId,
        limit: HISTORY_LIMIT,
      })
        .then((res) => setRecentRuns(res.runs))
        .catch(() => setRecentRuns([]));
    };
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [strategyId, notice]);

  /**
   * M3-18 参数版本对比：只有「同品种 + 同周期 + 同区间」的回测才可比，
   * 因此先按这三项分组，再在组内按参数签名归纳版本（同参数只留最新一次）。
   * 只保留有 ≥2 个参数版本的组。
   */
  const paramGroups = useMemo(() => {
    type VersionMap = Map<string, BacktestRun>;
    const groups = new Map<
      string,
      {
        inst: string;
        bar: string;
        start: string;
        end: string;
        versions: VersionMap;
      }
    >();
    for (const run of recentRuns) {
      if (run.status !== "done") continue;
      const key = `${run.inst}|${run.bar}|${run.start}|${run.end}`;
      const entry = groups.get(key) ?? {
        inst: run.inst,
        bar: run.bar,
        start: run.start,
        end: run.end,
        versions: new Map<string, BacktestRun>(),
      };
      const signature = formatParams(run.params);
      const prev = entry.versions.get(signature);
      if (!prev || (run.created_at ?? 0) > (prev.created_at ?? 0)) {
        entry.versions.set(signature, run);
      }
      groups.set(key, entry);
    }
    return [...groups.entries()]
      .filter(([, group]) => group.versions.size >= 2)
      .map(([key, group]) => ({
        key,
        inst: group.inst,
        bar: group.bar,
        start: group.start,
        end: group.end,
        // 组内按当前排序键从优到劣排列，行号即名次，扫读时不必自己找最优
        versions: sortCompareVersions(
          [...group.versions.entries()].map(([signature, run]) => ({
            signature,
            run,
          })),
          compareSort
        ),
      }));
  }, [recentRuns, compareSort]);

  /**
   * M3-18 曲线形态懒加载：只有展开分组时才逐条取权益曲线，且最多 3 个版本、
   * 每条只取 60 个降采样点，避免一次性打出几十个请求。
   *
   * 用 run_id 串做依赖而不是 paramGroups 对象：轮询每 5 秒都会重建 recentRuns，
   * 若直接依赖数组会重复拉取同一批曲线。
   */
  const curveKey = useMemo(() => {
    const group = curveGroup
      ? paramGroups.find((item) => item.key === curveGroup)
      : null;
    return group
      ? group.versions
          .slice(0, CURVE_VERSION_LIMIT)
          .map((item) => item.run.run_id)
          .join(",")
      : "";
  }, [curveGroup, paramGroups]);

  useEffect(() => {
    const runIds = curveKey ? curveKey.split(",") : [];
    if (runIds.length === 0) {
      setCurves({});
      setCurveLoading(false);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setCurves({});
    setCurveLoading(true);
    Promise.all(
      runIds.map((runId) =>
        fetchBacktestEquity(controller.signal, runId, CURVE_POINTS)
          .then((res) => {
            const values = res.points
              .map((point) => Number(point.equity))
              .filter((value) => Number.isFinite(value));
            return [runId, values] as const;
          })
          // 产物被清理或读取失败时留空，其余版本照常展示
          .catch(() => [runId, null] as const)
      )
    ).then((entries) => {
      if (cancelled) return;
      setCurves(Object.fromEntries(entries));
      setCurveLoading(false);
    });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [curveKey]);

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
            <p className="kill-note">
              保存后写入 strategy_params 并记变更历史，下一轮引擎 tick 生效，
              无需重启。
            </p>
          </div>
        )}
      </div>
      <div className="param-panel">
        <div className="param-head">
          <span className="card-note">
            参数变更历史（最近 {changes.length} 条）· 回滚会追加新记录，不删历史
          </span>
        </div>
        {changes.length === 0 ? (
          <p className="kill-note">暂无变更记录。</p>
        ) : (
          <ul className="param-history">
            {changes.map((row) => (
              <li key={row.id}>
                <span>{clock(row.changed_at)}</span>
                <span>
                  {row.param}：{row.old_value ?? "—"} → {row.new_value}
                </span>
                <span>{row.changed_by}</span>
                {row.reason && <span className="param-reason">{row.reason}</span>}
                <button
                  type="button"
                  className="kill-btn kill-btn-sm"
                  disabled={rollbackBusy === row.id || row.old_value === null}
                  title={
                    row.old_value === null
                      ? "该变更前使用 toml 默认值，没有可恢复的数据库覆盖"
                      : `恢复为 ${row.old_value}`
                  }
                  onClick={async () => {
                    if (!strategyId || row.old_value === null) return;
                    const confirmed = window.confirm(
                      `确定要回滚 ${row.param} 到 ${row.old_value} 吗？`
                    );
                    if (!confirmed) return;
                    setRollbackBusy(row.id);
                    setError("");
                    try {
                      await rollbackParam(
                        strategyId,
                        row.id,
                        {
                          reason: `回滚到 ${stamp(row.changed_at)}`,
                          changed_by: "owner",
                        },
                        token
                      );
                      if (token)
                        window.sessionStorage.setItem(KILL_TOKEN_KEY, token);
                      setNotice(`已回滚 ${row.param} → ${row.old_value}`);
                      await loadInfo();
                      const res = await fetchParamChanges(
                        new AbortController().signal,
                        strategyId
                      );
                      setChanges(res.changes);
                    } catch (e) {
                      setError((e as Error).message);
                    } finally {
                      setRollbackBusy(null);
                    }
                  }}
                >
                  {rollbackBusy === row.id ? "回滚中…" : "回滚"}
                </button>
              </li>
            ))}
          </ul>
        )}
        {error && <div className="kill-msg kill-msg-bad">操作失败：{error}</div>}
        {!error && notice && <div className="kill-msg">{notice}</div>}
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
      <div className="param-panel">
        <div className="param-head">
          <span className="card-note">
            最近 {evaluations.length} 次评估（含无信号轮次）
          </span>
          <button
            type="button"
            className="kill-btn"
            disabled={!strategyId}
            onClick={() =>
              strategyId &&
              onOpenBacktest?.({
                strategy: strategyId,
                inst: engine?.inst,
                bar: engine?.bar,
              })
            }
          >
            用当前参数回测
          </button>
        </div>
        {evaluations.length === 0 ? (
          <p className="kill-note">
            引擎尚未发布评估历史：worker 运行一轮后即出现。
          </p>
        ) : (
          <ul className="param-history">
            {evaluations.map((row, idx) => (
              <li key={`${row.inst_id}-${row.bar_ts ?? "na"}-${idx}`}>
                <span className="mono">{stamp(row.bar_ts)}</span>
                <span className="mono">
                  {row.inst_id} · {row.bar}
                </span>
                <span
                  className={
                    row.status === "signal"
                      ? "tag-ok"
                      : row.status === "error"
                        ? "tag-bad"
                        : "tag-muted"
                  }
                >
                  {ENGINE_STATUS[row.status ?? ""] ?? row.status ?? "—"}
                </span>
                <span>{row.signal_count} 信号</span>
                {row.reason && (
                  <span className="param-reason">{row.reason}</span>
                )}
              </li>
            ))}
          </ul>
        )}
        <p className="kill-note">
          记录来自 engine 每轮心跳，最多保留 10 条；点上方按钮把该策略带入回测面板。
        </p>
      </div>
      <div className="param-panel">
        <div className="param-head">
          <span className="card-note">
            最近 {Math.min(recentRuns.length, RECENT_RUN_LIMIT)} 次回测 ·
            点「查看」载入曲线与成交
          </span>
        </div>
        {recentRuns.length === 0 ? (
          <p className="kill-note">
            该策略还没有回测记录，点上方「用当前参数回测」跑一次。
          </p>
        ) : (
          <ul className="param-history">
            {recentRuns.slice(0, RECENT_RUN_LIMIT).map((row) => (
              <li key={row.run_id}>
                <button
                  type="button"
                  className="kill-btn kill-btn-sm"
                  onClick={() =>
                    onOpenBacktest?.({
                      strategy: row.strategy,
                      inst: row.inst,
                      bar: row.bar,
                      runId: row.run_id,
                    })
                  }
                >
                  查看
                </button>
                <span>{stamp(row.created_at)}</span>
                <span className="mono">
                  {row.inst} · {row.bar} · {row.start} ~ {row.end}
                </span>
                <span
                  className={
                    row.status === "done"
                      ? "tag-ok"
                      : row.status === "failed"
                        ? "tag-bad"
                        : "tag-muted"
                  }
                >
                  {RUN_STATUS_TEXT[row.status] ?? row.status}
                </span>
                {row.status === "done" && (
                  <>
                    <span>收益 {pct(row.metrics.total_return)}</span>
                    <span>回撤 {pct(row.metrics.max_drawdown)}</span>
                    <span>夏普 {num(row.metrics.sharpe)}</span>
                    <span>交易 {num(row.metrics.trade_count, 0)}</span>
                  </>
                )}
                {row.status === "failed" && row.error && (
                  <span className="param-reason">{row.error}</span>
                )}
                <span className="param-reason">
                  {formatParams(row.params)}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="kill-note">
          结果来自 backtest_runs 持久化记录，点「查看」跳到回测页载入该次曲线与成交明细。
        </p>
      </div>
      <div className="param-panel">
        <div className="param-head">
          <span className="card-note">
            参数版本对比 · 同品种/同周期/同区间下不同参数的回测结果
          </span>
        </div>
        {paramGroups.length === 0 ? (
          <p className="kill-note">
            暂无可比数据：需要「同品种 + 同周期 + 同区间」下至少 2 个不同参数的
            已完成回测。改一版参数再回测一次即可出现对比。
          </p>
        ) : (
          <div className="cmp-stack">
            {paramGroups.map((group) => {
              const metricValue = (run: BacktestRun, key: string) => {
                const raw = run.metrics[key];
                return typeof raw === "number" && Number.isFinite(raw)
                  ? raw
                  : null;
              };
              const extreme = (key: string, higherBetter: boolean) => {
                const values = group.versions
                  .map((v) => metricValue(v.run, key))
                  .filter((v): v is number => v !== null);
                if (values.length === 0) return null;
                return higherBetter
                  ? Math.max(...values)
                  : Math.min(...values);
              };
              const bestReturn = extreme("total_return", true);
              const bestDrawdown = extreme("max_drawdown", false);
              const bestSharpe = extreme("sharpe", true);
              const cellClass = (
                value: number | null,
                best: number | null
              ) =>
                value !== null && best !== null && value === best
                  ? "cmp-num cmp-best"
                  : "cmp-num";
              /**
               * 与最优值的差距：箭头表示原始数值方向（↑ 更高 / ↓ 更低），
               * 收益与回撤用百分点（pp），夏普用绝对差。最优值本身不显示。
               */
              const gapText = (
                value: number | null,
                best: number | null,
                unit: "pp" | "ratio"
              ) => {
                if (value === null || best === null || value === best) {
                  return null;
                }
                const diff = Math.abs(value - best);
                const arrow = value > best ? "↑" : "↓";
                return unit === "pp"
                  ? `${arrow}${(diff * 100).toFixed(1)}pp`
                  : `${arrow}${diff.toFixed(2)}`;
              };
              // 曲线共用同一纵轴范围，不同版本的形态才具备可比性
              const shownVersions = group.versions.slice(0, CURVE_VERSION_LIMIT);
              const expanded = expandedGroups.has(group.key);
              const visibleVersions = expanded
                ? group.versions
                : group.versions.slice(0, COMPARE_ROW_LIMIT);
              const loadedSeries = shownVersions
                .map((item) => curves[item.run.run_id])
                .filter(
                  (values): values is number[] =>
                    Array.isArray(values) && values.length > 1
                );
              const flat = loadedSeries.flat();
              const curveDomain =
                flat.length > 0
                  ? { min: Math.min(...flat), max: Math.max(...flat) }
                  : { min: 0, max: 1 };
              return (
                <div key={group.key} className="cmp-group">
                  <div className="cmp-group-head">
                    <span className="cmp-slug">
                      <span className="cmp-inst">{group.inst}</span>
                      <span className="cmp-bar">{group.bar}</span>
                      <span className="cmp-range">
                        {group.start} ~ {group.end}
                      </span>
                    </span>
                    <span className="cmp-actions">
                      <span className="cmp-count">
                        {group.versions.length} 个版本
                      </span>
                      <button
                        type="button"
                        className="cmp-ghost"
                        aria-expanded={curveGroup === group.key}
                        onClick={() =>
                          setCurveGroup((prev) =>
                            prev === group.key ? null : group.key
                          )
                        }
                      >
                        <span
                          className={
                            curveGroup === group.key
                              ? "cmp-caret cmp-caret-open"
                              : "cmp-caret"
                          }
                          aria-hidden="true"
                        >
                          ▸
                        </span>
                        曲线形态
                      </button>
                      <button
                        type="button"
                        className="cmp-primary"
                        onClick={() =>
                          onOpenBacktest?.({
                            strategy: group.versions[0].run.strategy,
                            inst: group.inst,
                            bar: group.bar,
                            pick: group.versions
                              .slice(0, COMPARE_LIMIT)
                              .map((v) => v.run.run_id),
                          })
                        }
                      >
                        在回测页对比 {Math.min(group.versions.length, COMPARE_LIMIT)}{" "}
                        条
                      </button>
                    </span>
                  </div>
                  <div className="cmp-table">
                    <div className="cmp-row cmp-row-head">
                      <span className="cmp-rank" />
                      <span>参数版本</span>
                      {(
                        [
                          ["total_return", "收益", "↑优"],
                          ["max_drawdown", "回撤", "↓优"],
                          ["sharpe", "夏普", "↑优"],
                        ] as const
                      ).map(([key, label, hint]) => (
                        <button
                          key={key}
                          type="button"
                          className={
                            compareSort === key
                              ? "cmp-num cmp-sort cmp-tip cmp-sort-on"
                              : "cmp-num cmp-sort cmp-tip"
                          }
                          aria-pressed={compareSort === key}
                          aria-label={`按${label}排序`}
                          data-tip={
                            compareSort === key
                              ? `当前按${label}排序，最佳版本排最前`
                              : `按${label}从${hint === "↓优" ? "低到高" : "高到低"}排序`
                          }
                          onClick={() => setCompareSort(key)}
                        >
                          {label}
                          <em className="cmp-dir">{hint}</em>
                        </button>
                      ))}
                      <span />
                    </div>
                    {visibleVersions.map(({ signature, run }, index) => {
                      const ret = metricValue(run, "total_return");
                      const dd = metricValue(run, "max_drawdown");
                      const sharpe = metricValue(run, "sharpe");
                      const retGap = gapText(ret, bestReturn, "pp");
                      const ddGap = gapText(dd, bestDrawdown, "pp");
                      const sharpeGap = gapText(sharpe, bestSharpe, "ratio");
                      return (
                        <div
                          key={run.run_id}
                          className={
                            index === 0 ? "cmp-row cmp-row-leader" : "cmp-row"
                          }
                        >
                          <span className="cmp-rank">
                            {String(index + 1).padStart(2, "0")}
                          </span>
                          <span className="cmp-param" title={signature}>
                            {signature}
                          </span>
                          <span className={cellClass(ret, bestReturn)}>
                            {pct(ret)}
                            {retGap && (
                              <em
                                className="cmp-gap"
                                title={`比最优收益低 ${retGap.replace(/[↑↓]/, "")}`}
                              >
                                {retGap}
                              </em>
                            )}
                          </span>
                          <span className={cellClass(dd, bestDrawdown)}>
                            {pct(dd)}
                            {ddGap && (
                              <em
                                className="cmp-gap"
                                title={`比最优回撤深 ${ddGap.replace(/[↑↓]/, "")}`}
                              >
                                {ddGap}
                              </em>
                            )}
                          </span>
                          <span className={cellClass(sharpe, bestSharpe)}>
                            {num(sharpe)}
                            {sharpeGap && (
                              <em
                                className="cmp-gap"
                                title={`与最优夏普相差 ${sharpeGap.replace(/[↑↓]/, "")}`}
                              >
                                {sharpeGap}
                              </em>
                            )}
                          </span>
                          <button
                            type="button"
                            className="cmp-open"
                            onClick={() =>
                              onOpenBacktest?.({
                                strategy: run.strategy,
                                inst: run.inst,
                                bar: run.bar,
                                runId: run.run_id,
                              })
                            }
                          >
                            查看
                          </button>
                        </div>
                      );
                    })}
                    {group.versions.length > COMPARE_ROW_LIMIT && (
                      <button
                        type="button"
                        className="cmp-more cmp-tip cmp-tip-start"
                        aria-expanded={expanded}
                        data-tip={
                          expanded
                            ? `收起，只保留前 ${COMPARE_ROW_LIMIT} 名`
                            : `展开其余 ${
                                group.versions.length - COMPARE_ROW_LIMIT
                              } 个版本（共 ${group.versions.length} 个）`
                        }
                        onClick={() =>
                          setExpandedGroups((prev) => {
                            const next = new Set(prev);
                            if (next.has(group.key)) next.delete(group.key);
                            else next.add(group.key);
                            return next;
                          })
                        }
                      >
                        {expanded
                          ? `收起，只看前 ${COMPARE_ROW_LIMIT} 名`
                          : `展开其余 ${
                              group.versions.length - COMPARE_ROW_LIMIT
                            } 个版本`}
                      </button>
                    )}
                  </div>
                  {curveGroup === group.key && (
                    <div className="cmp-curves">
                      {curveLoading ? (
                        <span className="cmp-curves-note">
                          加载权益曲线（{shownVersions.length} 条）…
                        </span>
                      ) : (
                        <>
                          {shownVersions.map(({ signature, run }, index) => {
                            const series = curves[run.run_id];
                            const usable =
                              Array.isArray(series) && series.length > 1;
                            const change = usable
                              ? series[0] === 0
                                ? 0
                                : (series[series.length - 1] - series[0]) /
                                  series[0]
                              : null;
                            return (
                              <div key={run.run_id} className="cmp-curve">
                                <div className="cmp-curve-head">
                                  <span
                                    className="cmp-curve-name"
                                    title={signature}
                                  >
                                    <em className="cmp-rank">
                                      {String(index + 1).padStart(2, "0")}
                                    </em>
                                    {signature}
                                  </span>
                                  {change !== null && (
                                    <span
                                      className={
                                        change >= 0 ? "positive" : "negative"
                                      }
                                    >
                                      {change >= 0 ? "+" : ""}
                                      {(change * 100).toFixed(1)}%
                                    </span>
                                  )}
                                </div>
                                {usable ? (
                                  <Sparkline
                                    values={series}
                                    domain={curveDomain}
                                  />
                                ) : (
                                  <div className="cmp-curve-empty">
                                    曲线不可用（产物可能已清理）
                                  </div>
                                )}
                              </div>
                            );
                          })}
                          {group.versions.length > CURVE_VERSION_LIMIT && (
                            <span className="cmp-curves-note">
                              仅显示前 {CURVE_VERSION_LIMIT} 个版本，其余版本可用
                              「对比」在回测页查看
                            </span>
                          )}
                        </>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        <p className="kill-note">
          点表头可切换排序键（收益 / 回撤 / 夏普），组内从优到劣排列，序号即名次，
          首行为当前排序下的最优版本；超过 {COMPARE_ROW_LIMIT} 个版本默认折叠。
          收益/回撤/夏普各自的最优值标绿，小字为该版本与最优的差距（收益、回撤为
          pp 百分点，夏普为绝对差），箭头指向原始数值方向。「曲线形态」按需加载前{" "}
          {CURVE_VERSION_LIMIT} 个版本的权益曲线（共用纵轴，可横向比形态）；
          「在回测页对比」把同区间下的参数版本带入叠加曲线。
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

function BacktestPanel({
  preset,
  onPresetConsumed,
}: {
  preset?: BacktestPreset | null;
  onPresetConsumed?: () => void;
}) {
  const [mode, setMode] = useState<"backtest" | "optimize" | "batch">("backtest");
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [strategy, setStrategy] = useState("sma_cross");
  const [inst, setInst] = useState("BTC-USDT");
  const [bar, setBar] = useState("1H");
  const [start, setStart] = useState(() => dateOffset(-30));
  const [end, setEnd] = useState(() => dateOffset(0));
  const [warmup, setWarmup] = useState("200");
  const [params, setParams] = useState("");
  const [grid, setGrid] = useState("fast_len=5,10,20;slow_len=20,50,80");
  // 批量回测（M4-7）：品种/周期都是列表，网格可留空（只跑当前参数）
  const [batchInsts, setBatchInsts] = useState("BTC-USDT,ETH-USDT");
  const [batchBars, setBatchBars] = useState("1H,4H");
  const [batchGrid, setBatchGrid] = useState("");
  const [batchRows, setBatchRows] = useState<BatchCellRow[]>([]);
  const [metric, setMetric] = useState("sharpe");
  const [top, setTop] = useState("10");
  const [jobs, setJobs] = useState("1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // 当前任务：登记行、权益曲线、成交分开取（M4-9 产物按需加载）
  const [run, setRun] = useState<BacktestRunDetail | null>(null);
  const [equity, setEquity] = useState<BacktestEquityPoint[]>([]);
  const [trades, setTrades] = useState<{
    total: number;
    trades: BacktestTrade[];
  } | null>(null);
  const [optimize, setOptimize] = useState<OptimizeResponse | null>(null);
  // 历史改由后端 backtest_runs 持久化（M4-9/M4-10），不再写 localStorage
  const [history, setHistory] = useState<BacktestRun[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [filters, setFilters] = useState({
    strategy: "",
    inst: "",
    bar: "",
    status: "",
  });
  const [historyVersion, setHistoryVersion] = useState(0);
  const [picked, setPicked] = useState<string[]>([]);
  const [compare, setCompare] = useState<BacktestCompareResponse | null>(null);
  const [compareError, setCompareError] = useState<string | null>(null);
  const submitAbort = useRef<AbortController | null>(null);
  const followAbort = useRef<AbortController | null>(null);
  // StrictMode 下 effect 会跑两次，用 nonce 去重避免重复提交同一请求
  const consumedNonce = useRef(0);

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

  // 历史列表按筛选条件重新拉取；任务结束后 historyVersion 自增触发刷新
  useEffect(() => {
    const controller = new AbortController();
    fetchBacktestRuns(controller.signal, { limit: HISTORY_LIMIT, ...filters })
      .then((res) => {
        setHistory(res.runs);
        setHistoryTotal(res.total);
      })
      .catch((err) => {
        if ((err as Error).name === "AbortError") return;
        setError((err as Error).message);
      });
    return () => controller.abort();
  }, [filters, historyVersion]);

  // 勾选 ≥2 条才对比；品种/周期不一致由后端拒绝并给出原因
  useEffect(() => {
    if (picked.length < 2) {
      setCompare(null);
      setCompareError(null);
      return;
    }
    const controller = new AbortController();
    setCompareError(null);
    fetchBacktestCompare(controller.signal, picked)
      .then(setCompare)
      .catch((err) => {
        if ((err as Error).name === "AbortError") return;
        setCompare(null);
        setCompareError((err as Error).message);
      });
    return () => controller.abort();
  }, [picked]);

  useEffect(
    () => () => {
      submitAbort.current?.abort();
      followAbort.current?.abort();
    },
    [],
  );

  const current = strategies.find((item) => item.id === strategy);
  const configHint = current ? formatParams(current.params) : "";

  /** 按任务类型取产物：批量回测读逐格表，其余读权益曲线与成交。 */
  const loadArtifacts = async (
    signal: AbortSignal,
    detail: BacktestRunDetail,
  ) => {
    if (detail.kind === "batch") {
      const res = await fetchBacktestBatch(signal, detail.run_id);
      setBatchRows(res.rows);
      return;
    }
    const [eq, tr] = await Promise.all([
      fetchBacktestEquity(signal, detail.run_id),
      fetchBacktestTrades(signal, detail.run_id),
    ]);
    setEquity(eq.points);
    setTrades(tr);
  };

  /** 轮询任务直到终态；done 后再取权益与成交（产物走单独接口）。 */
  const followRun = async (runId: string) => {
    followAbort.current?.abort();
    const controller = new AbortController();
    followAbort.current = controller;
    setBusy(true);
    setEquity([]);
    setTrades(null);
    setBatchRows([]);
    try {
      for (;;) {
        const detail = await fetchBacktestRun(controller.signal, runId);
        setRun(detail);
        if (isTerminalStatus(detail.status)) {
          if (detail.status === "done") {
            await loadArtifacts(controller.signal, detail);
          }
          break;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
      }
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError((err as Error).message);
    } finally {
      if (!controller.signal.aborted) setBusy(false);
      setHistoryVersion((n) => n + 1);
    }
  };

  /** 打开一条历史结果；未完成的任务只展示状态，不取产物。 */
  const openRun = async (runId: string) => {
    followAbort.current?.abort();
    const controller = new AbortController();
    followAbort.current = controller;
    setBusy(false);
    setError(null);
    setEquity([]);
    setTrades(null);
    setBatchRows([]);
    try {
      const detail = await fetchBacktestRun(controller.signal, runId);
      setRun(detail);
      if (detail.status === "done") {
        await loadArtifacts(controller.signal, detail);
      }
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError((err as Error).message);
    }
  };

  const cancelRun = async (runId: string) => {
    setError(null);
    try {
      const res = await cancelBacktest(
        runId,
        window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? "",
      );
      setNotice(
        res.status === "canceled"
          ? "任务已取消"
          : "已请求取消，将在下一根 bar 退出",
      );
      await followRun(runId);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const rerun = async (runId: string) => {
    setError(null);
    setNotice(null);
    try {
      const res = await rerunBacktest(
        runId,
        window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? "",
      );
      setMode("backtest");
      setNotice(`已按原参数重跑：${res.run_id}`);
      await followRun(res.run_id);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const togglePick = (runId: string) => {
    setPicked((items) => {
      if (items.includes(runId)) return items.filter((id) => id !== runId);
      if (items.length >= COMPARE_LIMIT) return items;
      return [...items, runId];
    });
  };

  /**
   * 提交任务。`override` 供策略详情页的一键回测使用：此时表单状态还没来得及
   * 更新，直接用传入值构造请求体，避免读到上一轮的 strategy/inst。
   */
  const submit = async (override?: {
    strategy?: string;
    inst?: string;
    bar?: string;
    params?: string;
    mode?: "backtest" | "optimize" | "batch";
  }) => {
    const nextMode = override?.mode ?? mode;
    const nextStrategy = override?.strategy ?? strategy;
    const nextInst = override?.inst ?? inst;
    const nextBar = override?.bar ?? bar;
    const nextParams = override?.params ?? params;
    const warmupBars = Number(warmup);
    if (!Number.isInteger(warmupBars) || warmupBars < 0) {
      setError("预热根数必须是非负整数");
      return;
    }
    // 批量回测的品种/周期是逗号分隔的列表，空列表直接拦在前端
    const instList = splitList(batchInsts);
    const barList = splitList(batchBars);
    if (nextMode === "batch" && (instList.length === 0 || barList.length === 0)) {
      setError("批量回测至少要一个品种和一个周期");
      return;
    }
    setError(null);
    setNotice(null);
    const controller = new AbortController();
    submitAbort.current = controller;
    const body = {
      strategy: nextStrategy,
      inst: nextInst,
      bar: nextBar,
      start,
      end,
      params: nextParams,
      warmup_bars: warmupBars,
    };
    try {
      if (nextMode === "batch") {
        setOptimize(null);
        setRun(null);
        setBatchRows([]);
        const { run_id } = await submitBatch(controller.signal, {
          strategy: nextStrategy,
          insts: instList,
          bars: barList,
          start,
          end,
          params: nextParams,
          grid: batchGrid,
          metric,
          warmup_bars: warmupBars,
        });
        setNotice(
          `批量任务已提交（${run_id}）：${instList.length} 品种 × ${barList.length} 周期`,
        );
        await followRun(run_id);
      } else if (nextMode === "backtest") {
        setOptimize(null);
        setRun(null);
        // 提交即返回 run_id（M4-12），页面轮询状态而不是同步等待
        const { run_id } = await submitBacktest(controller.signal, body);
        setNotice(`任务已提交（${run_id}），正在后台计算`);
        await followRun(run_id);
      } else {
        setRun(null);
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

  // M3-16 一键回测：从策略详情页带过来的组合填入表单并立即提交。
  // M3-17 若带 runId，则只载入该次历史结果，不重新提交。
  // 参数留空表示「不额外覆盖」，后端按 toml 默认 + DB 覆盖合并，
  // 即策略详情页展示的当前生效参数。
  useEffect(() => {
    if (!preset) return;
    if (consumedNonce.current === preset.nonce) return;
    consumedNonce.current = preset.nonce;
    setMode("backtest");
    setStrategy(preset.strategy);
    if (preset.inst) setInst(preset.inst);
    if (preset.bar) setBar(preset.bar);
    setParams("");
    setOptimize(null);
    setPicked([]);
    if (preset.runId) {
      void openRun(preset.runId);
    } else if (preset.pick && preset.pick.length >= 2) {
      // M3-18：只把参数版本设为对比对象，不重复提交任务
      setPicked(preset.pick.slice(0, COMPARE_LIMIT));
      setRun(null);
    } else {
      void submit({
        strategy: preset.strategy,
        inst: preset.inst ?? inst,
        bar: preset.bar ?? bar,
        params: "",
        mode: "backtest",
      });
    }
    // 请求已发出，清掉预设，避免下次切回本页时重复提交
    onPresetConsumed?.();
    // 只在详情页发起新请求时触发；submit 依赖的表单值由 override 兜住
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preset?.nonce]);

  return (
    <section className="card backtest reveal" style={{ animationDelay: "120ms" }}>
      <div className="card-head">
        <h2>策略回测</h2>
        <div className="chart-tools">
          <span className="card-note">本地 parquet · 结果落库可回看</span>
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
            <button
              type="button"
              className={mode === "batch" ? "seg-btn seg-on" : "seg-btn"}
              onClick={() => setMode("batch")}
            >
              批量回测
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
        {mode === "batch" ? (
          <>
            <label className="bt-field">
              <span>品种（逗号分隔）</span>
              <input
                value={batchInsts}
                placeholder="BTC-USDT,ETH-USDT"
                onChange={(e) => setBatchInsts(e.target.value)}
              />
            </label>
            <label className="bt-field">
              <span>周期（逗号分隔）</span>
              <input
                value={batchBars}
                placeholder="1H,4H"
                onChange={(e) => setBatchBars(e.target.value)}
              />
            </label>
          </>
        ) : (
          <>
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
          </>
        )}
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
            <span>参数覆盖（留空沿用策略当前生效参数：toml 默认 + DB 覆盖）</span>
            <input
              value={params}
              placeholder={configHint || "fast_len=10;slow_len=50"}
              onChange={(e) => setParams(e.target.value)}
            />
          </label>
        </div>
      ) : mode === "batch" ? (
        <div className="bt-form">
          <label className="bt-field bt-span">
            <span>参数网格（留空只跑当前生效参数）</span>
            <input
              value={batchGrid}
              placeholder="fast_len=5,10;slow_len=20,50"
              onChange={(e) => setBatchGrid(e.target.value)}
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
          {busy
            ? "计算中…"
            : mode === "batch"
              ? "运行批量回测"
              : mode === "backtest"
                ? "运行回测"
                : "开始寻优"}
        </button>
        <span className="card-note">
          {mode === "backtest"
            ? "区间前仅预热：不成交、不计入指标"
            : mode === "batch"
              ? "按品种 × 周期 × 参数逐格回测，单格失败不影响其它格"
              : "组合上限取配置 backtest.max_combos，非法组合自动跳过"}
        </span>
      </div>

      {notice && <div className="bt-notice">{notice}</div>}
      {error && <div className="bt-error">{error}</div>}

      {mode === "batch" ? (
        run ? (
          <BatchResultView
            run={run}
            rows={batchRows}
            onCancel={() => void cancelRun(run.run_id)}
          />
        ) : (
          <div className="empty">
            选好品种、周期与网格后点「运行批量回测」，汇总表会显示在这里
          </div>
        )
      ) : mode === "backtest" ? (
        run ? (
          <BacktestRunView
            run={run}
            equity={equity}
            trades={trades}
            onCancel={() => void cancelRun(run.run_id)}
          />
        ) : (
          <div className="empty">
            填好区间后点「运行回测」，任务先入队，指标与权益曲线随后显示
          </div>
        )
      ) : optimize ? (
        <OptimizeResult data={optimize} />
      ) : (
        <div className="empty">填好网格后点「开始寻优」，参数排名会显示在这里</div>
      )}

      {(mode === "backtest" || mode === "batch") && (
        <BacktestHistory
          runs={history}
          total={historyTotal}
          filters={filters}
          picked={picked}
          activeId={run?.run_id ?? null}
          compare={compare}
          compareError={compareError}
          onFilter={setFilters}
          onToggle={togglePick}
          onOpen={(id) => void openRun(id)}
          onRerun={(id) => void rerun(id)}
          onCancel={(id) => void cancelRun(id)}
        />
      )}
    </section>
  );
}

/** 任务状态条：未完成时给取消入口，失败时显示原因。 */
function RunStatusBar({
  run,
  onCancel,
}: {
  run: BacktestRunDetail;
  onCancel: () => void;
}) {
  return (
    <div className="bt-run-state">
      <span className={`bt-badge bt-badge-${run.status}`}>
        {RUN_STATUS_TEXT[run.status] ?? run.status}
      </span>
      <span className="mono">{run.run_id}</span>
      <span className="dim">提交 {stamp(run.created_at)}</span>
      {run.started_at !== null && (
        <span className="dim">开始 {stamp(run.started_at)}</span>
      )}
      {run.finished_at !== null && (
        <span className="dim">结束 {stamp(run.finished_at)}</span>
      )}
      {!isTerminalStatus(run.status) && (
        <button type="button" className="bt-clear" onClick={onCancel}>
          取消
        </button>
      )}
      {run.error && <span className="negative">{run.error}</span>}
    </div>
  );
}

/** 批量回测结果（M4-7）：成功格按指标排成一张表，失败格单列原因。 */
function BatchResultView({
  run,
  rows,
  onCancel,
}: {
  run: BacktestRunDetail;
  rows: BatchCellRow[];
  onCancel: () => void;
}) {
  const ok = rows.filter((row) => !row.error);
  const failed = rows.filter((row) => row.error);
  const metric = typeof run.params.metric === "string" ? run.params.metric : "sharpe";
  const metricLabel = METRIC_LABELS[metric] ?? metric;
  // 参数列取成功格里出现过的键，不依赖提交时的网格快照
  const paramKeys = Array.from(
    new Set(ok.flatMap((row) => Object.keys(row.params))),
  );
  const extraKeys = ["total_return", "max_drawdown", "trade_count"].filter(
    (key) => key !== metric,
  );
  const toneClass = (value: number | null | undefined) => {
    const tone = metricTone(metric, value);
    return tone === "good" ? "positive" : tone === "bad" ? "negative" : "";
  };
  const head = [
    "#",
    "品种",
    "周期",
    ...paramKeys,
    metricLabel,
    ...extraKeys.map((key) => METRIC_LABELS[key] ?? key),
  ];
  const body = ok.map((row, index) => [
    <span className="dim mono">{index + 1}</span>,
    <span className="mono">{row.inst}</span>,
    row.bar,
    ...paramKeys.map((key) => (
      <span className="mono">{row.params[key] ?? "—"}</span>
    )),
    <b className={toneClass(row.metrics[metric])}>
      {metricText(metric, row.metrics[metric])}
    </b>,
    ...extraKeys.map((key) => (
      <span>{metricText(key, row.metrics[key])}</span>
    )),
  ]);

  return (
    <>
      <RunStatusBar run={run} onCancel={onCancel} />
      {run.status !== "done" ? (
        <div className="empty">
          {run.status === "failed"
            ? "任务失败：见上方原因"
            : "批量回测在后台逐格运行，完成后汇总表会出现在这里"}
        </div>
      ) : (
        <>
          <div className="bt-run-state">
            <span>{run.strategy}</span>
            <span className="dim">
              {run.start} ~ {run.end} · {ok.length + failed.length} 格（失败{" "}
              {failed.length}）· 按{metricLabel}排序
            </span>
          </div>
          <Table
            head={head}
            rows={body}
            empty={ok.length === 0}
            emptyHint="所有格子都失败了，原因见下表"
          />
          {failed.length > 0 && (
            <Table
              head={["品种", "周期", "参数", "失败原因"]}
              rows={failed.map((row) => [
                <span className="mono">{row.inst}</span>,
                row.bar,
                <span className="mono">{formatParams(row.params)}</span>,
                <span className="negative">{row.error}</span>,
              ])}
              empty={false}
              emptyHint=""
            />
          )}
        </>
      )}
    </>
  );
}

/** 回测结果：登记行来自 backtest_runs，曲线与成交按需从产物读取。 */
function BacktestRunView({
  run,
  equity,
  trades,
  onCancel,
}: {
  run: BacktestRunDetail;
  equity: BacktestEquityPoint[];
  trades: { total: number; trades: BacktestTrade[] } | null;
  onCancel: () => void;
}) {
  const values = equity
    .map((point) => Number(point.equity))
    .filter((value) => Number.isFinite(value));
  // monthly_returns 在指标表里是 JSON 列，类型上仍是数值位，需要显式还原
  const monthly = Array.isArray(run.metrics.monthly_returns)
    ? (run.metrics.monthly_returns as unknown as MonthlyReturn[])
    : [];

  return (
    <>
      <RunStatusBar run={run} onCancel={onCancel} />
      {run.status !== "done" ? (
        <div className="empty">
          {run.status === "failed"
            ? "任务失败，原因见上方提示；可点历史里的「重跑」再试一次"
            : "任务已提交，页面每秒轮询状态；完成后自动加载指标、曲线与成交"}
        </div>
      ) : (
        <>
          <div className="bt-sub">
            <span>
              {run.strategy} · {run.inst} · {run.bar} · {run.start} ~ {run.end}
            </span>
            <span className="mono">{formatParams(run.params)}</span>
          </div>
          <div className="metric-grid bt-metrics">
            {METRIC_ORDER.map((key) => (
              <Metric
                key={key}
                label={METRIC_LABELS[key]}
                value={metricText(key, run.metrics[key])}
                hint={METRIC_HINTS[key] ?? ""}
                tone={metricTone(key, run.metrics[key])}
              />
            ))}
          </div>
          <Curve values={values} emptyHint="回测区间内权益数据不足" />
          <MonthlyReturnsTable rows={monthly} />
          <PineComparePanel run={run} />
          <Table
            empty={(trades?.trades.length ?? 0) === 0}
            emptyHint="区间内没有成交"
            head={["时间", "方向", "数量", "成交价", "手续费"]}
            rows={(trades?.trades ?? []).map((trade) => [
              stamp(trade.ts),
              <span className={trade.side === "buy" ? "positive" : "negative"}>
                {trade.side === "buy" ? "买入" : "卖出"}
              </span>,
              compactQty(trade.qty),
              num(trade.price, 2),
              num(trade.fee, 4),
            ])}
          />
          {trades && trades.total > trades.trades.length && (
            <div className="bt-note">
              仅显示最近 {trades.trades.length} 笔，区间内共 {trades.total} 笔
            </div>
          )}
        </>
      )}
    </>
  );
}

/** 月度收益表（M4-14）；首月只作基准，后端不会返回。 */
function MonthlyReturnsTable({ rows }: { rows: MonthlyReturn[] }) {
  if (rows.length === 0) return null;
  return (
    <>
      <div className="bt-sub">
        <span>月度收益</span>
        <span>{rows.length} 个月</span>
      </div>
      <Table
        empty={false}
        emptyHint=""
        head={["月份", "收益率"]}
        rows={rows.map((row) => [
          row.month,
          <span className={row.ret >= 0 ? "positive" : "negative"}>
            {(row.ret * 100).toFixed(2)}%
          </span>,
        ])}
      />
    </>
  );
}

/** Pine 结果导入对比（M4-16）：粘贴 metric,value 两列即逐项算偏离度。 */
function PineComparePanel({ run }: { run: BacktestRunDetail }) {
  const [csv, setCsv] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PineCompareResponse | null>(null);

  const submit = async () => {
    if (!csv.trim()) {
      setError("先粘贴 metric,value 两列内容");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setResult(
        await importPine(
          run.run_id,
          csv,
          window.sessionStorage.getItem(KILL_TOKEN_KEY) ?? "",
        ),
      );
    } catch (err) {
      setResult(null);
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="bt-sub">
        <span>Pine 结果对比</span>
        <span className="dim">
          指标名与内部字段同名；偏离度 &gt; 5% 标红且该参数集禁止启用
        </span>
      </div>
      <textarea
        className="bt-pine-input"
        rows={4}
        spellCheck={false}
        placeholder={"metric,value\ntotal_return,-0.0617\nsharpe,-4.8\nmax_drawdown,0.0921"}
        value={csv}
        onChange={(e) => setCsv(e.target.value)}
      />
      <div className="bt-actions">
        <button
          type="button"
          className="bt-run"
          onClick={() => void submit()}
          disabled={busy}
        >
          {busy ? "对比中…" : "导入并对比"}
        </button>
      </div>
      {error && <div className="bt-error">{error}</div>}
      {result && (
        <>
          <Table
            empty={result.rows.length === 0}
            emptyHint="没有可比对的指标"
            head={["指标", "Pine", "Python", "偏离度"]}
            rows={result.rows.map((row) => [
              METRIC_LABELS[row.metric] ?? row.metric,
              num(row.pine, 4),
              row.python === null ? "—" : num(row.python, 4),
              <span className={row.over_limit ? "negative" : ""}>
                {row.deviation === null
                  ? "无法计算"
                  : `${(row.deviation * 100).toFixed(2)}%`}
              </span>,
            ])}
          />
          <div className="bt-note">
            {result.over_limit
              ? `最大偏离度 ${((result.max_deviation ?? 0) * 100).toFixed(2)}%，超过 5%，禁止启用该参数集`
              : `全部指标在 5% 以内${
                  result.max_deviation === null
                    ? ""
                    : `（最大 ${(result.max_deviation * 100).toFixed(2)}%）`
                }`}
            {result.unmatched.length > 0 &&
              `；未匹配：${result.unmatched.join("、")}`}
            {result.persisted.length > 0 &&
              `；已落库：${result.persisted.join("、")}`}
          </div>
        </>
      )}
    </>
  );
}

function BacktestHistory({
  runs,
  total,
  filters,
  picked,
  activeId,
  compare,
  compareError,
  onFilter,
  onToggle,
  onOpen,
  onRerun,
  onCancel,
}: {
  runs: BacktestRun[];
  total: number;
  filters: { strategy: string; inst: string; bar: string; status: string };
  picked: string[];
  activeId: string | null;
  compare: BacktestCompareResponse | null;
  compareError: string | null;
  onFilter: (next: {
    strategy: string;
    inst: string;
    bar: string;
    status: string;
  }) => void;
  onToggle: (id: string) => void;
  onOpen: (id: string) => void;
  onRerun: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  return (
    <div className="bt-history">
      <div className="bt-history-head">
        <h3>历史回测</h3>
        <span className="card-note">
          共 {total} 条，显示最近 {runs.length} 条 · 勾选 2–{COMPARE_LIMIT} 条对比
        </span>
      </div>

      <div className="bt-form">
        <label className="bt-field">
          <span>策略筛选</span>
          <input
            value={filters.strategy}
            placeholder="全部"
            onChange={(e) => onFilter({ ...filters, strategy: e.target.value })}
          />
        </label>
        <label className="bt-field">
          <span>品种筛选</span>
          <input
            value={filters.inst}
            placeholder="全部"
            onChange={(e) => onFilter({ ...filters, inst: e.target.value })}
          />
        </label>
        <label className="bt-field">
          <span>周期筛选</span>
          <select
            value={filters.bar}
            onChange={(e) => onFilter({ ...filters, bar: e.target.value })}
          >
            <option value="">全部</option>
            {BAR_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <label className="bt-field">
          <span>状态筛选</span>
          <select
            value={filters.status}
            onChange={(e) => onFilter({ ...filters, status: e.target.value })}
          >
            <option value="">全部</option>
            {["pending", "running", "done", "failed", "canceled"].map((item) => (
              <option key={item} value={item}>
                {RUN_STATUS_TEXT[item] ?? item}
              </option>
            ))}
          </select>
        </label>
      </div>

      {runs.length === 0 ? (
        <div className="empty">还没有回测记录；运行一次后结果会落库，刷新页面也不会丢</div>
      ) : (
        <ul className="hist-list">
          {runs.map((item) => {
            const ret = item.metrics.total_return;
            const pending = !isTerminalStatus(item.status);
            return (
              <li
                key={item.run_id}
                className={[
                  "hist-item",
                  picked.includes(item.run_id) ? "hist-on" : "",
                  activeId === item.run_id ? "hist-active" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <label className="hist-check">
                  <input
                    type="checkbox"
                    checked={picked.includes(item.run_id)}
                    onChange={() => onToggle(item.run_id)}
                  />
                  <span className="hist-main">
                    <span className="hist-title">
                      {item.strategy} · {item.inst} · {item.bar}
                    </span>
                    <span className="hist-range">
                      {item.start} ~ {item.end} ·{" "}
                      {RUN_STATUS_TEXT[item.status] ?? item.status}
                      {item.kind === "walkforward"
                        ? " · walk-forward"
                        : item.kind === "batch"
                          ? " · 批量"
                          : ""}
                    </span>
                    <span className="hist-params mono">
                      {formatParams(item.params)}
                    </span>
                  </span>
                  <span className="hist-metrics">
                    <span
                      className={
                        typeof ret === "number"
                          ? ret > 0
                            ? "positive"
                            : ret < 0
                              ? "negative"
                              : ""
                          : ""
                      }
                    >
                      收益 {metricText("total_return", ret)}
                    </span>
                    <span>
                      回撤 {metricText("max_drawdown", item.metrics.max_drawdown)}
                    </span>
                    <span>夏普 {metricText("sharpe", item.metrics.sharpe)}</span>
                  </span>
                  <span className="hist-time mono">{stamp(item.created_at)}</span>
                </label>
                <span className="hist-actions">
                  <button
                    type="button"
                    className="hist-act"
                    onClick={() => onOpen(item.run_id)}
                  >
                    查看
                  </button>
                  <button
                    type="button"
                    className="hist-act"
                    onClick={() => onRerun(item.run_id)}
                  >
                    重跑
                  </button>
                  {pending && (
                    <button
                      type="button"
                      className="hist-act hist-act-warn"
                      onClick={() => onCancel(item.run_id)}
                    >
                      取消
                    </button>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}

      {compareError && <div className="bt-error">{compareError}</div>}
      {compare && compare.runs.length >= 2 && (
        <CompareTable runs={compare.runs} />
      )}
    </div>
  );
}

/** 多条回测横向对比：每个指标高亮最优值（回撤取最小，其余取最大）。 */
function CompareTable({ runs }: { runs: BacktestRun[] }) {
  const head = [
    "指标",
    ...runs.map(
      (run, index) => `#${index + 1} ${clock(run.created_at).slice(0, 5)}`,
    ),
  ];
  const rows: ReactNode[][] = [
    ["区间", ...runs.map((run) => `${run.start} ~ ${run.end}`)],
    ["配置", ...runs.map((run) => `${run.strategy} · ${run.inst} · ${run.bar}`)],
    [
      "参数",
      ...runs.map((run) => (
        <span className="mono">{formatParams(run.params)}</span>
      )),
    ],
    [
      "状态",
      ...runs.map((run) => RUN_STATUS_TEXT[run.status] ?? run.status),
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
          <span key={run.run_id} className={isBest ? "bt-best" : "mono"}>
            {metricText(key, value)}
          </span>
        );
      }),
    ]);
  }

  return (
    <>
      <div className="bt-sub">
        <span>横向对比（{runs.length} 条）</span>
        <span>高亮为该指标最优值；品种与周期由后端校验一致</span>
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

/** 迷你权益曲线：与同组其它版本共用纵轴范围，只比形态不重复标注数值。 */
function Sparkline({
  values,
  domain,
}: {
  values: number[];
  domain: { min: number; max: number };
}) {
  const W = 100;
  const H = 28;
  const span = domain.max - domain.min || 1;
  const step = W / (values.length - 1);
  const line = values
    .map((value, index) => {
      const x = (index * step).toFixed(2);
      const y = (H - ((value - domain.min) / span) * H).toFixed(2);
      return `${x},${y}`;
    })
    .join(" L");

  return (
    <svg
      className="cmp-curve-svg"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <path className="cmp-curve-area" d={`M${line} L${W},${H} L0,${H} Z`} />
      <path
        className="cmp-curve-line"
        d={`M${line}`}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
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
  return entries
    .map(([key, value]) => {
      // walk-forward / 寻优的 params 里带嵌套结构（base、grid），直接 String 会变成 [object Object]
      if (value !== null && typeof value === "object") {
        return `${key}=${JSON.stringify(value)}`;
      }
      return `${key}=${String(value)}`;
    })
    .join("; ");
}

/** 逗号分隔的输入拆成去空白、去重的列表（批量回测的品种/周期）。 */
function splitList(text: string): string[] {
  return Array.from(
    new Set(
      text
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

/**
 * 对比表排序：按当前排序键从优到劣，缺失值排末尾。
 * 回撤越小越好，其余指标越大越好。
 */
function sortCompareVersions(
  versions: { signature: string; run: BacktestRun }[],
  key: CompareSort
) {
  const higherBetter = !LOWER_IS_BETTER.has(key);
  return [...versions].sort((a, b) => {
    const left = a.run.metrics[key];
    const right = b.run.metrics[key];
    const leftValue =
      typeof left === "number" && Number.isFinite(left) ? left : null;
    const rightValue =
      typeof right === "number" && Number.isFinite(right) ? right : null;
    if (leftValue === rightValue) return 0;
    if (leftValue === null) return 1;
    if (rightValue === null) return -1;
    return higherBetter ? rightValue - leftValue : leftValue - rightValue;
  });
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
    case "funding_cost":
      // 正=付出（成本），负=收取（收益）
      return value > 0 ? "warn" : value < 0 ? "good" : "neutral";
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

const RECON_KIND_TEXT: Record<string, string> = {
  order: "订单",
  position: "持仓",
  balance: "余额",
};

/** 三项对账差异（M8-18）：自动修复留痕 + 人工确认待办。 */
function ReconcilePanel() {
  const [kind, setKind] = useState("");
  const [onlyOpen, setOnlyOpen] = useState(true);
  const [data, setData] = useState<ReconciliationsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);

  const pull = useCallback(async () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    try {
      const res = await fetchReconciliations(next.signal, {
        kind: kind || undefined,
        onlyOpen,
      });
      setData(res);
      setError(null);
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError((err as Error).message);
    }
  }, [kind, onlyOpen]);

  useEffect(() => {
    void pull();
    const id = window.setInterval(() => void pull(), 30_000);
    return () => {
      controller.current?.abort();
      window.clearInterval(id);
    };
  }, [pull]);

  const act = async (row: ReconciliationRow) => {
    setBusy(row.rec_id);
    setNotice(null);
    try {
      const res = await resolveReconciliation(row.rec_id, writeToken());
      setNotice(res.result === "resolved" ? "已标记处理" : "该差异已处理过");
      await pull();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const items = data?.items ?? [];
  const pending = items.filter((item) => !item.resolved).length;

  return (
    <>
      <div className="card-head">
        <h2>对账差异</h2>
        <span className="card-note">
          {data?.total === null
            ? "SQLite 不可读"
            : `${data?.total ?? "…"} 条 · 当前 ${pending} 条未处理`}
        </span>
      </div>

      <div className="dl-tools">
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          aria-label="按类型筛选对账差异"
        >
          <option value="">全部类型</option>
          <option value="order">order · 订单</option>
          <option value="position">position · 持仓</option>
          <option value="balance">balance · 余额</option>
        </select>
        <label className="dl-check">
          <input
            type="checkbox"
            checked={onlyOpen}
            onChange={(e) => setOnlyOpen(e.target.checked)}
          />
          只看未处理
        </label>
        {(notice || error) && (
          <span className={error ? "dl-msg dl-bad" : "dl-msg"}>
            {error ?? notice}
          </span>
        )}
      </div>

      {items.length === 0 ? (
        <div className="empty">
          {onlyOpen ? "没有待处理的对账差异。" : "暂无对账记录。"}
        </div>
      ) : (
        <ul className="dl-list">
          {items.map((item) => (
            <li
              key={item.rec_id}
              className={item.resolved ? "dl-item dl-done" : "dl-item"}
            >
              <div className="dl-row">
                <span className={`dl-stage dl-kind-${item.kind}`}>
                  {RECON_KIND_TEXT[item.kind] ?? item.kind}
                </span>
                <span className="dl-signal mono">{item.key}</span>
                <span className="dl-reason mono">
                  本地 {item.local_value ?? "—"} · 交易所{" "}
                  {item.remote_value ?? "—"}
                  {item.diff ? ` · 差 ${item.diff}` : ""}
                </span>
                <span
                  className={
                    item.severity === "manual"
                      ? "recon-tag recon-manual"
                      : "recon-tag"
                  }
                >
                  {item.severity === "manual" ? "待确认" : "已自动修复"}
                </span>
                <span className="dl-time mono">{stamp(item.ts)}</span>
                {item.resolved ? (
                  <span className="dl-result mono">
                    {item.resolved_by === "auto" ? "自动" : "已处理"}
                  </span>
                ) : (
                  <span className="dl-actions">
                    <button
                      type="button"
                      className="dl-btn"
                      onClick={() => void act(item)}
                      disabled={busy === item.rec_id}
                    >
                      {busy === item.rec_id ? "处理中…" : "标记已处理"}
                    </button>
                  </span>
                )}
              </div>
              {item.note ? <div className="recon-note">{item.note}</div> : null}
            </li>
          ))}
        </ul>
      )}
      <div className="rpt-foot">
        <span>订单/持仓/余额三类对比，以交易所为准</span>
        <span>本地多记自动下调；少记与余额差异需人工确认</span>
        <span>记录来自 SQLite reconciliations</span>
      </div>
    </>
  );
}

/** M9-14 告警趋势：各规则触发次数，条形按级别拆色。 */
function AlertTrendPanel({ data }: { data: AlertStatsResponse | null }) {
  if (!data) {
    return <div className="empty">/api/alerts/stats 不可用，趋势暂时读不到</div>;
  }
  if (data.rules.length === 0) {
    return <div className="empty">区间内没有告警</div>;
  }
  // 条长按最大值归一，避免一条 300 次的规则把其余规则压成一条线
  const max = Math.max(...data.rules.map((item) => item.total), 1);
  return (
    <ul className="trend-list">
      {data.rules.map((item) => (
        <li key={item.rule} className="trend-row">
          <span className="trend-name mono">{item.rule}</span>
          <span className="trend-bar">
            <span
              className="trend-fill trend-bad"
              style={{ width: `${(item.errors / max) * 100}%` }}
            />
            <span
              className="trend-fill trend-warn"
              style={{ width: `${(item.warnings / max) * 100}%` }}
            />
            <span
              className="trend-fill trend-ok"
              style={{ width: `${(item.infos / max) * 100}%` }}
            />
          </span>
          <span className="trend-count mono">{item.total}</span>
          <span className="trend-split mono dim">
            {item.errors}E / {item.warnings}W / {item.infos}I
          </span>
        </li>
      ))}
    </ul>
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

/** 静音表单里代表「全局」的选项值；提交时转成空 rule（M9-10）。 */
const SILENCE_GLOBAL = "__global__";

/** ms → `YYYY-MM-DDTHH:mm`（本地时区），供 datetime-local 回显。 */
function toLocalInput(ts: number | null): string {
  if (ts === null) return "";
  const date = new Date(ts);
  const pad = (value: number) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

function AlertPanel({
  alerts,
  silences,
  range,
  onRange,
  disabledRules,
  onChanged,
}: {
  alerts: AlertRow[];
  silences: AlertSilence[];
  range: AlertRange;
  onRange: (next: AlertRange) => void;
  disabledRules: string[];
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
    const global = silenceRule === SILENCE_GLOBAL;
    const hours = Number(silenceHours);
    if (!Number.isFinite(hours) || hours <= 0) {
      setMsg({ bad: true, text: "静音时长必须是正数小时" });
      return;
    }
    void run("silence", async () => {
      await silenceAlerts(
        {
          // 全局静音传空 rule：任意规则、任意品种都命中（M9-10）
          rule: global ? "" : silenceRule,
          inst_id: global ? null : silenceInst.trim() || null,
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
          <option value="silenced">静音中</option>
          <option value="acked">已读</option>
          <option value="resolved">已解决</option>
        </select>
        <label className="alert-range">
          <span className="dim">从</span>
          <input
            type="datetime-local"
            className="text-input"
            value={toLocalInput(range.since)}
            onChange={(event) =>
              onRange({
                ...range,
                since: event.target.value
                  ? new Date(event.target.value).getTime()
                  : null,
              })
            }
            aria-label="告警起始时间"
          />
          <span className="dim">到</span>
          <input
            type="datetime-local"
            className="text-input"
            value={toLocalInput(range.until)}
            onChange={(event) =>
              onRange({
                ...range,
                until: event.target.value
                  ? new Date(event.target.value).getTime()
                  : null,
              })
            }
            aria-label="告警结束时间"
          />
          {(range.since !== null || range.until !== null) && (
            <button
              type="button"
              className="link-btn"
              onClick={() => onRange({ since: null, until: null })}
            >
              清除区间
            </button>
          )}
        </label>
      </div>

      {disabledRules.length > 0 && (
        <p className="alert-disabled">
          规则级启停：{disabledRules.join(" / ")} 已停用，不会产生新告警
        </p>
      )}

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
                <span className="mono strong">
                  {item.rule === "*" ? "全局（全部规则）" : item.rule}
                </span>
                <span className="mono">
                  {item.rule === "*" ? "全部品种" : item.inst_id ?? "全部品种"}
                </span>
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
            <option value={SILENCE_GLOBAL}>全部规则（全局）</option>
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
  const ackable = (state === "open" || state === "silenced") && Boolean(alert.alert_id);
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

/** M1-6 今日事件流：四表合并倒序，可按类型筛选，点击跳到对应模块。 */
function TimelinePanel({
  data,
  kinds,
  onKinds,
  onJump,
}: {
  data: TimelineResponse | null;
  kinds: string[];
  onKinds: (next: string[]) => void;
  onJump: (module: string) => void;
}) {
  if (!data) {
    return <div className="empty">/api/timeline 不可用，今日事件流暂时读不到</div>;
  }
  const toggle = (key: string) => {
    onKinds(
      kinds.includes(key)
        ? kinds.filter((item) => item !== key)
        : [...kinds, key],
    );
  };
  return (
    <>
      <div className="tl-filter">
        <button
          type="button"
          className={kinds.length === 0 ? "tl-chip tl-chip-on" : "tl-chip"}
          onClick={() => onKinds([])}
        >
          全部
        </button>
        {TIMELINE_KINDS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={kinds.includes(item.key) ? "tl-chip tl-chip-on" : "tl-chip"}
            onClick={() => toggle(item.key)}
          >
            {item.label}
          </button>
        ))}
        {data.degraded.length > 0 && (
          <span className="tl-degraded">降级：{data.degraded.join(" / ")}</span>
        )}
      </div>
      {data.items.length === 0 ? (
        <div className="empty">
          {kinds.length === 0
            ? "当日还没有信号、下单、成交或告警"
            : "当前筛选下没有事件"}
        </div>
      ) : (
        <ul className="tl-list">
          {data.items.map((item) => (
            <li key={`${item.kind}-${item.ref_id}-${item.ts}`}>
              <button
                type="button"
                className="tl-row"
                onClick={() => onJump(item.module)}
                title="点击跳到对应模块"
              >
                <span className="tl-time mono">{clock(item.ts)}</span>
                <span className="tl-title">{item.title}</span>
                <span className="tl-detail mono">{item.detail}</span>
                <span className={`tl-sev mono ${timelineTone(item)}`}>
                  {item.severity ?? item.kind}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  );
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
          {item.ws && (
            <span
              className={`proc-meta mono proc-ws${item.ws.connected ? "" : " proc-ws-off"}`}
              title={item.ws.last_error ?? undefined}
            >
              {item.ws.connected
                ? "WS 已连接"
                : `WS 断开 · 连续失败 ${item.ws.failures}${
                    item.ws.next_delay !== null
                      ? ` · ${item.ws.next_delay.toFixed(0)}s 后重连`
                      : ""
                  }`}
            </span>
          )}
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
