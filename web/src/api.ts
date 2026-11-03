export type Heartbeat = {
  online: boolean;
  last_ts: number | null;
  age_seconds: number | null;
};

export type Overview = {
  ts: number;
  snapshot_ts: number | null;
  trading_mode: "demo" | "live";
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

export type TodayPnlInstrument = {
  inst_id: string;
  realized: string;
  unrealized: string;
  fee: string;
  turnover: string;
  pnl: string;
};

export type TodayPnlResponse = {
  ts: number;
  day_start: number;
  instruments: TodayPnlInstrument[];
  total_realized: string;
  total_unrealized: string;
  total_fee: string;
  total_pnl: string;
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

async function postWrite<T>(
  path: string,
  body: unknown,
  token = "",
  method: "POST" | "PUT" = "POST"
): Promise<T> {
  const response = await fetch(path, {
    method,
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

export type ManualOrderResult = {
  inst_id: string;
  inst_type: "SPOT" | "SWAP" | "FUTURES";
  side: "buy" | "sell";
  order_type: "market" | "limit";
  ok: boolean;
  ts: number;
  /** 用户请求的数量（基础币） */
  requested_qty: string;
  /** 实际下发数量；被风控减量时会小于 requested_qty */
  submitted_qty: string;
  contracts: string | null;
  price: string | null;
  adjusted: boolean;
  order_id: string;
  status: string;
  fill: ManualFill | null;
  error: string | null;
};

export type CancelOrderResult = {
  ok: boolean;
  order_id: string;
  status: string;
  /** true 表示订单早已终结，本次未再打交易所 */
  already: boolean;
  ts: number;
};

export type AmendOrderResult = {
  ok: boolean;
  order_id: string;
  inst_id: string;
  status: string;
  submitted_qty: string;
  contracts: string | null;
  price: string;
  adjusted: boolean;
  ts: number;
};

/** 手工下单（市价/限价）：限价单必须带 price。 */
export async function placeManualOrder(
  input: {
    instId: string;
    side: "buy" | "sell";
    orderType: "market" | "limit";
    qty: string;
    price?: string;
    reason: string;
  },
  token = ""
): Promise<ManualOrderResult> {
  return postWrite<ManualOrderResult>(
    "/api/manual/orders",
    {
      inst_id: input.instId,
      side: input.side,
      order_type: input.orderType,
      qty: input.qty,
      price: input.price,
      reason: input.reason,
    },
    token
  );
}

/** 撤单：订单已终结时返回 already=true。 */
export async function cancelManualOrder(
  orderId: string,
  reason: string,
  token = ""
): Promise<CancelOrderResult> {
  return postWrite<CancelOrderResult>(
    `/api/manual/orders/${encodeURIComponent(orderId)}/cancel`,
    { reason },
    token
  );
}

/** 改价/改量：qty 与 price 至少一个，仅限限价单。 */
export async function amendManualOrder(
  orderId: string,
  input: { qty?: string; price?: string; reason: string },
  token = ""
): Promise<AmendOrderResult> {
  return postWrite<AmendOrderResult>(
    `/api/manual/orders/${encodeURIComponent(orderId)}/amend`,
    input,
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
  alert_id?: string;
  rule: string;
  level: string;
  message: string;
  /** Redis 实时缓存行的时间字段 */
  ts?: number;
  /** SQLite 权威行的时间字段 */
  created_at?: number;
  /** open / acked / resolved；Redis 缓存行没有该字段 */
  state?: string;
  suppressed?: number;
  inst_id?: string | null;
  strategy_id?: string | null;
  acked_at?: number | null;
  acked_by?: string | null;
};

/** 生效中的静音：inst_id 为空表示该规则下全部品种。 */
export type AlertSilence = {
  silence_id: string;
  rule: string;
  inst_id: string | null;
  until_ts: number;
  reason: string;
  created_by: string;
  created_at: number;
};

export type AlertsResponse = {
  ts: number;
  /** SQLite 读失败回落 Redis 时为 null，表示「只有最近 N 条」 */
  total: number | null;
  alerts: AlertRow[];
  silences: AlertSilence[];
  /** 规则级启停（M9-2）：被配置有意停用的规则 */
  disabled_rules: string[];
  degraded: string[];
};

export type AlertRange = {
  since: number | null;
  until: number | null;
};

export type AlertStatsRule = {
  rule: string;
  total: number;
  errors: number;
  warnings: number;
  infos: number;
};

export type AlertStatsResponse = {
  ts: number;
  days: number;
  since: number;
  /** 区间内告警总数（各规则之和） */
  total: number;
  rules: AlertStatsRule[];
  degraded: string[];
};

export type AckResult = { alert_id: string; acked: boolean };
export type SilenceResult = { silence_id: string };
export type RevokeSilenceResult = { silence_id: string; revoked: boolean };

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

/** 规则最近一次命中详情（M7-9，来自 risk_events） */
export type RiskRuleEvent = {
  ts: number;
  decision: "adjusted" | "rejected" | "halted" | "sized" | string | null;
  strategy_id: string | null;
  inst_id: string | null;
  signal_id: string | null;
  limit_value: string | null;
  actual_value: string | null;
  qty_before: string | null;
  qty_after: string | null;
  reason: string | null;
};

export type RiskLimitRule = {
  key: string;
  label: string;
  used: string | null;
  limit: string | null;
  ratio: string | null;
  level: "ok" | "warn" | "halt";
  detail: string;
  /** 该限值来自哪一层：default/global/strategy/override */
  source: string | null;
  /** 今日（UTC+8 切日）该规则实际触发次数 */
  triggers: number;
  /** 最近一次命中详情；今日未触发时为 null */
  last_event: RiskRuleEvent | null;
};

export type RiskCircuit = {
  inst_id: string;
  triggered_ts: number;
  resume_at: number;
  reason: string;
  /** tripped=冷却中；awaiting_resume=冷却已结束，等待人工确认恢复 */
  status: "tripped" | "awaiting_resume";
  requires_confirm: boolean;
};

export type CircuitResumeResult = {
  inst_id: string;
  resumed: boolean;
  reason?: string;
  resume_at?: number;
  triggered_ts?: number;
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
  /** 当前处于熔断的品种（M7-6）；空数组表示无熔断 */
  circuits: RiskCircuit[];
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

export type ParamSpec = {
  name: string;
  type: "int" | "float" | "decimal" | "bool" | "str" | "choice";
  default: string | number;
  min: string | null;
  max: string | null;
  choices: string[] | null;
  description: string;
  unit: string | null;
};

export type StrategyInfo = {
  id: string;
  params: Record<string, unknown>;
  // 参数覆盖面板依赖这三项；旧缓存数据可能缺失，故为可选
  param_spec?: ParamSpec[];
  sources?: Record<string, string>;
  editable?: string[];
};

export type StrategiesResponse = {
  strategies: StrategyInfo[];
};

export type StrategyParamDetail = StrategyInfo & {
  changed: string[];
};

export type ParamChange = {
  id: number;
  strategy_id: string;
  param: string;
  old_value: string | null;
  new_value: string;
  reason: string | null;
  changed_by: string;
  changed_at: number;
  git_commit: string | null;
};

export type ParamChangesResponse = {
  changes: ParamChange[];
  total: number;
};

export type StrategyBinding = {
  strategy_id: string;
  inst_id: string;
  bar: string;
  enabled: boolean;
  updated_at: number;
};

export type BindingsResponse = {
  strategy_id: string;
  // 新增绑定时可省略 bar，后端用该默认周期
  default_bar: string;
  // false 表示回滚开关打开，引擎只跑 [engine] 单组合
  multi_binding?: boolean;
  bindings: StrategyBinding[];
};

export type BindingResponse = {
  binding: StrategyBinding;
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

/** 月度收益（M4-14）：首月只作基准，不出行 */
export type MonthlyReturn = {
  month: string;
  ret: number;
};

export type RunStatus = "pending" | "running" | "done" | "failed" | "canceled";

/** 回测任务登记行（M4-9/M4-10，来源 backtest_runs） */
export type BacktestRun = {
  run_id: string;
  strategy: string;
  inst: string;
  bar: string;
  start: string;
  end: string;
  params: Record<string, unknown>;
  warmup_bars: number;
  kind: string;
  status: RunStatus | string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  metrics: MetricMap;
};

export type BacktestRunsResponse = {
  total: number;
  runs: BacktestRun[];
};

export type BacktestRunDetail = BacktestRun & {
  artifacts: { kind: string; size: number | null; created_at: number }[];
};

export type BacktestCompareResponse = {
  inst_id: string;
  bar: string;
  runs: BacktestRun[];
};

export type BacktestEquityPoint = {
  ts: number;
  equity: string;
  drawdown: string;
};

export type BacktestTradesResponse = {
  total: number;
  trades: BacktestTrade[];
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
  calmar: "卡玛比率",
  sortino: "索提诺比率",
  max_consecutive_losses: "最大连亏笔数",
  max_consecutive_loss_amount: "最大连亏金额",
  exposure_ratio: "持仓时间占比",
  turnover: "换手率",
  funding_cost: "资金费率成本",
};

const PCT_METRICS = new Set([
  "total_return",
  "annual_return",
  "max_drawdown",
  "win_rate",
  "exposure_ratio",
  // walk-forward 汇总指标
  "compounded_return",
]);

const INT_METRICS = new Set([
  "trade_count",
  "max_consecutive_losses",
  "profitable_segments",
  "total_oos_trades",
]);

/** 金额类指标：4 位小数，避免几分钱的资金费被四舍五入成 0.00。 */
const AMOUNT_METRICS = new Set(["funding_cost"]);

/** 按指标语义格式化：比率转百分比，缺失显示占位符。 */
export function metricText(key: string, value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (!Number.isFinite(value)) return "—";
  if (PCT_METRICS.has(key)) return pct(String(value));
  if (INT_METRICS.has(key)) return String(Math.round(value));
  if (AMOUNT_METRICS.has(key)) return value.toFixed(4);
  return value.toFixed(2);
}

async function getJson<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`${path} -> HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

/** 带写操作守卫头的 POST：本机 Host + X-Dashboard-Request。 */
async function postGuarded<T>(
  path: string,
  body: unknown,
  token = "",
): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
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
      // 非 JSON 响应，保留状态码
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

/** 带写操作守卫头的 DELETE（用于清除覆盖这类无 body 的写操作）。 */
async function deleteGuarded<T>(path: string, token = ""): Promise<T> {
  const response = await fetch(path, {
    method: "DELETE",
    headers: {
      Accept: "application/json",
      "X-Dashboard-Request": "1",
      ...(token ? { "X-Dashboard-Token": token } : {}),
    },
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      // 非 JSON 响应，保留状态码
    }
    throw new Error(detail);
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

export type QualitySummary = {
  expected: number;
  present: number;
  gaps_open: number;
  gaps_repaired: number;
};

export type QualityItem = {
  inst_id: string;
  bar_size: string;
  generated_at: number | null;
  checked_range: number[];
  summary: Partial<QualitySummary>;
};

export type QualityListResponse = {
  ts: number;
  reports: QualityItem[];
  degraded: string[];
};

export type QualityGap = {
  from: number;
  to: number;
  bars: number;
  status: "open" | "repaired";
  repaired_at?: number;
};

export type QualityReport = {
  inst_id: string;
  bar_size: string;
  generated_at: number;
  checked_range: number[];
  summary: QualitySummary;
  gaps: QualityGap[];
  invalid_ohlc: { ts: number; reason: string }[];
};

export type QualityDetailResponse = {
  ts: number;
  inst_id: string;
  bar_size: string;
  report: QualityReport | null;
  degraded: string[];
};

export async function fetchAll(
  signal: AbortSignal,
  alertRange: AlertRange = { since: null, until: null },
) {
  const alertsQuery = new URLSearchParams({ limit: "200" });
  // M9-5 时间区间筛选走服务端：前端只截 200 条时按时间过滤会漏掉区间内的行
  if (alertRange.since !== null) alertsQuery.set("since", String(alertRange.since));
  if (alertRange.until !== null) alertsQuery.set("until", String(alertRange.until));
  const [overview, positions, orders, fills, pnl, alerts, engine] =
    await Promise.all([
      getJson<Overview>("/api/overview", signal),
      getJson<PositionsResponse>("/api/positions", signal),
      getJson<OrdersResponse>("/api/orders?limit=30", signal),
      getJson<FillsResponse>("/api/fills?limit=30", signal),
      getJson<PnlResponse>("/api/pnl", signal),
      getJson<AlertsResponse>(`/api/alerts?${alertsQuery.toString()}`, signal),
      getJson<EngineResponse>("/api/engine", signal),
    ]);
  return { overview, positions, orders, fills, pnl, alerts, engine };
}

/** M9-14 告警趋势：近 N 天各规则触发次数（days 支持 7/30）。 */
export async function fetchAlertStats(signal: AbortSignal, days = 7) {
  const data = await getJson<AlertStatsResponse>(
    `/api/alerts/stats?days=${days}`,
    signal,
  );
  return {
    ...data,
    rules: data.rules ?? [],
    degraded: data.degraded ?? [],
  };
}

/** 风控限额单独拉取：后端尚未部署该接口时不能拖垮整个页面。 */
export async function fetchRiskLimits(signal: AbortSignal) {
  const data = await getJson<RiskLimitsResponse>("/api/risk-limits", signal);
  // 兼容旧后端：尚未返回 circuits 字段时补空数组，避免渲染期崩溃
  return { ...data, circuits: data.circuits ?? [] };
}

export type WsStatus = {
  ts: number | null;
  connected: boolean;
  failures: number;
  last_error: string | null;
  last_error_ts: number | null;
  connected_since: number | null;
  next_delay: number | null;
};

export type ProcessItem = {
  name: string;
  online: boolean;
  last_ts: number | null;
  age_seconds: number | null;
  pid: number | null;
  restarts: number | null;
  last_exit_code: number | null;
  interval_seconds: number;
  /** no_key / not_managed 等判定原因 */
  reason?: string | null;
  /** M2-2：仅 worker 启用 --ws 时存在 */
  ws?: WsStatus | null;
};

export type ProcessesResponse = {
  ts: number;
  processes: ProcessItem[];
  online: number;
  total: number;
  offline: string[];
  degraded: string[];
};

/** M1-5：7 个常驻进程各自心跳，判活逻辑全在后端。 */
export async function fetchProcesses(signal: AbortSignal) {
  const data = await getJson<ProcessesResponse>("/api/processes", signal);
  return { ...data, processes: data.processes ?? [] };
}

/** M1-7 数据新鲜度：引擎实际消费的 inst × bar。 */
export type FreshnessItem = {
  inst_id: string;
  bar: string;
  last_bar_ts: number | null;
  lag_seconds: number | null;
  /** 本地最新已收盘 bar 与引擎水位的差 */
  lag_bars: number | null;
  /** 此刻应有的已收盘 bar 时间 */
  expected_ts: number | null;
  /** 本地最新 bar 相对 expected_ts 的缺口，用于发现 candles 停更 */
  missing_bars: number | null;
  engine_watermark_ts: number | null;
  stale: boolean;
  degraded: string[];
};

export type FreshnessResponse = {
  ts: number;
  threshold_bars: number;
  items: FreshnessItem[];
  stale_count: number;
  degraded: string[];
};

export async function fetchFreshness(signal: AbortSignal) {
  const data = await getJson<FreshnessResponse>("/api/freshness", signal);
  return { ...data, items: data.items ?? [] };
}

/** M1-6 今日事件流：当天信号/下单/成交/告警四表合并后的一条 */
export type TimelineItem = {
  ts: number;
  kind: "alert" | "signal" | "order" | "fill" | string;
  /** alert→级别；order→订单状态；fill/signal→方向 */
  severity: string | null;
  title: string;
  detail: string;
  ref_id: string;
  /** 跳转目标：alerts / strategies / execution（前端再映射到 tab） */
  module: string;
};

export type TimelineResponse = {
  ts: number;
  /** 当日 UTC+8 零点，与风控日切口径一致 */
  since: number;
  items: TimelineItem[];
  degraded: string[];
};

export async function fetchTimeline(
  signal: AbortSignal,
  kinds: string[] = [],
  limit = 50,
) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (kinds.length > 0) query.set("kinds", kinds.join(","));
  const data = await getJson<TimelineResponse>(
    `/api/timeline?${query.toString()}`,
    signal,
  );
  return { ...data, items: data.items ?? [] };
}

export type RiskOverrideItem = {
  id: number;
  strategy_id: string;
  param: string;
  old_value: string | null;
  new_value: string;
  reason: string | null;
  source: string;
  changed_by: string;
  changed_at: number;
  revoked_at: number | null;
  revoked_by: string | null;
};

export type RiskOverridesResponse = {
  ts: number;
  strategy_id: string | null;
  /** 当前生效的覆盖：{param: value} */
  effective: Record<string, string>;
  items: RiskOverrideItem[];
  total: number | null;
  degraded: string[];
};

export type RiskOverrideResult = {
  strategy_id: string;
  param: string;
  old_value: string | null;
  new_value: string;
  reason: string;
  changed_by: string;
  changed_at: number;
};

export type RiskOverrideClearResult = {
  strategy_id: string;
  param: string;
  cleared: boolean;
  reason?: string;
  old_value?: string | null;
  revoked?: number;
  cleared_at?: number;
};

/** 在线阈值覆盖与变更审计（M7-8 / M7-14）。 */
export async function fetchRiskOverrides(
  signal: AbortSignal,
  strategyId?: string | null,
  limit = 20,
) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (strategyId) query.set("strategy_id", strategyId);
  return getJson<RiskOverridesResponse>(
    `/api/risk/overrides?${query.toString()}`,
    signal,
  );
}

/** 写入/更新一条覆盖：调完 worker 下一轮心跳即生效，无需重启。 */
export async function setRiskOverride(
  payload: {
    strategy_id: string;
    param: string;
    value: string;
    reason: string;
    by?: string;
  },
  token = "",
) {
  return postGuarded<RiskOverrideResult>(
    "/api/risk/overrides",
    payload,
    token,
  );
}

/** 清除覆盖，回落到配置层（全局/策略取严结果）。 */
export async function clearRiskOverride(
  strategyId: string,
  param: string,
  token = "",
) {
  return deleteGuarded<RiskOverrideClearResult>(
    `/api/risk/overrides/${encodeURIComponent(strategyId)}/${encodeURIComponent(param)}`,
    token,
  );
}

/** 告警标记已读：后端幂等，重复确认不覆盖首次时间与人。 */
export async function ackAlert(alertId: string, token = "") {
  return postWrite<AckResult>(
    `/api/alerts/${encodeURIComponent(alertId)}/ack`,
    { by: "dashboard" },
    token,
  );
}

/** 创建静音：`rule` 传空串表示全局临时静音（M9-10）。 */
export async function silenceAlerts(
  input: { rule: string; inst_id?: string | null; hours: number; reason: string },
  token = "",
) {
  return postWrite<SilenceResult>("/api/alerts/silence", input, token);
}

/** 解除静音：只置 revoked_at，不删除历史记录。 */
export async function revokeSilence(silenceId: string, token = "") {
  return deleteGuarded<RevokeSilenceResult>(
    `/api/alerts/silence/${encodeURIComponent(silenceId)}`,
    token,
  );
}

/** 权益曲线单独拉取：时间范围由页面切换，后端上限 720 小时（30 天）。 */
export async function fetchEquity(signal: AbortSignal, hours: number) {
  return getJson<EquityResponse>(`/api/equity?hours=${hours}`, signal);
}

/** M1-8 今日盈亏汇总：按品种拆分已实现 + 未实现 + 手续费。 */
export async function fetchTodayPnl(signal: AbortSignal) {
  return getJson<TodayPnlResponse>("/api/today-pnl", signal);
}

/** 日报变化很慢（每天一份），单独低频拉取。 */
export async function fetchReports(signal: AbortSignal, limit = 20) {
  return getJson<ReportsResponse>(`/api/reports?limit=${limit}`, signal);
}

/** 数据质量报告摘要列表；candles 每轮同步后刷新。 */
export async function fetchQuality(signal: AbortSignal) {
  return getJson<QualityListResponse>("/api/quality", signal);
}

/** 单个品种/周期的完整报告（点开摘要行时才拉）。 */
export async function fetchQualityReport(
  signal: AbortSignal,
  instId: string,
  barSize: string,
) {
  const query = new URLSearchParams({ inst_id: instId, bar_size: barSize });
  return getJson<QualityDetailResponse>(`/api/quality?${query}`, signal);
}

export type DeadLetterItem = {
  dead_id: string;
  signal_id: string | null;
  stage: string;
  reason: string;
  payload: string;
  payload_truncated: boolean;
  created_at: number;
  replayed_at: number | null;
  replay_result: string | null;
};

export type DeadLettersResponse = {
  ts: number;
  items: DeadLetterItem[];
  /** null 表示 SQLite 读取失败，列表不可信 */
  total: number | null;
  degraded: string[];
};

export type DeadLetterResult = { dead_id: string; result: string };

export type DeadLetterBatch = { results: DeadLetterResult[] };

/** 死信列表；`stage` 可选 parse / submit / reconcile。 */
export async function fetchDeadLetters(
  signal: AbortSignal,
  options: {
    limit?: number;
    offset?: number;
    stage?: string;
    pendingOnly?: boolean;
  } = {},
) {
  const query = new URLSearchParams({
    limit: String(options.limit ?? 50),
    offset: String(options.offset ?? 0),
  });
  if (options.stage) query.set("stage", options.stage);
  if (options.pendingOnly) query.set("pending_only", "true");
  return getJson<DeadLettersResponse>(`/api/dead-letters?${query}`, signal);
}

/** 重放单条；返回 requeued / already_processed / already_replayed 等。 */
export async function replayDeadLetter(deadId: string, token = "") {
  return postGuarded<DeadLetterResult>(
    `/api/dead-letters/${encodeURIComponent(deadId)}/replay`,
    {},
    token,
  );
}

export async function replayDeadLetters(ids: string[], token = "") {
  return postGuarded<DeadLetterBatch>("/api/dead-letters/replay", { ids }, token);
}

/** 标记丢弃：保留记录，不再重放。 */
export async function discardDeadLetter(deadId: string, token = "") {
  return postGuarded<DeadLetterResult>(
    `/api/dead-letters/${encodeURIComponent(deadId)}/discard`,
    {},
    token,
  );
}

/** 人工确认恢复熔断中的品种（M7-12）。 */
export async function resumeCircuit(instId: string, token = "") {
  return postGuarded<CircuitResumeResult>(
    `/api/risk/circuits/${encodeURIComponent(instId)}/resume`,
    {},
    token,
  );
}

export type ReconciliationRow = {
  rec_id: string;
  ts: number;
  /** order / position / balance */
  kind: string;
  key: string;
  local_value: string | null;
  remote_value: string | null;
  diff: string | null;
  /** auto=本地已自动校正；manual=需人工确认 */
  severity: "auto" | "manual";
  resolved: boolean;
  resolved_at: number | null;
  resolved_by: string | null;
  note: string | null;
};

export type ReconciliationsResponse = {
  ts: number;
  items: ReconciliationRow[];
  /** null 表示 SQLite 读取失败，列表不可信 */
  total: number | null;
  degraded: string[];
};

export type ResolveReconciliationResult = {
  rec_id: string;
  /** resolved / already_resolved */
  result: string;
};

/** 对账差异列表；`kind` 可选 order / position / balance。 */
export async function fetchReconciliations(
  signal: AbortSignal,
  options: {
    limit?: number;
    offset?: number;
    kind?: string;
    onlyOpen?: boolean;
  } = {},
) {
  const query = new URLSearchParams({
    limit: String(options.limit ?? 50),
    offset: String(options.offset ?? 0),
  });
  if (options.kind) query.set("kind", options.kind);
  if (options.onlyOpen) query.set("only_open", "true");
  return getJson<ReconciliationsResponse>(
    `/api/reconciliations?${query}`,
    signal,
  );
}

/** 标记差异已处理；重复点击返回 already_resolved。 */
export async function resolveReconciliation(recId: string, token = "") {
  return postGuarded<ResolveReconciliationResult>(
    `/api/reconciliations/${encodeURIComponent(recId)}/resolve`,
    {},
    token,
  );
}

export type KillSwitchCancel = {
  ok: boolean;
  exchange_canceled?: number;
  local_marked?: number;
  error?: string;
};

export type KillSwitchState = {
  active: boolean;
  reason: string | null;
  auth_required: boolean;
  // 激活且勾选撤单时返回；未撤单则不出现该字段
  cancel?: KillSwitchCancel;
};

/** 切换紧急停止标志；激活时默认同时撤掉交易所挂单。自定义头用于挡跨站请求。 */
export async function setKillSwitch(
  action: "on" | "off",
  reason: string,
  token = "",
  cancelOrders = true
): Promise<KillSwitchState> {
  const response = await fetch("/api/kill-switch", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Dashboard-Request": "1",
      ...(token ? { "X-Dashboard-Token": token } : {}),
    },
    body: JSON.stringify({ action, reason, cancel_orders: cancelOrders }),
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
  // 并行进程数；>1 时后端走进程池
  n_jobs?: number;
};

/** 批量回测入参（M4-7）：品种与周期都是列表，网格可选。 */
export type BatchBody = {
  strategy: string;
  insts: string[];
  bars: string[];
  start: string;
  end: string;
  params?: string;
  grid?: string;
  metric?: string;
  warmup_bars?: number;
};

/** 批量回测的单格结果；失败行带 error、指标为空。 */
export type BatchCellRow = {
  inst: string;
  bar: string;
  params: Record<string, string>;
  metrics: MetricMap;
  error: string | null;
};

export type BatchResponse = {
  kind: "batch";
  strategy: string;
  insts: string[];
  bars: string[];
  start: string;
  end: string;
  params: Record<string, unknown>;
  grid: Record<string, (string | number)[]>;
  metric: string;
  combo_count: number;
  cell_count: number;
  failed_count: number;
  rows: BatchCellRow[];
  failed: BatchCellRow[];
  elapsed_ms: number;
};

export async function fetchStrategies(signal: AbortSignal) {
  return getJson<StrategiesResponse>("/api/strategies", signal);
}

export async function updateStrategyParams(
  strategyId: string,
  values: Record<string, unknown>,
  reason: string,
  token = ""
) {
  return postWrite<StrategyParamDetail>(
    `/api/strategies/${encodeURIComponent(strategyId)}/params`,
    { values, reason, by: "owner" },
    token,
    "PUT"
  );
}

export async function fetchParamChanges(
  signal: AbortSignal,
  strategyId: string
) {
  return getJson<ParamChangesResponse>(
    `/api/strategies/${encodeURIComponent(strategyId)}/param-changes?limit=5`,
    signal
  );
}

export type RollbackResult = {
  ok: boolean;
  message: string;
  ts: number;
};

/** M3-14 参数回滚：从历史记录恢复参数值，产生新的变更记录。 */
export async function rollbackParam(
  strategyId: string,
  changeId: number,
  body: { reason?: string; changed_by?: string } = {},
  token = ""
) {
  return postWrite<RollbackResult>(
    `/api/strategies/${encodeURIComponent(strategyId)}/param-changes/${changeId}/rollback`,
    body,
    token
  );
}

export type StrategyEvaluation = {
  inst_id: string;
  bar: string;
  /** signal / no_signal / duplicate / waiting_data / skipped / error */
  status: string | null;
  bar_ts: number | null;
  reason: string | null;
  signal_count: number;
  ts: number | null;
};

export type StrategyEvaluationsResponse = {
  ts: number;
  strategy_id: string;
  items: StrategyEvaluation[];
  degraded: string[];
};

/** M3-15 策略详情页：最近 N 次评估（含无信号的轮次）。 */
export async function fetchStrategyEvaluations(
  signal: AbortSignal,
  strategyId: string,
  limit = 10
) {
  return getJson<StrategyEvaluationsResponse>(
    `/api/strategies/${encodeURIComponent(strategyId)}/evaluations?limit=${limit}`,
    signal
  );
}

export async function fetchBindings(signal: AbortSignal, strategyId: string) {
  return getJson<BindingsResponse>(
    `/api/strategies/${encodeURIComponent(strategyId)}/bindings`,
    signal
  );
}

export async function updateBinding(
  strategyId: string,
  instId: string,
  body: { enabled?: boolean; bar?: string },
  token = ""
) {
  return postWrite<BindingResponse>(
    `/api/strategies/${encodeURIComponent(strategyId)}/bindings/${encodeURIComponent(instId)}`,
    body,
    token,
    "PUT"
  );
}

export async function runBacktest(signal: AbortSignal, body: BacktestBody) {
  // 后端默认已改异步，页面切换前先走兼容路径，避免拿到 {run_id} 解析失败
  return postJson<BacktestResponse>("/api/backtest?sync=1", body, signal);
}

/** 提交回测任务，立即返回 run_id（M4-12 后台任务）。 */
export async function submitBacktest(signal: AbortSignal, body: BacktestBody) {
  return postJson<{ run_id: string; status: string }>(
    "/api/backtest",
    body,
    signal,
  );
}

export type BacktestRunsQuery = {
  limit?: number;
  offset?: number;
  strategy?: string;
  inst?: string;
  bar?: string;
  status?: string;
};

export async function fetchBacktestRuns(
  signal: AbortSignal,
  query: BacktestRunsQuery = {},
) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === "") continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return getJson<BacktestRunsResponse>(
    `/api/backtest/runs${qs ? `?${qs}` : ""}`,
    signal,
  );
}

export async function fetchBacktestRun(signal: AbortSignal, runId: string) {
  return getJson<BacktestRunDetail>(
    `/api/backtest/${encodeURIComponent(runId)}`,
    signal,
  );
}

export async function fetchBacktestEquity(
  signal: AbortSignal,
  runId: string,
  limit = 2000,
) {
  return getJson<{ points: BacktestEquityPoint[] }>(
    `/api/backtest/${encodeURIComponent(runId)}/equity?limit=${limit}`,
    signal,
  );
}

export async function fetchBacktestTrades(
  signal: AbortSignal,
  runId: string,
  limit = 100,
) {
  return getJson<BacktestTradesResponse>(
    `/api/backtest/${encodeURIComponent(runId)}/trades?limit=${limit}`,
    signal,
  );
}

export async function fetchBacktestCompare(
  signal: AbortSignal,
  runIds: string[],
) {
  return getJson<BacktestCompareResponse>(
    `/api/backtest/compare?ids=${encodeURIComponent(runIds.join(","))}`,
    signal,
  );
}

/** 提交批量回测任务（M4-7），立即返回 run_id。 */
export async function submitBatch(signal: AbortSignal, body: BatchBody) {
  return postJson<{ run_id: string; status: string }>(
    "/api/backtest/batch",
    body,
    signal,
  );
}

/** 批量回测逐格结果，来自产物 batch.parquet。 */
export async function fetchBacktestBatch(
  signal: AbortSignal,
  runId: string,
) {
  return getJson<{ rows: BatchCellRow[] }>(
    `/api/backtest/${encodeURIComponent(runId)}/batch`,
    signal,
  );
}

export async function cancelBacktest(runId: string, token = "") {
  return postWrite<{ run_id: string; status: string }>(
    `/api/backtest/${encodeURIComponent(runId)}/cancel`,
    {},
    token,
  );
}

export async function rerunBacktest(runId: string, token = "") {
  return postWrite<{ run_id: string; status: string }>(
    `/api/backtest/${encodeURIComponent(runId)}/rerun`,
    {},
    token,
  );
}

/** Pine 对比单行（M4-16）；deviation 为 null 表示分母为 0 无法算比例 */
export type PineCompareRow = {
  metric: string;
  pine: number;
  python: number | null;
  deviation: number | null;
  over_limit: boolean;
};

export type PineCompareResponse = {
  run_id: string;
  limit: number;
  rows: PineCompareRow[];
  unmatched: string[];
  max_deviation: number | null;
  over_limit: boolean;
  persisted: string[];
};

export async function importPine(
  runId: string,
  csv: string,
  token = "",
) {
  return postWrite<PineCompareResponse>(
    `/api/backtest/${encodeURIComponent(runId)}/pine`,
    { csv },
    token,
  );
}

/** M4-13：提交寻优任务，立即返回 job_id（后端登记式，A2-P2），进度走轮询。 */
export async function submitOptimize(signal: AbortSignal, body: OptimizeBody) {
  return postJson<{ job_id: string; status: string; total: number }>(
    "/api/optimize",
    body,
    signal,
  );
}

export type OptimizeJobResponse = {
  job_id: string;
  status: "running" | "done" | "failed";
  total: number;
  completed: number;
  metric: string;
  grid: Record<string, (string | number)[]>;
  n_jobs: number;
  created_at: number;
  finished_at: number | null;
  result: OptimizeResponse | null;
  error: string | null;
};

/** M4-13 寻优进度查询（A2-P3）：按 job_id 拉取进度，done 时携带完整结果。 */
export async function getOptimizeJob(signal: AbortSignal, jobId: string) {
  return getJson<OptimizeJobResponse>(
    `/api/optimize/${encodeURIComponent(jobId)}`,
    signal,
  );
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

export function pct(
  value: string | number | null | undefined,
  digits = 2
): string {
  if (value === null || value === undefined || value === "") return "—";
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
