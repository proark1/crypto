import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { StatusResponse } from "../api/types";
import { StatusCard } from "./StatusCard";

const FLAT_STATUS: StatusResponse = {
  mode: "paper",
  paused: false,
  symbol: "BTC/USDT",
  exchange_id: "binance",
  quote_currency: "USDT",
  quote_balance: "10000",
  realized_pnl_quote: "0",
  position: null,
  last_candle_close_time: "2026-01-02T00:01:00+00:00",
  mark_price_quote: "67000.50000000",
  equity_quote: "10000",
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
});
