import type { EChartsOption } from "echarts";
import { TZ_OFFSET_MS } from "../api";
import {
  boll,
  kdj,
  macd,
  pivotLevels,
  sma,
  supertrend,
  type Series,
} from "./indicators";
import type { Bar, ChartSettings } from "./types";

export type FillPoint = { ts: number; side: string; qty: string; price: string };
export type Position = { qty: string; avg_price: string } | null;
export type ZoomRange = { start: number; end: number };

export type BuildInput = {
  bars: Bar[];
  settings: ChartSettings;
  fills: FillPoint[];
  position: Position;
  bar: string;
  zoom: ZoomRange | null;
};

const COLOR = {
  up: "#4fe3a1",
  down: "#ff5f5f",
  axis: "rgba(226, 240, 228, 0.12)",
  text: "#7d8b81",
  grid: "rgba(226, 240, 228, 0.05)",
  accent: "#c8f24f",
  level: "rgba(226, 240, 228, 0.34)",
};

const MA_COLORS = ["#f2c14f", "#4fa3f2", "#c8f24f", "#e07be0", "#9aa7a0"];

/** 默认展示最近约 120 根，避免 300 根挤在一起。 */
export const VISIBLE_BARS = 120;

export function defaultZoom(length: number): ZoomRange {
  if (length <= VISIBLE_BARS) return { start: 0, end: 100 };
  return { start: ((length - VISIBLE_BARS) / length) * 100, end: 100 };
}

export function toBars(
  rows: { ts: number; open: string; high: string; low: string; close: string; vol: string }[],
): Bar[] {
  const out: Bar[] = [];
  for (const row of rows) {
    const open = Number(row.open);
    const high = Number(row.high);
    const low = Number(row.low);
    const close = Number(row.close);
    const vol = Number(row.vol);
    // 一根脏 bar（NaN/Inf）会沿 sma 累加污染整段指标链，ECharts 静默断线；
    // 直接剔除（fills/撑压按 ts 定位，不受剔除影响）（P2）
    if (![open, high, low, close, vol].every(Number.isFinite)) continue;
    out.push({ ts: row.ts, open, high, low, close, vol });
  }
  return out;
}

