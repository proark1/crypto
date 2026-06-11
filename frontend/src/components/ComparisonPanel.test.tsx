import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ComparisonGroupResponse, EvaluationRunResponse } from "../api/types";
import { ComparisonPanel, strategyLabel } from "./ComparisonPanel";

function makeRun(overrides: Partial<EvaluationRunResponse>): EvaluationRunResponse {
  return {
    id: 1,
    created_at: "2026-06-10T12:00:00+00:00",
    status: "completed",
    symbols: ["BTC/USDT"],
    timeframes: ["1h"],
    progress_done: 10,
    progress_total: 10,
    config: {},
    summary: null,
    strategy: "production",
    comparison_group: 7,
    ...overrides,
  };
}

const GROUP: ComparisonGroupResponse = {
  group_id: 7,
  created_at: "2026-06-10T12:00:00+00:00",
  runs: [
    makeRun({
      id: 1,
      strategy: "production",
      summary: {
        scenario_count: 100,
        trade_count: 40,
        expectancy_r: "0.3100",
        win_rate: "0.5500",
        profit_factor: "1.8000",
        average_win_r: "1.2000",
        average_loss_r: "-0.8000",
        starting_balance_quote: "10000.00",
        final_balance_quote: "10800.00",
        net_pnl_quote: "800.00",
        return_fraction: "0.0800",
        verdicts: {},
      },
    }),
    makeRun({
      id: 2,
      strategy: "trend_following",
      summary: {
        scenario_count: 100,
        trade_count: 30,
        expectancy_r: "0.1000",
        win_rate: "0.4000",
        profit_factor: "1.2000",
        average_win_r: "1.0000",
        average_loss_r: "-0.9000",
        starting_balance_quote: "10000.00",
        final_balance_quote: "10200.00",
        net_pnl_quote: "200.00",
        return_fraction: "0.0200",
        verdicts: {},
      },
    }),
    makeRun({
      id: 3,
      strategy: "breakout",
      status: "running",
      progress_done: 4,
      progress_total: 10,
      summary: null,
    }),
  ],
};

describe("strategyLabel", () => {
  it("humanizes the known bot ids and falls back gracefully", () => {
    expect(strategyLabel("production")).toBe("Regime router");
    expect(strategyLabel("trend_following")).toBe("Trend following");
    expect(strategyLabel("mean_reversion")).toBe("Mean reversion");
    expect(strategyLabel("breakout")).toBe("Breakout");
    expect(strategyLabel("momentum")).toBe("Momentum");
    expect(strategyLabel("some_new_bot")).toBe("some new bot");
  });
});

describe("ComparisonPanel", () => {
  it("renders one column per strategy with humanized headers", () => {
    render(
      <ComparisonPanel groups={[GROUP]} onStart={() => undefined} startDisabled={false} />,
    );
    expect(screen.getByText("Regime router")).toBeTruthy();
    expect(screen.getByText("Trend following")).toBeTruthy();
    expect(screen.getByText("Breakout")).toBeTruthy();
    expect(screen.getByText("0.3100")).toBeTruthy();
    expect(screen.getByText(/55\.0%/)).toBeTruthy(); // win rate as a percentage
  });

  it("leads with the ending balance and ranks the columns by it", () => {
    render(
      <ComparisonPanel groups={[GROUP]} onStart={() => undefined} startDisabled={false} />,
    );
    // The richer strategy ends with more money and takes first place.
    const winner = screen.getByText(/10,800/);
    expect(winner.closest("td")?.className).toContain("emerald");
    expect(screen.getByText(/10,200/)).toBeTruthy();
    expect(screen.getByText(/1st/)).toBeTruthy();
    expect(screen.getByText(/2nd/)).toBeTruthy();
    // Net P/L reads in money with its return percentage alongside.
    expect(screen.getByText(/\+8\.00%/)).toBeTruthy();
  });

  it("marks the best completed value per highlighted metric", () => {
    render(
      <ComparisonPanel groups={[GROUP]} onStart={() => undefined} startDisabled={false} />,
    );
    const bestExpectancy = screen.getByText("0.3100");
    expect(bestExpectancy.closest("td")?.className).toContain("emerald");
    const worseExpectancy = screen.getByText("0.1000");
    expect(worseExpectancy.closest("td")?.className).not.toContain("emerald");
  });

  it("shows progress instead of metrics for a run still in flight", () => {
    render(
      <ComparisonPanel groups={[GROUP]} onStart={() => undefined} startDisabled={false} />,
    );
    expect(screen.getByText(/running · 4\/10/)).toBeTruthy();
    // The running column has no metrics yet: every metric cell is a dash.
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("disables the start button while a batch is in flight", () => {
    const onStart = vi.fn();
    render(<ComparisonPanel groups={[GROUP]} onStart={onStart} startDisabled={true} />);
    const button = screen.getByRole("button", { name: "compare all strategies" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });
});
