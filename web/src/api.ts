export type Heartbeat = {
  online: boolean;
  last_ts: number | null;
  age_seconds: number | null;
};

export type Overview = {
  ts: number;
  snapshot_ts: number | null;
  snapshot_age_seconds: number | null;
  account: {
    total_equity: string;
    avail_usdt: string;
    frozen_usdt: string;
    unrealized_pnl: string;
    margin_ratio: string | null;
  } | null;
  metrics: {
    max_drawdown: string;
    cancel_rate: string | null;
    min_liq_distance: string | null;
  } | null;
  positions_count: number | null;
  kill_switch: {
    active: boolean;
    reason: string | null;
    auth_required: boolean;
    known: boolean;
  };
  worker: Heartbeat;
  monitor: Heartbeat;
  queue: { pending: number | null; inflight: number | null; dead: number | null };
  orders: Record<string, number>;
  degraded: string[];
};

export type EngineResponse = {
  ts: number;
  strategy: string;
  inst: string;
  bar: string;
  online: boolean;
  status_age_seconds: number | null;
  timeout_seconds: number;
  status: string | null;
  reason: string | null;
  last_bar_ts: number | null;
  last_bar_age_seconds: number | null;
  watermark_ts: number | null;
  watermark_age_seconds: number | null;
  local_bar_ts: number | null;
  local_bar_age_seconds: number | null;
  lag_bars: number | null;
  catch_up: number | null;
  catch_up_total: number | null;
  skipped_total: number | null;
  signals_total: number | null;
  ticks: number | null;
  started_ts: number | null;
  max_catchup_bars: number;
  degraded: string[];
};

export type PositionRow = {
  inst_id: string;
  local_qty: string | null;
  local_avg_price: string | null;
  realized_pnl: string | null;
  /** 策略持仓的浮动盈亏：(标记价 - 本地均价) × 本地数量 */
  strategy_unrealized_pnl: string | null;
  qty: string | null;
  avg_price: string | null;
  mark_price: string | null;
  unrealized_pnl: string | null;
  leverage: string | null;
  liq_price: string | null;
  liq_distance: string | null;
};

export type PositionsResponse = {
  ts: number;
  positions: PositionRow[];
  degraded: string[];
};

export type ManualFill = {
  qty: string;
  price: string;
  fee: string;
  ts: number;
};

export type ReduceResult = {
  inst_id: string;
  inst_type: "SPOT" | "SWAP" | "FUTURES";
  ok: boolean;
  ts: number;
  available_qty?: string;
  submitted_qty?: string;
  contracts?: string | null;
  order_id?: string;
  status?: string;
  fill?: ManualFill | null;
  error?: string | null;
};

export type FlattenResult = {
  ok: boolean;
  ts: number;
  results: ReduceResult[];
  total: number;
  succeeded: number;
  failed: number;
};

