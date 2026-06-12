import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchFills } from "../api/client";
import type { FillResponse } from "../api/types";
import { FillsTable } from "./FillsTable";

vi.mock("../api/client", () => ({ fetchFills: vi.fn() }));

let nextId = 1;

function makeFill(overrides: Partial<FillResponse>): FillResponse {
  return {
    id: nextId++,
    client_order_id: "ord-1",
    symbol: "BTC/USDT",
    side: "buy",
    price_quote: "62000.50",
    quantity_base: "0.0100",
    value_quote: "620.005",
    fee_quote: "0.62",
    filled_at: "2026-06-10T12:00:00+00:00",
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(fetchFills).mockReset();
  nextId = 1;
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
          makeFill({ id: 1, client_order_id: "old", price_quote: "100.00" }),
          makeFill({ id: 2, client_order_id: "new", side: "sell", price_quote: "200.00" }),
        ]}
      />,
    );
    const rows = screen.getAllByRole("row");
    // Header row, then the newest fill first (id 2), the older one after it.
    expect(rows[1]?.textContent).toContain("200");
    expect(rows[2]?.textContent).toContain("100");
    // Trailing zeros are trimmed for the eye (200.00 -> 200).
    expect(screen.queryByText("200.00")).toBeNull();
    // The trade value (notional) column is shown, formatted with grouping.
    expect(screen.getByText("value")).toBeTruthy();
    expect(screen.getAllByText("620").length).toBeGreaterThan(0);
  });

  it("renders stacked cards instead of a table on a narrow screen", () => {
    mockMatchMedia(true);
    render(<FillsTable fills={[makeFill({})]} />);
    // No table is rendered in the card layout.
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText(/0.01 @ 62000.5/)).toBeTruthy();
  });

  it("loads older trades through the cursor and stops at the start", async () => {
    // One short page (< OLDER_PAGE_SIZE) signals the start of the journal.
    vi.mocked(fetchFills).mockResolvedValueOnce([
      makeFill({ id: 1, client_order_id: "older", price_quote: "50.00" }),
    ]);
    render(
      <FillsTable fills={[makeFill({ id: 9, client_order_id: "live" })]} bot="momentum" />,
    );

    fireEvent.click(screen.getByRole("button", { name: /load older trades/i }));

    // Fetched older than the oldest shown id (9), scoped to the bot.
    await waitFor(() => {
      expect(vi.mocked(fetchFills)).toHaveBeenCalledWith("momentum", {
        beforeId: 9,
        limit: 100,
      });
    });
    // The older fill is merged in, and the button gives way to the end marker.
    await screen.findByText(/start of journal/i);
    const rows = screen.getAllByRole("row");
    expect(rows[1]?.textContent).toContain("live");
    expect(rows[2]?.textContent).toContain("older");
  });
});
