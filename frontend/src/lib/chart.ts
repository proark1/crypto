/**
 * Pure mapping from API responses to lightweight-charts inputs, kept out of
 * the component so it is testable without a canvas. parseFloat here is
 * display formatting (chart pixels), not money arithmetic — the strings
 * remain the source of truth everywhere else.
 */

import type { CandlestickData, SeriesMarker, UTCTimestamp } from "lightweight-charts";

import type { CandleResponse, FillResponse } from "../api/types";

function toUtcTimestamp(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;
}

export function toChartCandles(candles: CandleResponse[]): CandlestickData<UTCTimestamp>[] {
  return candles.map((candle) => ({
    time: toUtcTimestamp(candle.open_time),
    open: parseFloat(candle.open_quote),
    high: parseFloat(candle.high_quote),
    low: parseFloat(candle.low_quote),
    close: parseFloat(candle.close_quote),
  }));
}

export function toTradeMarkers(fills: FillResponse[]): SeriesMarker<UTCTimestamp>[] {
  return fills
    .map((fill): SeriesMarker<UTCTimestamp> => {
      const isBuy = fill.side === "buy";
      return {
        time: toUtcTimestamp(fill.filled_at),
        position: isBuy ? "belowBar" : "aboveBar",
        shape: isBuy ? "arrowUp" : "arrowDown",
        color: isBuy ? "#34d399" : "#f87171",
        text: `${fill.side.toUpperCase()} ${fill.quantity_base}`,
      };
    })
    .sort((a, b) => a.time - b.time);
}
