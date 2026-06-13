import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RoutingCandidacyResponse } from "../api/types";
import { RoutingCandidacyPanel } from "./RoutingCandidacyPanel";

function candidacy(overrides: Partial<RoutingCandidacyResponse>): RoutingCandidacyResponse {
  return {
    family: "breakout",
    is_candidate: false,
    validated_edge: { met: false, detail: "no validated sweep yet" },
    beats_incumbent: { met: false, detail: "beat the incumbent in 0 of 2 batches" },
    live_paper: { met: false, detail: "only 0 of 8 weeks of live paper" },
    ...overrides,
  };
}

describe("RoutingCandidacyPanel", () => {
  it("flags a met candidacy and lists the conditions", () => {
    const met = candidacy({
      family: "breakout",
      is_candidate: true,
      validated_edge: { met: true, detail: "validated edge concentrated in breakout (+0.40R)" },
      beats_incumbent: {
        met: true,
        detail: "beat the incumbent in 2 batches spanning 4 weeks",
      },
      live_paper: {
        met: true,
        detail: "positive over 9 weeks of live paper with no breaker trips",
      },
    });
    render(<RoutingCandidacyPanel candidacies={[met]} />);

    expect(screen.getByText("Breakout")).toBeTruthy();
    expect(screen.getByText("candidacy met")).toBeTruthy();
    expect(screen.getByText(/concentrated in breakout/)).toBeTruthy();
  });

  it("shows building-evidence families with the reason each condition fails", () => {
    render(<RoutingCandidacyPanel candidacies={[candidacy({ family: "momentum" })]} />);

    expect(screen.getByText("Momentum")).toBeTruthy();
    expect(screen.getByText("building evidence")).toBeTruthy();
    expect(screen.getByText(/no validated sweep yet/)).toBeTruthy();
    expect(screen.getByText(/0 of 8 weeks/)).toBeTruthy();
  });

  it("always frames itself as flag-only (routing stays human)", () => {
    render(<RoutingCandidacyPanel candidacies={[candidacy({})]} />);
    expect(screen.getByText(/stays a human/)).toBeTruthy();
  });

  it("says so when there is nothing to assess", () => {
    render(<RoutingCandidacyPanel candidacies={[]} />);
    expect(screen.getByText(/nothing|no research families/i)).toBeTruthy();
  });
});
