import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type Time,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

import type { CandleResponse, FillResponse } from "../api/types";
import { toChartCandles, toTradeMarkers } from "../lib/chart";

/**
 * Price candles with the bot's own trades marked on them — entries as green
 * arrows below the bar, exits as red arrows above (ARCHITECTURE.md 6.2).
 * Thin wrapper around lightweight-charts; all data mapping lives in
 * lib/chart.ts where it is unit-tested.
 */
export function CandleChart(props: { candles: CandleResponse[]; fills: FillResponse[] }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  useEffect(() => {
    if (containerRef.current === null) {
      return;
    }
    const chart = createChart(containerRef.current, {
      height: 320,
      autoSize: true,
      layout: { background: { color: "#18181b" }, textColor: "#a1a1aa" },
      grid: {
        vertLines: { color: "#27272a" },
        horzLines: { color: "#27272a" },
      },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#34d399",
      downColor: "#f87171",
      borderVisible: false,
      wickUpColor: "#34d399",
      wickDownColor: "#f87171",
    });
    chartRef.current = chart;
    seriesRef.current = series;
    markersRef.current = createSeriesMarkers(series, []);
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      markersRef.current = null;
    };
  }, []);

  useEffect(() => {
    seriesRef.current?.setData(toChartCandles(props.candles));
    markersRef.current?.setMarkers(toTradeMarkers(props.fills));
  }, [props.candles, props.fills]);

  // The container always renders: the chart is created once against it, so
  // hiding it during the empty state would leave the chart never initialized.
  return (
    <section className="relative overflow-hidden rounded-xl border border-zinc-800 bg-zinc-900">
      <div ref={containerRef} className="h-80 w-full" />
      {props.candles.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-zinc-500">
          no candles stored yet
        </div>
      )}
    </section>
  );
}
