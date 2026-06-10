import { describe, expect, it } from "vitest";

import type { CandleResponse, FillResponse } from "../api/types";
import { toChartCandles, toTradeMarkers } from "./chart";

const CANDLE: CandleResponse = {
  open_time: "2026-01-02T00:00:00+00:00",
  open_quote: "100.5",
  high_quote: "101.25",
  low_quote: "99.75",
  close_quote: "100.0",
  volume_base: "2.5",
};

const BUY: FillResponse = {
  client_order_id: "ord-1",
  symbol: "BTC/USDT",
  side: "buy",
  price_quote: "100",
  quantity_base: "0.05",
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
