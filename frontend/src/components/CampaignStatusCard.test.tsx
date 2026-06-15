import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CampaignStatusResponse } from "../api/types";
import { CampaignStatusCard } from "./CampaignStatusCard";

const RUNNING: CampaignStatusResponse = {
  enabled: true,
  max_rounds: 8,
  max_hours: 6,
  timeframe: "1h",
  campaign: {
    target: "momentum",
    symbol: "BTC/USDT",
    status: "running",
    promotions: 1,
    stop_reason: null,
    holdout_start: "2026-06-13T00:00:00Z",
    started_at: "2026-06-15T00:00:00Z",
    finished_at: null,
    holdout_read: {
      judged: true,
      improved: true,
      explanation:
        "on 1500 untouched holdout candles the campaign moved expectancy from 0.0500R to 0.2000R (an improvement out of sample)",
      start_expectancy_r: "0.05",
      final_expectancy_r: "0.20",
    },
    rounds: [
      {
        index: 0,
        scale: 1,
        sweep_id: 1,
        verdict: "validated",
        winner: "faster_macd",
        promoted_version: 3,
        note: "promoted momentum settings v3 (faster_macd)",
      },
      {
        index: 1,
        scale: 1,
        sweep_id: 2,
        verdict: "overfit",
        winner: "challenger",
        promoted_version: null,
        note: "kept the active configuration (verdict: overfit)",
      },
    ],
  },
};

describe("CampaignStatusCard", () => {
  it("shows the round trail and holdout read for a running campaign", () => {
    render(<CampaignStatusCard status={RUNNING} />);
    expect(screen.getByText("research campaigns")).toBeTruthy();
    expect(screen.getByText("running now…")).toBeTruthy();
    expect(screen.getByText(/promoted momentum settings v3/)).toBeTruthy();
    expect(screen.getByText(/kept the active configuration/)).toBeTruthy();
    expect(screen.getByText(/an improvement out of sample/)).toBeTruthy();
  });

  it("says off when the loop is disabled", () => {
    render(
      <CampaignStatusCard
        status={{
          enabled: false,
          max_rounds: 8,
          max_hours: 6,
          timeframe: "1h",
          campaign: null,
        }}
      />,
    );
    expect(screen.getByText("off")).toBeTruthy();
    expect(screen.getByText(/flip it on in Settings/)).toBeTruthy();
  });
});
