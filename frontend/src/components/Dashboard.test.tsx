import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CompetitionResponse, CompetitorResponse, StatusResponse } from "../api/types";
import { MiniLeaderboard } from "./MiniLeaderboard";
import { PortfolioSummary } from "./PortfolioSummary";
import { StatusPill } from "./StatusPill";

const STATUS: StatusResponse = {
  mode: "paper",
  paused: false,
  protective_stop_quote: null,
  regime: { enabled: true, symbol: "BTC/USDT", label: "trending", reasons: [], reason: null },
  data_health: { healthy: true, reason: null },
  symbol: "BTC/USDT",
  symbols: ["BTC/USDT"],
  exchange_id: "binance",
  quote_currency: "USDT",
  quote_balance: "10000",
  realized_pnl_quote: "0",
  position: null,
  last_candle_close_time: "2026-01-02T00:01:00+00:00",
  mark_price_quote: "67000",
  equity_quote: "10000",
  breakers: { tripped_reason: null, cooldown_until: null, entries_today: 0 },
};

function competitor(overrides: Partial<CompetitorResponse>): CompetitorResponse {
  return {
    bot_id: "production",
    label: "Main bot",
    description: "regime router",
    is_production: true,
    kind: "production",
    paused: false,
    equity_quote: "10500",
    initial_balance_quote: "10000",
    return_fraction: "0.05",
    quote_balance: "10500",
    realized_pnl_quote: "500",
    unrealized_pnl_quote: "0",
    open_positions: 0,
    entry_fills: 3,
    exit_fills: 3,
    breaker_tripped_reason: null,
    ...overrides,
  };
}

const COMPETITION: CompetitionResponse = {
  quote_currency: "USDT",
  competitors: [
    competitor({ bot_id: "production" }),
    competitor({
      bot_id: "breakout",
      label: "Breakout",
      is_production: false,
      kind: "builtin",
      return_fraction: "-0.02",
      equity_quote: "9800",
    }),
  ],
};

describe("StatusPill", () => {
  it("shows the mode and an attention badge when something is wrong", () => {
    render(
      <StatusPill
        status={{
          ...STATUS,
          data_health: { healthy: false, reason: "backfill failed" },
        }}
      />,
    );
    expect(screen.getByText("paper")).toBeDefined();
    expect(screen.getByText("1 alert")).toBeDefined();
  });

  it("shows no attention badge when healthy", () => {
    render(<StatusPill status={STATUS} />);
    expect(screen.queryByText(/alert/)).toBeNull();
  });
});

describe("PortfolioSummary", () => {
  it("reads the production bot's headline numbers", () => {
    render(<PortfolioSummary status={STATUS} competition={COMPETITION} />);
    expect(screen.getByText("Portfolio")).toBeDefined();
    expect(screen.getByText("+5.00%")).toBeDefined(); // return from the main bot
    expect(screen.getByText("10500")).toBeDefined(); // equity
  });
});

describe("MiniLeaderboard", () => {
  it("lists the top bots and links through to the full board", () => {
    const onViewAll = vi.fn();
    const onSelectBot = vi.fn();
    render(
      <MiniLeaderboard
        competition={COMPETITION}
        onViewAll={onViewAll}
        onSelectBot={onSelectBot}
      />,
    );
    expect(screen.getByText("Main bot")).toBeDefined();
    expect(screen.getByText("Breakout")).toBeDefined();
    fireEvent.click(screen.getByText("view all"));
    expect(onViewAll).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText("Breakout"));
    expect(onSelectBot).toHaveBeenCalledWith("breakout");
  });
});
