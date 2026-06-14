import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CandleResponse, ScenarioReplayResponse } from "../api/types";
import { ScenarioReplay } from "./ScenarioReplay";

// lightweight-charts needs a real canvas; the reveal logic under test does not.
vi.mock("./CandleChart", () => ({
  CandleChart: (props: { candles: CandleResponse[] }) => (
    <div data-testid="chart">{props.candles.length} candles</div>
  ),
}));

function makeCandle(openTime: string): CandleResponse {
  return {
    open_time: openTime,
    open_quote: "100",
    high_quote: "101",
    low_quote: "99",
    close_quote: "100.5",
    volume_base: "1",
  };
}

const REPLAY: ScenarioReplayResponse = {
  scenario: {
    scenario_id: 7,
    run_id: 1,
    symbol: "BTC/USDT",
    timeframe: "1h",
    decision_time: "2026-01-02T02:00:00+00:00",
    scenario_class: "flat",
    trend: "up",
    volatility: "high",
    events: ["breakout_real"],
    decision: "buy",
    verdict: "excellent",
    r_multiple: "1.85",
    timing: "on_time",
  },
  confidence: 0.8,
  reasons: ["fast EMA crossed above slow EMA"],
  entry_price_quote: "100.1",
  exit_price_quote: "104.9",
  pnl_quote: "4.5",
  mfe_r: "2.1",
  mae_r: "-0.1",
  duration_candles: 2,
  stop_hit: false,
  oracle_r: "2.2",
  window: [makeCandle("2026-01-02T00:00:00+00:00"), makeCandle("2026-01-02T01:00:00+00:00")],
  horizon: [makeCandle("2026-01-02T02:00:00+00:00"), makeCandle("2026-01-02T03:00:00+00:00")],
};

describe("ScenarioReplay", () => {
  it("starts blind: decision and reasons shown, grade hidden", () => {
    render(<ScenarioReplay replay={REPLAY} onBack={() => undefined} />);
    expect(screen.getByText("BUY")).toBeDefined();
    expect(screen.getByText("fast EMA crossed above slow EMA")).toBeDefined();
    expect(screen.getByText("2 candles")).toBeDefined(); // window only
    expect(screen.getByText(/grade is hidden/)).toBeDefined();
    expect(screen.queryByText("excellent")).toBeNull();
  });

  it("reveals candle by candle and grades only at the end", () => {
    render(<ScenarioReplay replay={REPLAY} onBack={() => undefined} />);
    fireEvent.click(screen.getByText("reveal next candle"));
    expect(screen.getByText("3 candles")).toBeDefined();
    expect(screen.getByText(/grade is hidden/)).toBeDefined();

    fireEvent.click(screen.getByText("reveal next candle"));
    expect(screen.getByText("4 candles")).toBeDefined();
    expect(screen.getByText("excellent")).toBeDefined();
    expect(screen.getByText("1.85")).toBeDefined();
    expect(screen.getByText("2 / 2 future candles revealed")).toBeDefined();
  });

  it("reveal all jumps to the grade and hide resets to blind", () => {
    render(<ScenarioReplay replay={REPLAY} onBack={() => undefined} />);
    fireEvent.click(screen.getByText("reveal all"));
    expect(screen.getByText("excellent")).toBeDefined();

    fireEvent.click(screen.getByText("hide the future again"));
    expect(screen.getByText("2 candles")).toBeDefined();
    expect(screen.queryByText("excellent")).toBeNull();
  });

  it("never flashes the grade when the horizon is empty", () => {
    const noHorizon: ScenarioReplayResponse = { ...REPLAY, horizon: [] };
    render(<ScenarioReplay replay={noHorizon} onBack={() => undefined} />);
    // 0 / 0 revealed must NOT count as fully revealed: the grade stays hidden
    // and the reveal buttons are disabled (nothing to reveal).
    expect(screen.getByText(/grade is hidden/)).toBeDefined();
    expect(screen.queryByText("excellent")).toBeNull();
    expect(screen.getByText("reveal next candle").closest("button")?.disabled).toBe(true);
    expect(screen.getByText("reveal all").closest("button")?.disabled).toBe(true);
  });

  it("calls onBack from the back button", () => {
    const onBack = vi.fn();
    render(<ScenarioReplay replay={REPLAY} onBack={onBack} />);
    fireEvent.click(screen.getByText("← back to run"));
    expect(onBack).toHaveBeenCalledOnce();
  });
});
