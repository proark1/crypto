/**
 * Pure mapping from API responses to lightweight-charts inputs, kept out of
 * the component so it is testable without a canvas. parseFloat here is
 * display formatting (chart pixels), not money arithmetic — the strings
 * remain the source of truth everywhere else.
 */

import type { CandlestickData, SeriesMarker, UTCTimestamp } from "lightweight-charts";

import type { CandleResponse, FillResponse, ScenarioReplayResponse } from "../api/types";

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

/**
 * Markers for the scenario replay viewer: the decision point on the last
 * window candle, plus entry/exit arrows that only appear once the candle
 * they happened on has been revealed — the future must not leak through
 * the markers before the user asks for it.
 */
export function toReplayMarkers(
  replay: ScenarioReplayResponse,
  revealedCount: number,
): SeriesMarker<UTCTimestamp>[] {
  const markers: SeriesMarker<UTCTimestamp>[] = [];
  const decisionCandle = replay.window[replay.window.length - 1];
  if (decisionCandle !== undefined) {
    markers.push({
      time: toUtcTimestamp(decisionCandle.open_time),
      position: "aboveBar",
      shape: "circle",
      color: "#fbbf24",
      text: replay.scenario.decision.toUpperCase(),
    });
  }
  // A buy decision fills at the first horizon candle's open; sell and hold
  // decisions have no entry inside the revealed future.
  const entryCandle = replay.horizon[0];
  if (
    replay.scenario.decision === "buy" &&
    replay.entry_price_quote !== null &&
    revealedCount >= 1 &&
    entryCandle !== undefined
  ) {
    markers.push({
      time: toUtcTimestamp(entryCandle.open_time),
      position: "belowBar",
      shape: "arrowUp",
      color: "#34d399",
      text: `entry ${replay.entry_price_quote}`,
    });
  }
  // duration_candles counts candles to the exit, so the exit sits on
  // horizon[duration - 1] (a sell decision exits on the first one).
  const exitIndex = replay.duration_candles === null ? null : replay.duration_candles - 1;
  const exitCandle = exitIndex === null ? undefined : replay.horizon[exitIndex];
  if (
    replay.exit_price_quote !== null &&
    exitIndex !== null &&
    revealedCount >= exitIndex + 1 &&
    exitCandle !== undefined
  ) {
    markers.push({
      time: toUtcTimestamp(exitCandle.open_time),
      position: "aboveBar",
      shape: "arrowDown",
      color: "#f87171",
      text: `exit ${replay.exit_price_quote}`,
    });
  }
  return markers;
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
