import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { BakeOffJobResponse } from "../api/types";
import { BakeOffPanel, contestantLabel } from "./BakeOffPanel";

function makeJob(overrides: Partial<BakeOffJobResponse>): BakeOffJobResponse {
  return {
    id: 1,
    created_at: "2026-06-13T12:00:00+00:00",
    updated_at: "2026-06-13T12:05:00+00:00",
    status: "completed",
    config: {},
    contestants: ["production", "trend_bold"],
    cells_done: 2,
    cells_total: 2,
    results: {
      ranking: [
        {
          bot_id: "trend_bold",
          average_return_fraction: "0.08",
          cells_scored: 2,
          total_trades: 12,
        },
        {
          bot_id: "production",
          average_return_fraction: "-0.01",
          cells_scored: 2,
          total_trades: 6,
        },
      ],
      cells: [
        {
          timeframe: "1h",
          history_days: 100,
          comparison_group: 50,
          status: "completed",
          results: {
            trend_bold: { return_fraction: "0.10", net_pnl_quote: "1000", trade_count: 8 },
            production: { return_fraction: "-0.02", net_pnl_quote: "-200", trade_count: 4 },
          },
        },
        {
          timeframe: "1d",
          history_days: 10,
          comparison_group: 52,
          status: "insufficient_data",
          results: {},
        },
      ],
    },
    ...overrides,
  };
}

describe("contestantLabel", () => {
  it("names the known presets and humanizes the rest", () => {
    expect(contestantLabel("production")).toBe("Production (baseline)");
    expect(contestantLabel("trend_bold")).toBe("Trend (bold)");
    expect(contestantLabel("future_preset")).toBe("future preset");
  });
});

describe("BakeOffPanel", () => {
  it("invites a run when there are no jobs", () => {
    render(<BakeOffPanel jobs={[]} onStart={() => undefined} startDisabled={false} />);
    expect(screen.getByText(/no bake-off yet/)).toBeTruthy();
  });

  it("ranks contestants best first with their average return", () => {
    render(
      <BakeOffPanel jobs={[makeJob({})]} onStart={() => undefined} startDisabled={false} />,
    );
    const rows = screen.getAllByRole("row");
    // The labels appear in both the leaderboard and the per-cell grid, so
    // match all and assert ordering via the rows instead of a unique lookup.
    expect(screen.getAllByText("Trend (bold)").length).toBeGreaterThan(0);
    expect(screen.getByText("+8.00%")).toBeTruthy(); // the winner's average — unique
    expect(screen.getByText("-1.00%")).toBeTruthy(); // the baseline's average — unique
    // The winner's leaderboard row precedes the baseline's in document order.
    const winnerIndex = rows.findIndex((row) => row.textContent.includes("Trend (bold)"));
    const baselineIndex = rows.findIndex((row) =>
      row.textContent.includes("Production (baseline)"),
    );
    expect(winnerIndex).toBeGreaterThanOrEqual(0);
    expect(winnerIndex).toBeLessThan(baselineIndex);
  });

  it("marks an infeasible cell instead of charging the bots for it", () => {
    render(
      <BakeOffPanel jobs={[makeJob({})]} onStart={() => undefined} startDisabled={false} />,
    );
    // The 1d/10d cell had too little history: its per-cell entries are dashes.
    expect(screen.getByText("1d · 10d")).toBeTruthy();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("shows progress while a bake-off is still running", () => {
    render(
      <BakeOffPanel
        jobs={[makeJob({ status: "running", cells_done: 1, cells_total: 9 })]}
        onStart={() => undefined}
        startDisabled={true}
      />,
    );
    expect(screen.getByText(/running · 1\/9 cells/)).toBeTruthy();
  });

  it("disables the start button and fires onStart otherwise", () => {
    const onStart = vi.fn();
    const { rerender } = render(
      <BakeOffPanel jobs={[]} onStart={onStart} startDisabled={false} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "run bake-off" }));
    expect(onStart).toHaveBeenCalledOnce();

    rerender(<BakeOffPanel jobs={[]} onStart={onStart} startDisabled={true} />);
    expect(screen.getByRole("button", { name: "run bake-off" }).hasAttribute("disabled")).toBe(
      true,
    );
  });
});