async function postWrite<T>(path: string, body: unknown, token = ""): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Dashboard-Request": "1",
      ...(token ? { "X-Dashboard-Token": token } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      // 非 JSON 响应，保留状态码即可
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

/** 手动减仓：ratio 与 qty 二选一。 */
export async function reducePosition(
  instId: string,
  input: { ratio?: number; qty?: string; reason: string },
  token = ""
): Promise<ReduceResult> {
  return postWrite<ReduceResult>(
    "/api/positions/reduce",
    { inst_id: instId, ...input },
    token
  );
}

/** 一键全平：confirm 必须是 FLATTEN。 */
export async function flattenPositions(
  reason: string,
  token = ""
): Promise<FlattenResult> {
  return postWrite<FlattenResult>(
    "/api/positions/flatten",
    { reason, confirm: "FLATTEN" },
    token
  );
}

export type OrderRow = {
  order_id: string;
  status: string;
  created_ts: number;
  filled_ts: number | null;
  filled_price: string | null;
  filled_qty: string | null;
  fee: string | null;
  strategy_id: string | null;
  inst_id: string | null;
  side: string | null;
  order_type: string | null;
  qty: string | null;
  price: string | null;
};

export type OrdersResponse = {
  ts: number;
  orders: OrderRow[];
  degraded: string[];
};

export type FillRow = {
  order_id: string;
  inst_id: string;
  side: string;
  qty: string;
  price: string;
  fee: string;
  notional: string;
  ts: number;
  age_seconds: number | null;
};

export type FillsResponse = {
  ts: number;
  fills: FillRow[];
  total_fee: string;
  degraded: string[];
};

export type AlertRow = {
  rule: string;
  level: string;
  message: string;
  ts: number;
};

export type AlertsResponse = {
  ts: number;
  alerts: AlertRow[];
  degraded: string[];
};

export type PnlResponse = {
  ts: number;
  fill_count: number;
  first_ts: number | null;
  last_ts: number | null;
  turnover: string;
  fee: string;
  round_trips: number;
  wins: number;
  losses: number;
  win_rate: string | null;
  profit_factor: string | null;
  total_pnl: string;
  avg_win: string | null;
  avg_loss: string | null;
  best: string | null;
  worst: string | null;
  degraded: string[];
};

export type RiskLimitRule = {
  key: string;
  label: string;
  used: string | null;
  limit: string | null;
  ratio: string | null;
  level: "ok" | "warn" | "halt";
  detail: string;
};

export type RiskLimitsResponse = {
  ts: number;
  known: boolean;
  strategy: string | null;
  paper_mode: boolean;
  equity: string | null;
  equity_source: "snapshot" | "initial_cash" | null;
  day_start_ts: number;
  rules: RiskLimitRule[];
  degraded: string[];
};

export type EquityPoint = {
  ts: number;
  total_equity: string;
  avail_usdt: string;
  frozen_usdt: string;
  unrealized_pnl: string;
  margin_ratio: string | null;
};

export type EquityResponse = {
  ts: number;
  hours: number;
  points: EquityPoint[];
  degraded: string[];
};

export type CandleRow = {
  ts: number;
  open: string;
  high: string;
  low: string;
  close: string;
  vol: string;
};

export type CandleFill = {
  ts: number;
  side: string;
  qty: string;
  price: string;
};

export type CandlesResponse = {
  ts: number;
  inst: string;
  bar: string;
  available_bars: string[];
  bars: CandleRow[];
  fills: CandleFill[];
  position: { qty: string; avg_price: string } | null;
  degraded: string[];
};

export type StrategyInfo = {
  id: string;
  params: Record<string, unknown>;
};

export type StrategiesResponse = {
  strategies: StrategyInfo[];
};

export type MetricMap = Record<string, number | null>;

export type BacktestTrade = {
  ts: number;
  side: string;
  qty: string;
  price: string;
  fee: string;
};

export type BacktestResponse = {
  kind: "backtest";
  strategy: string;
  inst: string;
  bar: string;
  start: string;
  end: string;
  params: Record<string, unknown>;
  bar_count: number;
  warmup_count: number;
  trade_count: number;
  trades_truncated: boolean;
  unfilled_signals: number;
  final_equity: string;
  metrics: MetricMap;
  equity: { ts: number; equity: string }[];
  trades: BacktestTrade[];
  elapsed_ms: number;
};

export type OptimizeRow = {
  rank: number;
  params: Record<string, string>;
  metrics: MetricMap;
};

export type OptimizeResponse = {
  kind: "optimize";
  strategy: string;
  inst: string;
  bar: string;
  start: string;
  end: string;
  grid: Record<string, (string | number)[]>;
  metric: string;
  bar_count: number;
  warmup_count: number;
  combo_count: number;
  skipped_count: number;
  results: OptimizeRow[];
  elapsed_ms: number;
};

export const METRIC_LABELS: Record<string, string> = {
  total_return: "总收益率",
  annual_return: "年化收益率",
  sharpe: "夏普比率",
  max_drawdown: "最大回撤",
  win_rate: "胜率",
  profit_factor: "盈亏比",
  trade_count: "交易次数",
  avg_holding_bars: "平均持仓K线数",
};

const PCT_METRICS = new Set([
  "total_return",
  "annual_return",
  "max_drawdown",
  "win_rate",
]);

/** 按指标语义格式化：比率转百分比，缺失显示占位符。 */
export function metricText(key: string, value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (!Number.isFinite(value)) return "—";
  if (PCT_METRICS.has(key)) return pct(String(value));
  if (key === "trade_count") return String(Math.round(value));
  return value.toFixed(2);
}

async function getJson<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`${path} -> HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export type ReportRow = {
  metric: string;
  paper: string;
  backtest: string;
  deviation: string;
};

export type ReportItem = {
  name: string;
  date: string;
  strategy: string;
  inst: string;
  mtime: number;
  size: number;
  status: string;
  max_deviation: number;
  rows: ReportRow[];
};

export type ReportsResponse = {
  ts: number;
  out_dir: string;
  threshold: number;
  reports: ReportItem[];
  degraded: string[];
};

export async function fetchAll(signal: AbortSignal) {
  const [overview, positions, orders, fills, pnl, alerts, engine] =
    await Promise.all([
      getJson<Overview>("/api/overview", signal),
      getJson<PositionsResponse>("/api/positions", signal),
      getJson<OrdersResponse>("/api/orders?limit=30", signal),
      getJson<FillsResponse>("/api/fills?limit=30", signal),
      getJson<PnlResponse>("/api/pnl", signal),
      getJson<AlertsResponse>("/api/alerts?limit=200", signal),
      getJson<EngineResponse>("/api/engine", signal),
    ]);
  return { overview, positions, orders, fills, pnl, alerts, engine };
}

/** 风控限额单独拉取：后端尚未部署该接口时不能拖垮整个页面。 */
export async function fetchRiskLimits(signal: AbortSignal) {
  return getJson<RiskLimitsResponse>("/api/risk-limits", signal);
}

/** 权益曲线单独拉取：时间范围由页面切换，后端上限 720 小时（30 天）。 */
export async function fetchEquity(signal: AbortSignal, hours: number) {
  return getJson<EquityResponse>(`/api/equity?hours=${hours}`, signal);
}

/** 日报变化很慢（每天一份），单独低频拉取。 */
export async function fetchReports(signal: AbortSignal, limit = 20) {
  return getJson<ReportsResponse>(`/api/reports?limit=${limit}`, signal);
}

export type KillSwitchState = {
  active: boolean;
  reason: string | null;
  auth_required: boolean;
};

/** 切换紧急停止标志（唯一的写操作）。自定义头用于挡跨站请求。 */
export async function setKillSwitch(
  action: "on" | "off",
  reason: string,
  token = ""
): Promise<KillSwitchState> {
  const response = await fetch("/api/kill-switch", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Dashboard-Request": "1",
      ...(token ? { "X-Dashboard-Token": token } : {}),
    },
    body: JSON.stringify({ action, reason }),
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // 非 JSON 响应，保留状态码即可
    }
    throw new Error(detail);
  }
  return (await response.json()) as KillSwitchState;
}

export async function fetchCandles(signal: AbortSignal, bar: string) {
  return getJson<CandlesResponse>(`/api/candles?bar=${bar}&limit=300`, signal);
}

async function postJson<T>(
  path: string,
  body: unknown,
  signal: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    // FastAPI 的错误详情在 detail 字段，直接展示给页面
    const payload = (await response.json().catch(() => null)) as
      | { detail?: unknown }
      | null;
    const detail = payload?.detail;
    throw new Error(
      typeof detail === "string" ? detail : `${path} -> HTTP ${response.status}`,
    );
  }
  return (await response.json()) as T;
}

export type BacktestBody = {
  strategy: string;
  start: string;
  end: string;
  inst?: string;
  bar: string;
  params: string;
  warmup_bars: number;
};

export type OptimizeBody = BacktestBody & {
  grid: string;
  metric: string;
  top: number;
};

export async function fetchStrategies(signal: AbortSignal) {
  return getJson<StrategiesResponse>("/api/strategies", signal);
}

export async function runBacktest(signal: AbortSignal, body: BacktestBody) {
  return postJson<BacktestResponse>("/api/backtest", body, signal);
}

export async function runOptimize(signal: AbortSignal, body: OptimizeBody) {
  return postJson<OptimizeResponse>("/api/optimize", body, signal);
}

export function num(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return parsed.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function pct(value: string | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return `${(parsed * 100).toFixed(digits)}%`;
}

export function compactQty(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return Math.abs(parsed) >= 1 ? num(parsed, 4) : parsed.toPrecision(4);
}

export function clock(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts).toLocaleTimeString("zh-CN", { hour12: false });
}

export function stamp(ts: number | null | undefined): string {
  if (!ts) return "—";
  const date = new Date(ts);
  const day = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  return `${day} ${clock(ts)}`;
}

export function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h`;
}
