import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CampaignSnapshotResponse } from "../api/types";
import { CampaignHistoryCard } from "./CampaignHistoryCard";

const PAST: CampaignSnapshotResponse = {
  target: "momentum",
  symbol: "ETH/USDT",
  status: "completed",
  promotions: 2,
  stop_reason: "budget spent: reached the 8-round limit",
  holdout_start: "2026-06-13T00:00:00Z",
  started_at: "2026-06-14T00:00:00Z",
  finished_at: "2026-06-15T00:00:00Z",
  holdout_read: {
    judged: true,
    improved: true,
    explanation: "moved expectancy from 0.05R to 0.20R out of sample",
    start_expectancy_r: "0.05",
    final_expectancy_r: "0.20",
  },
  rounds: [],
};

describe("CampaignHistoryCard", () => {
  it("lists each past campaign with its outcome and holdout read", () => {
    render(<CampaignHistoryCard campaigns={[PAST]} />);
    expect(screen.getByText("past campaigns")).toBeTruthy();
    expect(screen.getByText("momentum")).toBeTruthy();
    expect(screen.getByText("on ETH/USDT")).toBeTruthy();
    expect(screen.getByText("2 promoted")).toBeTruthy();
    expect(screen.getByText(/reached the 8-round limit/)).toBeTruthy();
    expect(screen.getByText(/0.05R to 0.20R out of sample/)).toBeTruthy();
  });

  it("renders nothing when there is no history yet", () => {
    const { container } = render(<CampaignHistoryCard campaigns={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
