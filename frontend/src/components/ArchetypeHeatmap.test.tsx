import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ComparisonGroupResponse, EvaluationRunResponse } from "../api/types";
import { ArchetypeHeatmap } from "./ArchetypeHeatmap";

function makeRun(overrides: Partial<EvaluationRunResponse>): EvaluationRunResponse {
  return {
    id: 1,
    created_at: "2026-06-13T12:00:00+00:00",
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
  created_at: "2026-06-13T12:00:00+00:00",
  runs: [
    makeRun({
      id: 1,
      strategy: "production",
      summary: {
        by_archetype: {
          bull: { expectancy_r: "0.40", trade_count: 12 },
          chop: { expectancy_r: "-0.10", trade_count: 8 },
        },
      },
    }),
    makeRun({
      id: 2,
      strategy: "trend_following",
      summary: {
        by_archetype: {
          bull: { expectancy_r: "0.20", trade_count: 10 },
          chop: { expectancy_r: "-0.30", trade_count: 6 },
        },
      },
    }),
    makeRun({ id: 3, strategy: "breakout", status: "running", summary: null }),
  ],
};

describe("ArchetypeHeatmap", () => {
  it("pivots the comparison into a bot × archetype grid", () => {
    render(<ArchetypeHeatmap group={GROUP} />);
    // Only the archetypes with data show as columns.
    expect(screen.getByText("bull")).toBeTruthy();
    expect(screen.getByText("chop")).toBeTruthy();
    expect(screen.queryByText("coil")).toBeNull(); // compression had no trades
    // Rows are the humanized bot labels; cells are expectancy to 2 dp.
    expect(screen.getByText("Regime router")).toBeTruthy();
    expect(screen.getByText("Trend following")).toBeTruthy();
    expect(screen.getByText("0.40")).toBeTruthy();
    expect(screen.getByText("-0.30")).toBeTruthy();
  });

  it("rings the best bot in each archetype", () => {
    render(<ArchetypeHeatmap group={GROUP} />);
    // Production wins bull (0.40 > 0.20) — its cell carries the winner ring.
    expect(screen.getByText("0.40").className).toContain("ring-emerald");
    expect(screen.getByText("0.20").className).not.toContain("ring-emerald");
  });

  it("renders nothing when no run has graded any archetype yet", () => {
    const empty: ComparisonGroupResponse = {
      ...GROUP,
      runs: [makeRun({ id: 9, status: "running", summary: null })],
    };
    const { container } = render(<ArchetypeHeatmap group={empty} />);
    expect(container.firstChild).toBeNull();
  });
});
