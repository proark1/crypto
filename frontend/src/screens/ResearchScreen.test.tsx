import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  fetchComparisons,
  fetchEvaluations,
  fetchEvaluationStrategies,
  fetchEvaluationSuggestions,
  fetchImprovementStatus,
  fetchStrategyVersions,
  fetchSweeps,
  startEvaluation,
} from "../api/client";
import type { EvaluationRunResponse } from "../api/types";
import { ResearchScreen, RunReport } from "./ResearchScreen";

// The poll fetches reject; everything else is an unused stub. RunReport is a
// pure component and never touches the client, so mocking it here is harmless.
vi.mock("../api/client", () => {
  const rejecting = () => vi.fn().mockRejectedValue(new Error("offline"));
  return {
    fetchEvaluations: rejecting(),
    fetchSweeps: rejecting(),
    fetchStrategyVersions: rejecting(),
    fetchComparisons: rejecting(),
    fetchEvaluationStrategies: rejecting(),
    fetchEvaluationSuggestions: rejecting(),
    fetchImprovementStatus: rejecting(),
    fetchResearchTimeline: rejecting(),
    fetchScenarios: rejecting(),
    fetchFindings: rejecting(),
    fetchScenarioReplay: rejecting(),
    acceptFinding: vi.fn(),
    cancelEvaluation: vi.fn(),
    cancelSweep: vi.fn(),
    rejectFinding: vi.fn(),
    revertStrategyVersion: vi.fn(),
    startComparison: vi.fn(),
    startEvaluation: vi.fn(),
    startSweep: vi.fn(),
  };
});

const RUN: EvaluationRunResponse = {
  id: 1,
  created_at: "2026-06-10T12:00:00+00:00",
  status: "completed",
  symbols: ["BTC/USDT"],
  timeframes: ["1h"],
  progress_done: 10,
  progress_total: 10,
  config: {},
  strategy: "production",
  comparison_group: null,
  summary: {
    scenario_count: 10,
    trade_count: 4,
    expectancy_r: "0.3100",
    profit_factor: "1.8000",
    win_rate: "0.5000",
    starting_balance_quote: "10000.00",
    final_balance_quote: "10450.00",
    net_pnl_quote: "450.00",
    return_fraction: "0.0450",
    verdicts: { good: 2, very_bad: 1, correct_hold: 7 },
    by_trend: {
      up: { scenario_count: 6, trade_count: 3, expectancy_r: "0.5", win_rate: "0.66" },
    },
  },
};

describe("RunReport", () => {
  it("leads with expectancy and shows verdicts and breakdowns", () => {
    render(<RunReport run={RUN} />);
    expect(screen.getAllByText("expectancy (R)").length).toBeGreaterThan(0);
    expect(screen.getByText("0.3100")).toBeDefined();
    expect(screen.getByText(/very bad: 1/)).toBeDefined();
    expect(screen.getByText("by trend")).toBeDefined();
  });

  it("shows the start and end value of the stake", () => {
    render(<RunReport run={RUN} />);
    expect(screen.getByText("starting value")).toBeDefined();
    expect(screen.getByText("ending value")).toBeDefined();
    expect(screen.getByText("10,450")).toBeDefined();
    expect(screen.getByText(/\+4\.50%/)).toBeDefined();
  });

  it("offers a tap-friendly definition for the jargon metrics", () => {
    render(<RunReport run={RUN} />);
    // The expectancy metric carries a glossary tooltip explaining R.
    expect(
      screen.getByRole("button", { name: /average R won or lost per trade/ }),
    ).toBeDefined();
  });

  it("says so when the run has no report yet", () => {
    render(<RunReport run={{ ...RUN, summary: null }} />);
    expect(screen.getByText(/no report yet/)).toBeDefined();
  });
});

describe("ResearchScreen", () => {
  it("surfaces a stale banner when polling fails instead of looking fresh", async () => {
    render(<ResearchScreen />);
    // The first poll rejects; rather than swallowing it silently, the screen
    // tells the user its data is no longer refreshing.
    await waitFor(() => {
      expect(screen.getByText("not refreshing")).toBeDefined();
    });
  });

  it("splits the workspace into Evaluate / Compare / Tune sub-tabs", async () => {
    render(<ResearchScreen />);
    await waitFor(() => {
      expect(screen.getByText("not refreshing")).toBeDefined();
    });
    // The Evaluate tab is the default and shows the custom-evaluation form.
    expect(screen.getByRole("button", { name: "start evaluation" })).toBeDefined();
    // Switching to Compare hides the evaluation form.
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(screen.queryByRole("button", { name: "start evaluation" })).toBeNull();
    // And back again brings it into view.
    fireEvent.click(screen.getByRole("button", { name: "Evaluate" }));
    expect(screen.getByRole("button", { name: "start evaluation" })).toBeDefined();
  });

  it("offers every gradeable bot and submits the chosen one", async () => {
    // The selector loads with the poll, so the whole chain must succeed here.
    vi.mocked(fetchEvaluations).mockResolvedValue([]);
    vi.mocked(fetchSweeps).mockResolvedValue([]);
    vi.mocked(fetchStrategyVersions).mockResolvedValue([]);
    vi.mocked(fetchComparisons).mockResolvedValue([]);
    vi.mocked(fetchEvaluationSuggestions).mockResolvedValue([]);
    vi.mocked(fetchImprovementStatus).mockResolvedValue({
      enabled: false,
      interval_hours: 12,
      history_days: 365,
      timeframe: "1h",
      last_cycle_started_at: null,
      last_cycle_finished_at: null,
      last_outcome: null,
      next_cycle_at: null,
    });
    vi.mocked(fetchEvaluationStrategies).mockResolvedValue([
      {
        id: "production",
        label: "Regime router",
        description: "the incumbent",
        kind: "production",
      },
      {
        id: "breakout",
        label: "Breakout",
        description: "Donchian-channel entries",
        kind: "builtin",
      },
    ]);
    vi.mocked(startEvaluation).mockResolvedValue({ run_id: 7, detail: "evaluation started" });
    render(<ResearchScreen />);

    // Options arrive from the backend, never hardcoded in the frontend.
    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Breakout" })).toBeDefined();
    });
    fireEvent.change(screen.getByRole("combobox", { name: /whose strategy the run grades/ }), {
      target: { value: "breakout" },
    });
    fireEvent.click(screen.getByRole("button", { name: "start evaluation" }));

    await waitFor(() => {
      expect(vi.mocked(startEvaluation)).toHaveBeenCalledWith(
        expect.objectContaining({ strategy: "breakout" }),
      );
    });
  });
});
