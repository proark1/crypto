import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FillResponse } from "../api/types";
import { FillsTable } from "./FillsTable";

function makeFill(overrides: Partial<FillResponse>): FillResponse {
  return {
    client_order_id: "ord-1",
    symbol: "BTC/USDT",
    side: "buy",
    price_quote: "62000.50",
    quantity_base: "0.0100",
    fee_quote: "0.62",
    filled_at: "2026-06-10T12:00:00+00:00",
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function mockMatchMedia(matches: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
}

describe("FillsTable", () => {
  it("invites the first trade when empty", () => {
    render(<FillsTable fills={[]} />);
    expect(screen.getByText(/no trades yet/)).toBeTruthy();
  });

  it("lists fills newest first with trimmed amounts", () => {
    render(
      <FillsTable
        fills={[
          makeFill({ client_order_id: "old", price_quote: "100.00" }),
          makeFill({ client_order_id: "new", side: "sell", price_quote: "200.00" }),
        ]}
      />,
    );
    const rows = screen.getAllByRole("row");
    // Header row, then the newest fill first, the older one after it.
    expect(rows[1]?.textContent).toContain("200");
    expect(rows[2]?.textContent).toContain("100");
    // Trailing zeros are trimmed for the eye (200.00 -> 200).
    expect(screen.queryByText("200.00")).toBeNull();
  });

  it("renders stacked cards instead of a table on a narrow screen", () => {
    mockMatchMedia(true);
    render(<FillsTable fills={[makeFill({})]} />);
    // No table is rendered in the card layout.
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText(/0.01 @ 62000.5/)).toBeTruthy();
  });
});
