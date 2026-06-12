import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchTradingFees, updateTradingFees } from "../api/client";
import type { TradingFeesResponse } from "../api/types";
import { SettingsScreen } from "./SettingsScreen";

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {
    readonly status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  fetchTradingFees: vi.fn(),
  updateTradingFees: vi.fn(),
}));

const FEES: TradingFeesResponse = {
  buy_fee_percent: "0.1",
  sell_fee_percent: "0.1",
  buy_fee_bps: "10",
  sell_fee_bps: "10",
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchTradingFees).mockResolvedValue(FEES);
});

describe("SettingsScreen", () => {
  it("prefills the current fees as percentages", async () => {
    render(<SettingsScreen />);
    const buy = await screen.findByRole("spinbutton", { name: "Buy fee" });
    const sell = screen.getByRole("spinbutton", { name: "Sell fee" });
    expect((buy as HTMLInputElement).value).toBe("0.1");
    expect((sell as HTMLInputElement).value).toBe("0.1");
  });

  it("saves edited fees and shows confirmation", async () => {
    vi.mocked(updateTradingFees).mockResolvedValue({
      ...FEES,
      buy_fee_percent: "0.2",
      buy_fee_bps: "20",
    });
    render(<SettingsScreen />);
    const buy = await screen.findByRole("spinbutton", { name: "Buy fee" });

    fireEvent.change(buy, { target: { value: "0.2" } });
    fireEvent.click(screen.getByRole("button", { name: "save fees" }));

    await waitFor(() => {
      expect(updateTradingFees).toHaveBeenCalledWith("0.2", "0.1");
    });
    expect(await screen.findByText(/effective on the next fill/)).toBeTruthy();
  });

  it("blocks saving an out-of-range fee", async () => {
    render(<SettingsScreen />);
    const sell = await screen.findByRole("spinbutton", { name: "Sell fee" });

    fireEvent.change(sell, { target: { value: "50" } });

    expect(screen.getByText("10% is the maximum")).toBeTruthy();
    expect(screen.getByRole("button", { name: "save fees" })).toHaveProperty("disabled", true);
    expect(updateTradingFees).not.toHaveBeenCalled();
  });
});
