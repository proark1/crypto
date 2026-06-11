import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EvaluationRunResponse } from "../api/types";
import { RunReport } from "./ResearchScreen";

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

  it("says so when the run has no report yet", () => {
    render(<RunReport run={{ ...RUN, summary: null }} />);
    expect(screen.getByText(/no report yet/)).toBeDefined();
  });
});
