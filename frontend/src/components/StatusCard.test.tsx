import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { StatusResponse } from "../api/types";
import { StatusCard } from "./StatusCard";

const FLAT_STATUS: StatusResponse = {
  mode: "paper",
  paused: false,
  protective_stop_quote: null,
  regime: {
    enabled: true,
    symbol: "BTC/USDT",
    label: "trending",
    reasons: ["ADX 31 above 25"],
  },
  symbol: "BTC/USDT",
  symbols: ["BTC/USDT"],
  exchange_id: "binance",
  quote_currency: "USDT",
  quote_balance: "10000",
  realized_pnl_quote: "0",
  position: null,
  last_candle_close_time: "2026-01-02T00:01:00+00:00",
  mark_price_quote: "67000.50000000",
  equity_quote: "10000",
  breakers: { tripped_reason: null, cooldown_until: null, entries_today: 0 },
};

describe("StatusCard", () => {
  it("shows the paper-mode badge and flat state", () => {
    render(<StatusCard status={FLAT_STATUS} />);
    expect(screen.getByText("paper")).toBeDefined();
    expect(screen.getByText(/no open position/)).toBeDefined();
    expect(screen.getByText("67000.5")).toBeDefined(); // trailing zeros trimmed
  });

  it("shows the paused badge when paused", () => {
    render(<StatusCard status={{ ...FLAT_STATUS, paused: true }} />);
    expect(screen.getByText("paused")).toBeDefined();
  });

  it("refuses to invent equity when the backend reports null", () => {
    render(<StatusCard status={{ ...FLAT_STATUS, equity_quote: null }} />);
    expect(screen.getByText("unknown")).toBeDefined();
  });

  it("shows a tripped circuit breaker prominently", () => {
    render(
      <StatusCard
        status={{
          ...FLAT_STATUS,
          breakers: {
            tripped_reason: "daily loss limit: equity fell below day start",
            cooldown_until: null,
            entries_today: 2,
          },
        }}
      />,
    );
    expect(screen.getByText("circuit breaker tripped")).toBeDefined();
    expect(screen.getByText(/daily loss limit/)).toBeDefined();
  });

  it("shows the loss-streak cooldown when not tripped", () => {
    render(
      <StatusCard
        status={{
          ...FLAT_STATUS,
          breakers: {
            tripped_reason: null,
            cooldown_until: "2026-01-02T04:00:00+00:00",
            entries_today: 3,
          },
        }}
      />,
    );
    expect(screen.getByText("loss-streak cooldown")).toBeDefined();
  });
});
