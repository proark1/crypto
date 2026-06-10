import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { DecisionResponse } from "../api/types";
import { DecisionsPanel } from "./DecisionsPanel";

const VETOED: DecisionResponse = {
  signal_id: "sig-1",
  strategy_name: "trend_following",
  symbol: "BTC/USDT",
  side: "buy",
  stop_price_quote: "95.50000000",
  reasons: ["fast EMA(20) crossed above slow EMA(50)", "stop at 2.0 x ATR below close"],
  outcome: "vetoed",
  created_at: "2026-01-02T00:01:00+00:00",
};

describe("DecisionsPanel", () => {
  it("shows the outcome badge and every reason verbatim", () => {
    render(<DecisionsPanel decisions={[VETOED]} />);
    expect(screen.getByText("vetoed")).toBeDefined();
    expect(screen.getByText(/fast EMA\(20\) crossed above/)).toBeDefined();
    expect(screen.getByText(/2\.0 x ATR/)).toBeDefined();
    expect(screen.getByText(/stop 95\.5/)).toBeDefined(); // trimmed, not rounded
  });

  it("explains silence when there are no decisions", () => {
    render(<DecisionsPanel decisions={[]} />);
    expect(screen.getByText(/no signals yet/)).toBeDefined();
  });
});
