import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  BakeOffJobResponse,
  ImprovementStatusResponse,
  RoutingCandidacyResponse,
} from "../api/types";
import { ResearchHome } from "./ResearchHome";

const BAKEOFF: BakeOffJobResponse = {
  id: 1,
  created_at: "2026-06-10T12:00:00+00:00",
  updated_at: "2026-06-10T12:30:00+00:00",
  status: "completed",
  config: {},
  contestants: ["trend_calm", "breakout_bold"],
  cells_done: 9,
  cells_total: 9,
  results: {
    ranking: [
      {
        bot_id: "trend_calm",
        average_return_fraction: "0.034",
        cells_scored: 9,
        total_trades: 120,
      },
      {
        bot_id: "breakout_bold",
        average_return_fraction: "-0.012",
        cells_scored: 9,
        total_trades: 90,
      },
    ],
    cells: [],
  },
};

const IMPROVER: ImprovementStatusResponse = {
  enabled: true,
  interval_hours: 12,
  history_days: 365,
  timeframe: "1h",
  last_cycle_started_at: null,
  last_cycle_finished_at: null,
  last_outcome: "promoted breakout",
  next_cycle_at: null,
};

function candidacy(family: string, isCandidate: boolean): RoutingCandidacyResponse {
  const condition = { met: isCandidate, detail: "" };
  return {
    family,
    is_candidate: isCandidate,
    validated_edge: condition,
    beats_incumbent: condition,
    live_paper: condition,
  };
}

describe("ResearchHome", () => {
  it("summarizes the three signals and deep-links into their tabs", () => {
    const onNavigate = vi.fn();
    render(
      <ResearchHome
        bakeOffs={[BAKEOFF]}
        improver={IMPROVER}
        candidacies={[candidacy("breakout", true), candidacy("squeeze", false)]}
        onNavigate={onNavigate}
      />,
    );

    // Tournament: leader from the latest bake-off's ranking, by plain-words name.
    expect(screen.getByText(/Trend \(calm\)/)).toBeDefined();
    // Improver: on, with its last outcome.
    expect(screen.getByText(/last: promoted breakout/)).toBeDefined();
    // Candidacy: how many families are flagged.
    expect(screen.getByText(/of 2 families flagged/)).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: /Tournament/ }));
    expect(onNavigate).toHaveBeenCalledWith("bakeoff");
    fireEvent.click(screen.getByRole("button", { name: /Auto-improve/ }));
    expect(onNavigate).toHaveBeenCalledWith("tune");
    fireEvent.click(screen.getByRole("button", { name: /Routing candidates/ }));
    expect(onNavigate).toHaveBeenCalledWith("compare");
  });

  it("stays quiet when there is no data yet", () => {
    render(
      <ResearchHome bakeOffs={[]} improver={null} candidacies={[]} onNavigate={vi.fn()} />,
    );
    expect(screen.getByText(/no bake-off yet/)).toBeDefined();
    expect(screen.getByText(/no families assessed yet/)).toBeDefined();
  });
});
