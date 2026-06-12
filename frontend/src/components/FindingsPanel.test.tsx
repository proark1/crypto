import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { FindingResponse } from "../api/types";
import { FindingsPanel } from "./FindingsPanel";

const FINDING: FindingResponse = {
  id: 3,
  run_id: 1,
  pattern: "entries lose money when trend is ranging",
  evidence_scenario_ids: [11, 12, 13],
  affected_count: 3,
  average_r_impact: "-0.4",
  suggestion: "gate entries behind extra confirmation when trend is ranging",
  confidence: "low",
  status: "proposed",
  created_at: "2026-06-10T12:00:00+00:00",
  seen_in_prior_runs: 0,
  first_seen_run_id: null,
};

describe("FindingsPanel", () => {
  it("shows the pattern, impact, and verdict buttons while proposed", () => {
    const onAccept = vi.fn();
    const onReject = vi.fn();
    render(
      <FindingsPanel
        findings={[FINDING]}
        onAccept={onAccept}
        onReject={onReject}
        onReplayEvidence={() => undefined}
      />,
    );
    expect(screen.getByText("entries lose money when trend is ranging")).toBeDefined();
    expect(screen.getByText(/-0\.4R/)).toBeDefined();

    fireEvent.click(screen.getByText("accept"));
    expect(onAccept).toHaveBeenCalledWith(3);
    fireEvent.click(screen.getByText("reject"));
    expect(onReject).toHaveBeenCalledWith(3);
  });

  it("replaces the buttons with the recorded verdict once decided", () => {
    render(
      <FindingsPanel
        findings={[{ ...FINDING, status: "accepted" }]}
        onAccept={() => undefined}
        onReject={() => undefined}
        onReplayEvidence={() => undefined}
      />,
    );
    expect(screen.getByText("accepted")).toBeDefined();
    expect(screen.queryByText("accept")).toBeNull();
    expect(screen.queryByText("reject")).toBeNull();
  });

  it("links evidence scenarios to the replay viewer", () => {
    const onReplay = vi.fn();
    render(
      <FindingsPanel
        findings={[FINDING]}
        onAccept={() => undefined}
        onReject={() => undefined}
        onReplayEvidence={onReplay}
      />,
    );
    fireEvent.click(screen.getByText("#12"));
    expect(onReplay).toHaveBeenCalledWith(12);
  });

  it("labels a first-time pattern and a recurring one differently", () => {
    render(
      <FindingsPanel
        findings={[
          FINDING,
          {
            ...FINDING,
            id: 4,
            pattern: "old wound",
            seen_in_prior_runs: 3,
            first_seen_run_id: 44,
          },
        ]}
        onAccept={() => undefined}
        onReject={() => undefined}
        onReplayEvidence={() => undefined}
      />,
    );
    expect(screen.getByText("new pattern")).toBeDefined();
    expect(screen.getByText("recurred · 4 runs since #44")).toBeDefined();
  });

  it("renders nothing when a run has no findings", () => {
    const { container } = render(
      <FindingsPanel
        findings={[]}
        onAccept={() => undefined}
        onReject={() => undefined}
        onReplayEvidence={() => undefined}
      />,
    );
    expect(container.innerHTML).toBe("");
  });
});
