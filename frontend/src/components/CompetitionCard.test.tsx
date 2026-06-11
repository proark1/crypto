import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CompetitionResponse, CompetitorResponse } from "../api/types";
import { CompetitionCard } from "./CompetitionCard";

const PRODUCTION: CompetitorResponse = {
  bot_id: "production",
  label: "Regime router",
  description: "picks a strategy to match the market's mood",
  is_production: true,
  equity_quote: "10123.450000000000000000000000",
  initial_balance_quote: "10000",
  return_fraction: "0.0123",
  quote_balance: "8000.12",
  realized_pnl_quote: "98.7",
  unrealized_pnl_quote: "24.75",
  open_positions: 1,
  entry_fills: 3,
  exit_fills: 2,
  breaker_tripped_reason: null,
};

const CHALLENGER: CompetitorResponse = {
  bot_id: "breakout",
  label: "Breakout",
  description: "buys when price escapes a tight range",
  is_production: false,
  equity_quote: null,
  initial_balance_quote: "10000",
  return_fraction: null,
  quote_balance: "9500",
  realized_pnl_quote: "-42.5",
  unrealized_pnl_quote: null,
  open_positions: 0,
  entry_fills: 5,
  exit_fills: 5,
  breaker_tripped_reason: "daily loss limit reached",
};

const COMPETITION: CompetitionResponse = {
  quote_currency: "USDT",
  competitors: [PRODUCTION, CHALLENGER],
};

describe("CompetitionCard", () => {
  it("renders nothing until the first snapshot arrives", () => {
    const { container } = render(<CompetitionCard competition={null} />);
    expect(container.innerHTML).toBe("");
  });

  it("ranks competitors in backend order with equity, return, and trades", () => {
    render(<CompetitionCard competition={COMPETITION} />);
    expect(screen.getByText("Regime router")).toBeTruthy();
    expect(screen.getByText("production")).toBeTruthy(); // the badge
    expect(screen.getByText("10123.45")).toBeTruthy(); // equity, truncated
    const gain = screen.getByText("+1.23%");
    expect(gain.className).toContain("emerald");
    expect(screen.getByText("98.7")).toBeTruthy(); // realized pnl
    expect(screen.getByText(/\(3 entries\)/)).toBeTruthy(); // entries beside round trips
  });

  it("shows muted dashes for unknown amounts and a breaker warning", () => {
    render(<CompetitionCard competition={COMPETITION} />);
    // The challenger has no equity and no return: both render as a dash.
    expect(screen.getAllByText("—").length).toBe(2);
    const loss = screen.getByText("-42.5");
    expect(loss.className).toContain("red");
    expect(screen.getByTitle(/daily loss limit reached/)).toBeTruthy();
  });
});
