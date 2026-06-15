import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { TimelineEventResponse } from "../api/types";
import { ResearchTimeline } from "./ResearchTimeline";

const RUN_EVENT: TimelineEventResponse = {
  at: "2026-06-12T10:00:00+00:00",
  kind: "evaluation",
  headline: "run #51 graded production: -0.0412R per trade over 142 trades",
  detail:
    "mined 4 finding(s) (1 accepted, 0 rejected so far); vs run #50: 1 new pattern(s), 2 no longer firing",
  status: "completed",
  strategy: "production",
  run_id: 51,
  sweep_id: null,
  version_id: null,
  expectancy_r: "-0.0412",
  verdict: null,
  new_patterns: ["entries chase moves that are already over"],
  resolved_patterns: ["entries lose money when trend is down"],
  changes: [],
};

const SWEEP_EVENT: TimelineEventResponse = {
  at: "2026-06-12T09:00:00+00:00",
  kind: "sweep",
  headline:
    "sweep #9 on BTC/USDT: validated — trend_filtered_reversion beat the baseline out of sample",
  detail: "motivated by 2 finding(s) · the improvement survived walk-forward",
  status: "completed",
  strategy: null,
  run_id: null,
  sweep_id: 9,
  version_id: null,
  expectancy_r: null,
  verdict: "validated",
  new_patterns: [],
  resolved_patterns: [],
  changes: [],
};

const PROMOTION_EVENT: TimelineEventResponse = {
  at: "2026-06-12T09:05:00+00:00",
  kind: "promotion",
  headline: "settings v7 activated for mean_reversion (from sweep #9)",
  detail: "auto-promoted: the improvement survived walk-forward",
  status: null,
  strategy: "mean_reversion",
  run_id: null,
  sweep_id: 9,
  version_id: 7,
  expectancy_r: null,
  verdict: null,
  new_patterns: [],
  resolved_patterns: [],
  changes: [],
};

describe("ResearchTimeline", () => {
  it("tells the story: runs with their pattern diffs, verdicts, promotions", () => {
    render(
      <ResearchTimeline
        events={[RUN_EVENT, PROMOTION_EVENT, SWEEP_EVENT]}
        onSelectRun={() => undefined}
      />,
    );
    expect(screen.getByText(/run #51 graded production/)).toBeDefined();
    expect(screen.getByText("new: entries chase moves that are already over")).toBeDefined();
    expect(
      screen.getByText("no longer firing: entries lose money when trend is down"),
    ).toBeDefined();
    expect(screen.getByText("validated")).toBeDefined();
    expect(screen.getByText(/settings v7 activated for mean_reversion/)).toBeDefined();
  });

  it("shows what each promotion changed, field by field", () => {
    render(
      <ResearchTimeline
        events={[
          {
            ...PROMOTION_EVENT,
            changes: [
              { field: "fast", before: "12", after: "8" },
              { field: "stop_atr", before: "2.5", after: "2.0" },
            ],
          },
        ]}
        onSelectRun={() => undefined}
      />,
    );
    // The moved field, its old value, and its new value all read out.
    expect(screen.getByText("fast")).toBeDefined();
    expect(screen.getByText("12")).toBeDefined();
    expect(screen.getByText("8")).toBeDefined();
    expect(screen.getByText("stop_atr")).toBeDefined();
  });

  it("links a run event to its report", () => {
    const onSelectRun = vi.fn();
    render(<ResearchTimeline events={[RUN_EVENT]} onSelectRun={onSelectRun} />);
    fireEvent.click(screen.getByText(/run #51 graded production/));
    expect(onSelectRun).toHaveBeenCalledWith(51);
  });

  it("flags failed work instead of hiding it", () => {
    render(
      <ResearchTimeline
        events={[
          {
            ...RUN_EVENT,
            status: "failed",
            headline: "run #52 (production) failed",
            detail: null,
            new_patterns: [],
            resolved_patterns: [],
          },
        ]}
        onSelectRun={() => undefined}
      />,
    );
    expect(screen.getByText("failed")).toBeDefined();
  });

  it("says so when there is no history yet", () => {
    render(<ResearchTimeline events={[]} onSelectRun={() => undefined} />);
    expect(screen.getByText(/nothing yet/)).toBeDefined();
  });
});
