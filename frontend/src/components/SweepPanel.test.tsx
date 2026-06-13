import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SweepResponse } from "../api/types";
import { SweepPanel, SweepReport } from "./SweepPanel";

const SWEEP: SweepResponse = {
  id: 5,
  created_at: "2026-06-10T12:00:00+00:00",
  status: "completed",
  symbol: "BTC/USDT",
  timeframe: "1h",
  config: {},
  motivating_finding_ids: [],
  report: {
    baseline: "baseline_20_50",
    winner: "faster_cross_10_30",
    verdict: "overfit",
    explanation:
      "faster_cross_10_30 won the training period but not the untouched validation period; " +
      "it wins only on the data it was tuned on — keep baseline_20_50",
    training: {
      baseline_20_50: { trade_count: 30, expectancy_r: "0.1000", win_rate: "0.5000" },
      faster_cross_10_30: { trade_count: 40, expectancy_r: "0.4000", win_rate: "0.6000" },
    },
    validation: {
      baseline_20_50: { trade_count: 12, expectancy_r: "0.2000", win_rate: "0.5000" },
      faster_cross_10_30: { trade_count: 15, expectancy_r: "-0.3000", win_rate: "0.3000" },
    },
  },
};

describe("SweepReport", () => {
  it("leads with the verdict in plain words and shows both periods", () => {
    render(<SweepReport sweep={SWEEP} />);
    expect(screen.getByText("overfit")).toBeDefined();
    expect(screen.getByText(/wins only on the data it was tuned on/)).toBeDefined();
    expect(screen.getByText("training period")).toBeDefined();
    expect(screen.getByText("validation period (untouched)")).toBeDefined();
    expect(screen.getByText("0.4000")).toBeDefined(); // training winner
    expect(screen.getByText("-0.3000")).toBeDefined(); // its validation collapse
  });

  it("says so when the sweep has no report yet", () => {
    render(<SweepReport sweep={{ ...SWEEP, report: null }} />);
    expect(screen.getByText(/no report yet/)).toBeDefined();
  });

  it("shows the cost-sensitivity read when present and flags a fragile edge", () => {
    const withCosts: SweepResponse = {
      ...SWEEP,
      report: {
        ...SWEEP.report,
        cost_sensitivity: {
          points: [
            {
              multiplier: "1",
              trade_count: 15,
              expectancy_r: "0.2000",
              return_fraction: "0.0300",
            },
            {
              multiplier: "2",
              trade_count: 15,
              expectancy_r: "-0.1000",
              return_fraction: "-0.0150",
            },
          ],
          survives_worse_costs: false,
        },
      },
    };
    render(<SweepReport sweep={withCosts} />);
    expect(screen.getByText(/cost sensitivity/i)).toBeDefined();
    expect(screen.getByText(/fragile to fees and slippage/)).toBeDefined();
  });

  it("omits the cost-sensitivity block when the sweep did not compute one", () => {
    render(<SweepReport sweep={SWEEP} />);
    expect(screen.queryByText(/cost sensitivity/i)).toBeNull();
  });
});

describe("SweepPanel", () => {
  it("starts a sweep and cancels a running one", () => {
    const onStart = vi.fn();
    const onCancel = vi.fn();
    render(
      <SweepPanel
        sweeps={[{ ...SWEEP, status: "running", report: null }]}
        onStart={onStart}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByText("run sweep"));
    expect(onStart).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByText("cancel"));
    expect(onCancel).toHaveBeenCalledWith(5);
  });
});
