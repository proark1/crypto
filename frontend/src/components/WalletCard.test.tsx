import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { WalletResponse } from "../api/types";
import { WalletCard } from "./WalletCard";

const USDT_HOLDING = {
  asset: "USDT",
  symbol: null,
  quantity: "9799.800000000000000000000000",
  mark_price_quote: null,
  value_quote: "9799.8",
  unrealized_pnl_quote: null,
};

const BTC_HOLDING = {
  asset: "BTC",
  symbol: "BTC/USDT",
  quantity: "0.033461853938",
  mark_price_quote: "62551.26",
  value_quote: "2093.11",
  unrealized_pnl_quote: "19.8",
};

const WALLET: WalletResponse = {
  quote_currency: "USDT",
  equity_quote: "10019.800000000000000000000000",
  holdings: [USDT_HOLDING, BTC_HOLDING],
};

describe("WalletCard", () => {
  it("renders nothing until the first wallet snapshot arrives", () => {
    const { container } = render(<WalletCard wallet={null} />);
    expect(container.innerHTML).toBe("");
  });

  it("shows free quote truncated and coin quantities at full precision", () => {
    render(<WalletCard wallet={WALLET} />);
    expect(screen.getByText("USDT")).toBeTruthy();
    expect(screen.getByText("9799.8")).toBeTruthy(); // truncated for the eye
    expect(screen.getByText("BTC")).toBeTruthy();
    expect(screen.getByText("0.033461853938")).toBeTruthy(); // never rounded
    expect(screen.getByText(/≈ 2093.11 USDT @ 62551.26/)).toBeTruthy();
    expect(screen.getByText(/19.8 unrealized/)).toBeTruthy();
    expect(screen.getByText(/10019.8 USDT/)).toBeTruthy(); // the equity total
  });

  it("says unpriced rather than hiding a coin without a mark", () => {
    const unpriced: WalletResponse = {
      ...WALLET,
      equity_quote: null,
      holdings: [USDT_HOLDING, { ...BTC_HOLDING, mark_price_quote: null, value_quote: null }],
    };
    render(<WalletCard wallet={unpriced} />);
    expect(screen.getByText(/unpriced/)).toBeTruthy();
    expect(screen.getByText(/unknown/)).toBeTruthy();
  });
});
