import { describe, expect, it } from "vitest";

import type { CandleResponse, FillResponse, ScenarioReplayResponse } from "../api/types";
import { toChartCandles, toReplayMarkers, toTradeMarkers } from "./chart";

const CANDLE: CandleResponse = {
  open_time: "2026-01-02T00:00:00+00:00",
  open_quote: "100.5",
  high_quote: "101.25",
  low_quote: "99.75",
  close_quote: "100.0",
  volume_base: "2.5",
};

const BUY: FillResponse = {
  id: 1,
  client_order_id: "ord-1",
  symbol: "BTC/USDT",
  side: "buy",
  price_quote: "100",
  quantity_base: "0.05",
  value_quote: "5",
  fee_quote: "0.1",
  filled_at: "2026-01-02T00:05:00+00:00",
};

describe("toChartCandles", () => {
  it("maps ISO times to unix seconds and prices to numbers", () => {
    const [candle] = toChartCandles([CANDLE]);
    expect(candle?.time).toBe(Date.UTC(2026, 0, 2) / 1000);
    expect(candle?.open).toBeCloseTo(100.5);
    expect(candle?.high).toBeCloseTo(101.25);
    expect(candle?.low).toBeCloseTo(99.75);
    expect(candle?.close).toBeCloseTo(100.0);
  });
});

function makeCandle(openTime: string): CandleResponse {
  return { ...CANDLE, open_time: openTime };
}

const REPLAY: ScenarioReplayResponse = {
  scenario: {
    scenario_id: 7,
    run_id: 1,
    symbol: "BTC/USDT",
    timeframe: "1m",
    decision_time: "2026-01-02T00:02:00+00:00",
    scenario_class: "flat",
    trend: "up",
    volatility: "normal",
    events: [],
    decision: "buy",
    verdict: "good",
    r_multiple: "1.2",
    timing: "on_time",
  },
  confidence: 0.8,
  reasons: ["fast EMA crossed above slow EMA"],
  entry_price_quote: "100.1",
  exit_price_quote: "104.9",
  pnl_quote: "4.5",
  mfe_r: "1.5",
  mae_r: "-0.1",
  duration_candles: 2,
  stop_hit: false,
  oracle_r: "1.6",
  window: [makeCandle("2026-01-02T00:00:00+00:00"), makeCandle("2026-01-02T00:01:00+00:00")],
  horizon: [makeCandle("2026-01-02T00:02:00+00:00"), makeCandle("2026-01-02T00:03:00+00:00")],
};

describe("toReplayMarkers", () => {
  it("shows only the decision point while the future is hidden", () => {
    const markers = toReplayMarkers(REPLAY, 0);
    expect(markers).toHaveLength(1);
    expect(markers[0]?.shape).toBe("circle");
    expect(markers[0]?.text).toBe("BUY");
    expect(markers[0]?.time).toBe(Date.UTC(2026, 0, 2, 0, 1) / 1000); // last window candle
  });

  it("reveals the entry with the first candle and the exit only at its candle", () => {
    const oneRevealed = toReplayMarkers(REPLAY, 1);
    expect(oneRevealed.map((marker) => marker.shape)).toEqual(["circle", "arrowUp"]);

    const fullyRevealed = toReplayMarkers(REPLAY, 2);
    expect(fullyRevealed.map((marker) => marker.shape)).toEqual([
      "circle",
      "arrowUp",
      "arrowDown",
    ]);
    // duration_candles = 2 puts the exit on the second horizon candle.
    expect(fullyRevealed[2]?.time).toBe(Date.UTC(2026, 0, 2, 0, 3) / 1000);
    expect(fullyRevealed[2]?.text).toBe("exit 104.9");
  });

  it("draws no trade arrows for a hold decision", () => {
    const hold: ScenarioReplayResponse = {
      ...REPLAY,
      scenario: { ...REPLAY.scenario, decision: "hold", verdict: "correct_hold" },
      entry_price_quote: null,
      exit_price_quote: null,
      duration_candles: null,
    };
    expect(toReplayMarkers(hold, 2)).toHaveLength(1); // just the decision dot
  });
});

describe("toTradeMarkers", () => {
  it("draws buys below the bar and sells above, sorted by time", () => {
    const sell: FillResponse = {
      ...BUY,
      side: "sell",
      filled_at: "2026-01-02T00:01:00+00:00",
    };
    const markers = toTradeMarkers([BUY, sell]);
    expect(markers[0]?.shape).toBe("arrowDown"); // earlier sell first
    expect(markers[0]?.position).toBe("aboveBar");
    expect(markers[1]?.shape).toBe("arrowUp");
    expect(markers[1]?.position).toBe("belowBar");
    expect(markers[1]?.text).toBe("BUY 0.05");
  });
});