export function buildChartOption(input: BuildInput): EChartsOption {
  const { bars, settings, fills, position, bar } = input;
  const labels = bars.map((item) => formatTime(item.ts, bar));
  const closes = bars.map((item) => item.close);
  const subKinds = [
    settings.vol.enabled ? "vol" : null,
    settings.macd.enabled ? "macd" : null,
    settings.kdj.enabled ? "kdj" : null,
  ].filter((item): item is "vol" | "macd" | "kdj" => item !== null);

  const grids = layoutGrids(subKinds.length);
  const xAxes = grids.map((_, index) => ({
    type: "category" as const,
    gridIndex: index,
    data: labels,
    boundaryGap: true,
    axisLine: { lineStyle: { color: COLOR.axis } },
    axisTick: { show: false },
    splitLine: { show: false },
    axisLabel: {
      show: index === grids.length - 1,
      color: COLOR.text,
      fontSize: 10,
      hideOverlap: true,
    },
    axisPointer: { label: { backgroundColor: "#2a3327" } },
  }));

  // 指标预计算：tooltip 与图形共用，避免重复计算
  const maSeries = settings.ma.periods.map((period) => ({
    period,
    values: sma(closes, period),
  }));
  const bollValues = settings.boll.enabled
    ? boll(closes, settings.boll.period, settings.boll.mult)
    : null;
  const stValues = settings.supertrend.enabled
    ? supertrend(bars, settings.supertrend.period, settings.supertrend.mult)
    : null;
  const levels = settings.sr.enabled
    ? pivotLevels(bars, settings.sr.leftRight, settings.sr.tolerancePct, settings.sr.maxLevels)
    : [];
  const volMaSeries = settings.vol.enabled
    ? settings.vol.maPeriods.map((period) => ({
        period,
        values: sma(bars.map((item) => item.vol), period),
      }))
    : [];
  const macdValues = settings.macd.enabled
    ? macd(closes, settings.macd.fast, settings.macd.slow, settings.macd.signal)
    : null;
  const kdjValues = settings.kdj.enabled
    ? kdj(bars, settings.kdj.n, settings.kdj.k, settings.kdj.d)
    : null;

  const series: NonNullable<EChartsOption["series"]> = [
    {
      name: "K线",
      type: "candlestick",
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: bars.map((item) => [item.open, item.close, item.low, item.high]),
      itemStyle: {
        color: COLOR.up,
        color0: COLOR.down,
        borderColor: COLOR.up,
        borderColor0: COLOR.down,
      },
      markLine: buildMarkLines(levels, position),
    },
  ];

  if (settings.ma.enabled) {
    maSeries.forEach((item, index) => {
      series.push({
        name: `MA${item.period}`,
        type: "line",
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: item.values,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1, color: MA_COLORS[index % MA_COLORS.length] },
        itemStyle: { color: MA_COLORS[index % MA_COLORS.length] },
        connectNulls: false,
        z: 3,
      });
    });
  }

  if (bollValues) {
    series.push(
      lineSeries("BOLL上轨", bollValues.upper, "#6f7f75", 0),
      lineSeries("BOLL中轨", bollValues.mid, COLOR.accent, 0, true),
      lineSeries("BOLL下轨", bollValues.lower, "#6f7f75", 0),
    );
  }

  if (stValues) {
    series.push(
      lineSeries("SuperTrend多", stValues.down, COLOR.up, 0, false, 1.4),
      lineSeries("SuperTrend空", stValues.up, COLOR.down, 0, false, 1.4),
    );
  }

  if (fills.length > 0) {
    const offset = Math.max(meanRange(bars) * 0.28, 0);
    const buys: [number, number][] = [];
    const sells: [number, number][] = [];
    for (const fill of fills) {
      const index = indexAt(bars, fill.ts);
      if (index < 0) continue;
      const price = Number(fill.price);
      if (!Number.isFinite(price)) continue;
      const target = fill.side === "buy" ? buys : sells;
      target.push([index, fill.side === "buy" ? bars[index].low - offset : bars[index].high + offset]);
    }
    if (buys.length > 0) series.push(fillSeries("买入", buys, COLOR.up, 0, "买"));
    if (sells.length > 0) series.push(fillSeries("卖出", sells, COLOR.down, 180, "卖"));
  }

  subKinds.forEach((kind, index) => {
    const gridIndex = index + 1;
    if (kind === "vol") {
      series.push({
        name: "VOL",
        type: "bar",
        xAxisIndex: gridIndex,
        yAxisIndex: gridIndex,
        barWidth: "60%",
        data: bars.map((item) => ({
          value: item.vol,
          itemStyle: {
            color: item.close >= item.open ? "rgba(79, 227, 161, 0.45)" : "rgba(255, 95, 95, 0.45)",
          },
        })),
      });
      volMaSeries.forEach((item, maIndex) => {
        series.push({
          name: `VOL MA${item.period}`,
          type: "line",
          xAxisIndex: gridIndex,
          yAxisIndex: gridIndex,
          data: item.values,
          showSymbol: false,
          smooth: true,
          lineStyle: { width: 1, color: MA_COLORS[maIndex % MA_COLORS.length] },
          itemStyle: { color: MA_COLORS[maIndex % MA_COLORS.length] },
        });
      });
    }
    if (kind === "macd" && macdValues) {
      series.push({
        name: "MACD柱",
        type: "bar",
        xAxisIndex: gridIndex,
        yAxisIndex: gridIndex,
        barWidth: "55%",
        data: macdValues.hist.map((value) => ({
          value,
          itemStyle: {
            color: (value ?? 0) >= 0 ? "rgba(79, 227, 161, 0.6)" : "rgba(255, 95, 95, 0.6)",
          },
        })),
      });
      series.push(
        lineSeries("DIF", macdValues.dif, "#f2c14f", gridIndex),
        lineSeries("DEA", macdValues.dea, "#4fa3f2", gridIndex),
      );
    }
    if (kind === "kdj" && kdjValues) {
      series.push(
        lineSeries("K", kdjValues.k, "#f2c14f", gridIndex),
        lineSeries("D", kdjValues.d, "#4fa3f2", gridIndex),
        lineSeries("J", kdjValues.j, "#e07be0", gridIndex),
      );
    }
  });

  const yAxes = grids.map((_, index) => {
    const kind = index === 0 ? "main" : subKinds[index - 1];
    return {
      gridIndex: index,
      scale: index === 0,
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: COLOR.grid } },
      axisLabel: {
        color: COLOR.text,
        fontSize: 10,
        hideOverlap: true,
        // 成交量数量级大，压成 K/M 避免标签过宽
        formatter: kind === "vol" ? (value: number) => compact(value) : undefined,
      },
      splitNumber: index === 0 ? 6 : 2,
    };
  });

  const zoom = input.zoom ?? defaultZoom(bars.length);

  return {
    animation: false,
    backgroundColor: "transparent",
    textStyle: { fontFamily: "'IBM Plex Mono', ui-monospace, monospace", fontSize: 11 },
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(12, 16, 13, 0.94)",
      borderColor: COLOR.axis,
      textStyle: { color: "#d6e3d8", fontSize: 11 },
      axisPointer: { type: "cross", lineStyle: { color: "rgba(200, 242, 79, 0.4)" } },
      formatter: buildFormatter(labels, bars, {
        ma: settings.ma.enabled ? maSeries : [],
        boll: bollValues,
        st: settings.supertrend.enabled ? settings.supertrend : null,
        stValues,
        volMa: volMaSeries,
        macd: macdValues,
        kdj: kdjValues,
      }),
    },
    // start/end 必须合进同一份 dataZoom：原先 ...zoomOption 的第二次 spread
    // 会把带 xAxisIndex/滑块样式的配置整个覆盖掉，缩放只作用于主图，
    // VOL/MACD/KDJ 副图不跟随且样式丢失（P1）
    dataZoom: [
      {
        type: "inside",
        xAxisIndex: allIndexes(grids.length),
        start: zoom.start,
        end: zoom.end,
      },
      {
        type: "slider",
        xAxisIndex: allIndexes(grids.length),
        start: zoom.start,
        end: zoom.end,
        height: 18,
        bottom: 8,
        borderColor: COLOR.axis,
        backgroundColor: "rgba(255, 255, 255, 0.02)",
        fillerColor: "rgba(200, 242, 79, 0.12)",
        handleStyle: { color: COLOR.accent },
        textStyle: { color: COLOR.text, fontSize: 10 },
      },
    ],
    series,
  } as EChartsOption;
}

