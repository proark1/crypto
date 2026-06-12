import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ImprovementStatusResponse } from "../api/types";
import { ImproverStatusCard } from "./ImproverStatusCard";

const STATUS: ImprovementStatusResponse = {
  enabled: true,
  interval_hours: 12,
  history_days: 365,
  timeframe: "1h",
  last_cycle_started_at: "2026-06-10T00:00:00+00:00",
  last_cycle_finished_at: "2026-06-10T02:00:00+00:00",
  last_outcome: "sweep #4 kept the active configuration (verdict: overfit)",
  next_cycle_at: "2026-06-10T12:00:00+00:00",
};

describe("ImproverStatusCard", () => {
  it("shows the loop's cadence and the last cycle's outcome in its own words", () => {
    render(<ImproverStatusCard status={STATUS} />);
    expect(screen.getByText("automated improver")).toBeDefined();
    expect(screen.getByText("on")).toBeDefined();
    expect(
      screen.getByText("sweep #4 kept the active configuration (verdict: overfit)"),
    ).toBeDefined();
    expect(screen.getByText(/next cycle:/)).toBeDefined();
  });

  it("flags a cycle still in progress instead of looking idle", () => {
    render(
      <ImproverStatusCard
        status={{
          ...STATUS,
          last_cycle_started_at: "2026-06-10T13:00:00+00:00",
          last_outcome: "sweep #5 running on BTC/USDT (7 candidates)",
        }}
      />,
    );
    expect(screen.getByText("cycle in progress")).toBeDefined();
    expect(screen.getByText(/sweep #5 running/)).toBeDefined();
  });

  it("says when the loop has not completed a cycle yet", () => {
    render(
      <ImproverStatusCard
        status={{
          ...STATUS,
          last_cycle_started_at: null,
          last_cycle_finished_at: null,
          last_outcome: null,
          next_cycle_at: null,
        }}
      />,
    );
    expect(screen.getByText(/none yet/)).toBeDefined();
  });

  it("says off — not nothing — when the loop is disabled", () => {
    render(<ImproverStatusCard status={{ ...STATUS, enabled: false }} />);
    expect(screen.getByText("off")).toBeDefined();
    expect(screen.getByText(/not tuning itself/)).toBeDefined();
  });

  it("renders nothing until the status has loaded", () => {
    const { container } = render(<ImproverStatusCard status={null} />);
    expect(container.innerHTML).toBe("");
  });
});
