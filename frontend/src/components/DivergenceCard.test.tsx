import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { DivergenceReportResponse } from "../api/types";
import { DivergenceCard } from "./DivergenceCard";

const REPORT: DivergenceReportResponse = {
  window_start: "2026-01-02T00:00:00+00:00",
  window_end: "2026-01-03T00:00:00+00:00",
  live_fill_count: 2,
  replay_fill_count: 2,
  matched_count: 1,
  divergence_fraction: 0.5,
  mismatches: ["live buy at 10:00 was not replayed"],
};

describe("DivergenceCard", () => {
  it("shows the paper-vs-replay drift metric", () => {
    render(<DivergenceCard report={REPORT} />);
    expect(screen.getByText("paper vs replay")).toBeTruthy();
    expect(screen.getByText("drift")).toBeTruthy();
    expect(screen.getByText("50.00%")).toBeTruthy();
    expect(screen.getByText(/not replayed/)).toBeTruthy();
  });

  it("renders nothing until the report is loaded", () => {
    const { container } = render(<DivergenceCard report={null} />);
    expect(container.firstChild).toBeNull();
  });
});