function layoutGrids(subCount: number) {
  const top = 4;
  const bottom = 13;
  const gap = 5;
  const usable = 100 - top - bottom - subCount * gap;
  const mainHeight = subCount > 0 ? usable * 0.54 : usable;
  const subHeight = subCount > 0 ? (usable - mainHeight) / subCount : 0;

  const grids: { left: string; right: string; top: string; height: string }[] = [];
  let cursor = top;
  // left/right 用百分比：数字会被 ECharts 当作像素，价格轴标签会被裁掉
  grids.push({ left: "8%", right: "7%", top: `${cursor}%`, height: `${mainHeight}%` });
  cursor += mainHeight;
  for (let i = 0; i < subCount; i += 1) {
    cursor += gap;
    grids.push({ left: "8%", right: "7%", top: `${cursor}%`, height: `${subHeight}%` });
    cursor += subHeight;
  }
  return grids;
}

function allIndexes(count: number): number[] {
  return Array.from({ length: count }, (_, index) => index);
}

function lineSeries(
  name: string,
  data: Series,
  color: string,
  gridIndex: number,
  dashed = false,
  width = 1,
) {
  return {
    name,
    type: "line" as const,
    xAxisIndex: gridIndex,
    yAxisIndex: gridIndex,
    data,
    showSymbol: false,
    smooth: true,
    connectNulls: false,
    lineStyle: { width, color, type: dashed ? ("dashed" as const) : ("solid" as const) },
    itemStyle: { color },
  };
}

function fillSeries(
  name: string,
  points: [number, number][],
  color: string,
  rotate: number,
  text: string,
) {
  return {
    name,
    type: "scatter" as const,
    xAxisIndex: 0,
    yAxisIndex: 0,
    data: points,
    symbol: "triangle",
    symbolSize: 9,
    symbolRotate: rotate,
    itemStyle: { color },
    label: { show: true, formatter: text, color, fontSize: 9, position: "bottom" as const },
    z: 5,
  };
}

function buildMarkLines(levels: { price: number; touches: number; kind: string }[], position: Position) {
  const data: Record<string, unknown>[] = levels.map((level) => ({
    yAxis: level.price,
    name: level.kind === "resistance" ? "压力" : "支撑",
    lineStyle: { color: COLOR.level, width: 1, type: "dashed" as const },
    label: {
      show: true,
      position: "insideStartTop" as const,
      color: COLOR.text,
      fontSize: 9,
      formatter: `${level.kind === "resistance" ? "压" : "撑"} ${fmt(level.price)} ·${level.touches}`,
    },
  }));

  if (position) {
    const price = Number(position.avg_price);
    if (Number.isFinite(price)) {
      data.push({
        yAxis: price,
        name: "成本",
        lineStyle: { color: COLOR.accent, width: 1, type: "dashed" as const },
        label: {
          show: true,
          position: "insideStartTop" as const,
          color: COLOR.accent,
          fontSize: 9,
          formatter: `成本 ${fmt(price)}`,
        },
      });
    }
  }

  return data.length > 0 ? { silent: true, symbol: "none", data } : undefined;
}

