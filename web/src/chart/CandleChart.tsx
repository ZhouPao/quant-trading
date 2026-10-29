import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { BarChart, CandlestickChart, LineChart, ScatterChart } from "echarts/charts";
import {
  AxisPointerComponent,
  DataZoomComponent,
  GridComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { CandlesResponse } from "../api";
import { buildChartOption, toBars, type ZoomRange } from "./options";
import type { ChartSettings } from "./types";

echarts.use([
  CandlestickChart,
  LineChart,
  BarChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  MarkLineComponent,
  AxisPointerComponent,
  CanvasRenderer,
]);

type Props = { data: CandlesResponse | null; settings: ChartSettings };

type ZoomEvent = {
  start?: number;
  end?: number;
  batch?: { start?: number; end?: number }[];
};

export function CandleChart({ data, settings }: Props) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  // 用户缩放位置：轮询刷新时保留，切换周期时重置
  const zoomRef = useRef<ZoomRange | null>(null);
  const keyRef = useRef("");

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const chart = echarts.init(el, undefined, { renderer: "canvas" });
    chartRef.current = chart;

    chart.on("dataZoom", (params: unknown) => {
      const item = (params as ZoomEvent).batch?.[0] ?? (params as ZoomEvent);
      if (typeof item.start === "number" && typeof item.end === "number") {
        zoomRef.current = { start: item.start, end: item.end };
      }
    });

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(el);

    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !data) return;

    const key = `${data.inst}-${data.bar}`;
    if (keyRef.current !== key) {
      zoomRef.current = null;
      keyRef.current = key;
    }

    const bars = toBars(data.bars);
    if (bars.length === 0) {
      chart.clear();
      return;
    }

    chart.setOption(
      buildChartOption({
        bars,
        settings,
        fills: data.fills,
        position: data.position,
        bar: data.bar,
        zoom: zoomRef.current,
      }),
      { notMerge: true, lazyUpdate: true },
    );
  }, [data, settings]);

  // 副图越多主图越容易被压扁，按启用数量撑高画布
  const subCount =
    Number(settings.vol.enabled) + Number(settings.macd.enabled) + Number(settings.kdj.enabled);

  return <div className="chart-canvas" ref={boxRef} style={{ height: 340 + subCount * 120 }} />;
}
