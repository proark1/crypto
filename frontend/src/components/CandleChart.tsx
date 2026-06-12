import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import type { CandleResponse } from "../api/types";
import { toChartCandles } from "../lib/chart";
import { isDarkClassActive } from "../lib/theme";

/** Chart chrome per theme — lightweight-charts is canvas, so Tailwind's
 * `dark:` classes cannot reach it; we mirror the zinc palette by hand. */
function layoutOptions(dark: boolean) {
  return {
    layout: {
      background: { color: dark ? "#18181b" : "#ffffff" },
      textColor: dark ? "#a1a1aa" : "#52525b",
    },
    grid: {
      vertLines: { color: dark ? "#27272a" : "#e4e4e7" },
      horzLines: { color: dark ? "#27272a" : "#e4e4e7" },
    },
  };
}

/** Track the `.dark` class on <html> so the canvas follows the app theme
 * without threading theme props through every screen that shows a chart. */
function useDarkClass(): boolean {
  const [dark, setDark] = useState(isDarkClassActive);
  useEffect(() => {
    // No DOM (SSR/pre-render): nothing to observe, and `document` is absent.
    if (typeof document === "undefined") {
      return;
    }
    const observer = new MutationObserver(() => {
      setDark(isDarkClassActive());
    });
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => {
      observer.disconnect();
    };
  }, []);
  return dark;
}

/**
 * Price candles with markers supplied by the caller — trade fills on the
 * overview, decision/entry/exit points in the scenario replay. Thin wrapper
 * around lightweight-charts; all data mapping lives in lib/chart.ts where it
 * is unit-tested.
 */
export function CandleChart(props: {
  candles: CandleResponse[];
  markers: SeriesMarker<UTCTimestamp>[];
  emptyText?: string;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const dark = useDarkClass();

  useEffect(() => {
    if (containerRef.current === null) {
      return;
    }
    const chart = createChart(containerRef.current, {
      height: 320,
      autoSize: true,
      ...layoutOptions(document.documentElement.classList.contains("dark")),
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
    chartRef.current?.applyOptions(layoutOptions(dark));
  }, [dark]);

  useEffect(() => {
    seriesRef.current?.setData(toChartCandles(props.candles));
    markersRef.current?.setMarkers(props.markers);
  }, [props.candles, props.markers]);

  // The container always renders: the chart is created once against it, so
  // hiding it during the empty state would leave the chart never initialized.
  return (
    <section className="relative overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <div ref={containerRef} className="h-80 w-full" />
      {props.candles.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-zinc-500">
          {props.emptyText ?? "no candles stored yet"}
        </div>
      )}
    </section>
  );
}