function buildFormatter(
  labels: string[],
  bars: Bar[],
  extra: {
    ma: { period: number; values: Series }[];
    boll: { upper: Series; mid: Series; lower: Series } | null;
    st: { mult: number } | null;
    stValues: { up: Series; down: Series } | null;
    volMa: { period: number; values: Series }[];
    macd: { dif: Series; dea: Series; hist: Series } | null;
    kdj: { k: Series; d: Series; j: Series } | null;
  },
) {
  return (params: unknown): string => {
    const list = Array.isArray(params) ? params : [params];
    const first = list[0] as { dataIndex?: number } | undefined;
    const index = first?.dataIndex ?? 0;
    const bar = bars[index];
    if (!bar) return "";

    const row = (label: string, value: string, color?: string) =>
      `<div style="display:flex;gap:10px;justify-content:space-between">` +
      `<span style="color:#7d8b81">${label}</span>` +
      `<span style="color:${color ?? "#d6e3d8"}">${value}</span></div>`;

    const lines = [
      `<div style="margin-bottom:4px;color:#c8f24f">${labels[index] ?? ""}</div>`,
      row("开", fmt(bar.open)),
      row("高", fmt(bar.high)),
      row("低", fmt(bar.low)),
      row("收", fmt(bar.close), bar.close >= bar.open ? COLOR.up : COLOR.down),
      row("量", compact(bar.vol)),
    ];

    for (const item of extra.ma) {
      const value = item.values[index];
      if (value !== null && value !== undefined) lines.push(row(`MA${item.period}`, fmt(value)));
    }
    if (extra.boll) {
      const { upper, mid, lower } = extra.boll;
      if (upper[index] !== null) lines.push(row("BOLL", `${fmt(upper[index]!)} / ${fmt(lower[index]!)}`));
      if (mid[index] !== null) lines.push(row("BOLL中轨", fmt(mid[index]!)));
    }
    if (extra.stValues) {
      const st = extra.stValues.down[index] ?? extra.stValues.up[index];
      if (st !== null && st !== undefined) {
        const trend = extra.stValues.down[index] !== null ? "多" : "空";
        lines.push(row(`ST(${trend})`, fmt(st), trend === "多" ? COLOR.up : COLOR.down));
      }
    }
    for (const item of extra.volMa) {
      const value = item.values[index];
      if (value !== null && value !== undefined) lines.push(row(`VOL MA${item.period}`, compact(value)));
    }
    if (extra.macd) {
      const { dif, dea, hist } = extra.macd;
      if (dif[index] !== null) {
        lines.push(row("DIF", fmt(dif[index]!, 4), "#f2c14f"));
        lines.push(row("DEA", fmt(dea[index]!, 4), "#4fa3f2"));
        lines.push(row("MACD", fmt(hist[index]!, 4), (hist[index] ?? 0) >= 0 ? COLOR.up : COLOR.down));
      }
    }
    if (extra.kdj) {
      const { k, d, j } = extra.kdj;
      if (k[index] !== null) {
        lines.push(row("K", fmt(k[index]!, 2), "#f2c14f"));
        lines.push(row("D", fmt(d[index]!, 2), "#4fa3f2"));
        lines.push(row("J", fmt(j[index]!, 2), "#e07be0"));
      }
    }

    return lines.join("");
  };
}

function indexAt(bars: Bar[], ts: number): number {
  if (bars.length === 0) return -1;
  let low = 0;
  let high = bars.length - 1;
  let found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (bars[mid].ts <= ts) {
      found = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return found;
}

function meanRange(bars: Bar[]): number {
  if (bars.length === 0) return 0;
  const total = bars.reduce((sum, item) => sum + (item.high - item.low), 0);
  return total / bars.length;
}

export function fmt(value: number, digits?: number): string {
  if (!Number.isFinite(value)) return "—";
  const precision = digits ?? (Math.abs(value) >= 1000 ? 2 : Math.abs(value) >= 1 ? 2 : 6);
  return value.toLocaleString("en-US", {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  });
}

function compact(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (Math.abs(value) >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`;
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(2)}K`;
  return value.toFixed(2);
}

/** 图表横轴/tooltip 统一按 UTC+8（与全站 stamp、后端日切同口径）。
 *  原先用浏览器本地时区，非 UTC+8 机器上同一根 bar 在图上和其它卡片对不上（P1）。 */
export function formatTime(ts: number, bar: string): string {
  const date = new Date(ts + TZ_OFFSET_MS);
  const pad = (value: number) => String(value).padStart(2, "0");
  const month = pad(date.getUTCMonth() + 1);
  const day = pad(date.getUTCDate());
  if (/^(1D|1W|1M|1Dutc)$/i.test(bar)) return `${month}-${day}`;
  return `${month}-${day} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}
