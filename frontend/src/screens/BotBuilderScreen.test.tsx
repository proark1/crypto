import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createBot, fetchBot, fetchBotOptions, updateBotRules } from "../api/client";
import type { BotDetailResponse, BotOptionsResponse } from "../api/types";
import { BotBuilderScreen } from "./BotBuilderScreen";

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {
    readonly status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  createBot: vi.fn(),
  fetchBot: vi.fn(),
  fetchBotOptions: vi.fn(),
  updateBotRules: vi.fn(),
}));

const OPTIONS: BotOptionsResponse = {
  families: [
    {
      family: "trend_following",
      label: "Trend following",
      description: "buys when a fast average crosses above a slow one (a trend starting)",
      defaults: { fast_ema_period: 20, slow_ema_period: 50, long_only: true },
    },
    {
      family: "breakout",
      label: "Breakout",
      description: "buys when price escapes a tight range",
      defaults: { lookback: 48 },
    },
  ],
  entry_modes: ["any", "all"],
};

function renderBuilder(editBotId: string | null = null) {
  const handlers = { onCancel: vi.fn(), onSaved: vi.fn() };
  render(<BotBuilderScreen editBotId={editBotId} {...handlers} />);
  return handlers;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchBotOptions).mockResolvedValue(OPTIONS);
});

describe("BotBuilderScreen", () => {
  it("lists every rule card with its plain description", async () => {
    renderBuilder();
    expect(await screen.findByText("Trend following")).toBeTruthy();
    expect(screen.getByText("Breakout")).toBeTruthy();
    expect(screen.getByText(/a trend starting/)).toBeTruthy();
    // No entry-mode choice while fewer than two rules are picked.
    expect(screen.queryByText(/ANY rule fires/)).toBeNull();
  });

  it("creates a bot from the picked rule and navigates to it", async () => {
    vi.mocked(createBot).mockResolvedValue({ bot_id: "custom-9", detail: "created" });
    const handlers = renderBuilder();
    fireEvent.click(await screen.findByRole("checkbox", { name: /Trend following/ }));
    fireEvent.change(screen.getByLabelText("name"), { target: { value: "My bot" } });
    fireEvent.click(screen.getByRole("button", { name: "create the bot" }));
    await waitFor(() => {
      expect(vi.mocked(createBot)).toHaveBeenCalledWith({
        name: "My bot",
        rules: {
          families: {
            trend_following: { fast_ema_period: 20, slow_ema_period: 50, long_only: true },
          },
        },
      });
    });
    await waitFor(() => {
      expect(handlers.onSaved).toHaveBeenCalledWith("custom-9");
    });
  });

  it("offers the entry-mode choice once two rules are picked and sends it", async () => {
    vi.mocked(createBot).mockResolvedValue({ bot_id: "custom-10", detail: "created" });
    renderBuilder();
    fireEvent.click(await screen.findByRole("checkbox", { name: /Trend following/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Breakout/ }));
    fireEvent.click(screen.getByRole("radio", { name: /ALL rules agree/ }));
    fireEvent.change(screen.getByLabelText("name"), { target: { value: "Combo" } });
    fireEvent.click(screen.getByRole("button", { name: "create the bot" }));
    await waitFor(() => {
      expect(vi.mocked(createBot)).toHaveBeenCalled();
    });
    const body = vi.mocked(createBot).mock.calls[0]?.[0];
    expect(body?.rules.entry_mode).toBe("all");
    expect(Object.keys(body?.rules.families ?? {})).toEqual(["trend_following", "breakout"]);
  });

  it("refuses to submit without a rule and surfaces backend details", async () => {
    vi.mocked(createBot).mockRejectedValue(new Error("a bot with that name already exists"));
    renderBuilder();
    await screen.findByText("Trend following");
    fireEvent.click(screen.getByRole("button", { name: "create the bot" }));
    expect(await screen.findByText("pick at least one rule")).toBeTruthy();
    expect(vi.mocked(createBot)).not.toHaveBeenCalled();
    // Now pick a rule and a name; the backend's plain-words 400 shows inline.
    fireEvent.click(screen.getByRole("checkbox", { name: /Trend following/ }));
    fireEvent.change(screen.getByLabelText("name"), { target: { value: "Taken" } });
    fireEvent.click(screen.getByRole("button", { name: "create the bot" }));
    expect(await screen.findByText("a bot with that name already exists")).toBeTruthy();
  });

  it("prefills from a custom bot's rules in edit mode and PUTs the update", async () => {
    const detail: BotDetailResponse = {
      summary: {
        bot_id: "custom-7",
        label: "My experiment",
        description: "my own recipe",
        is_production: false,
        kind: "custom",
        paused: false,
        equity_quote: "10000",
        initial_balance_quote: "10000",
        return_fraction: "0",
        quote_balance: "10000",
        realized_pnl_quote: "0",
        unrealized_pnl_quote: null,
        open_positions: 0,
        entry_fills: 0,
        exit_fills: 0,
        breaker_tripped_reason: null,
      },
      positions: [],
      strategy: {
        kind: "custom",
        rules: { entry_mode: "any", families: { breakout: { lookback: 96 } } },
      },
    };
    vi.mocked(fetchBot).mockResolvedValue(detail);
    vi.mocked(updateBotRules).mockResolvedValue({ paused: false, detail: "updated" });
    const handlers = renderBuilder("custom-7");
    expect(await screen.findByText(/edit rules — My experiment/)).toBeTruthy();
    // The bot's rule is pre-selected with its overridden parameter.
    const breakoutBox = screen.getByRole("checkbox", { name: /Breakout/ });
    expect((breakoutBox as HTMLInputElement).checked).toBe(true);
    // No name field in edit mode — PUT only changes the rules.
    expect(screen.queryByLabelText("name")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "save the rules" }));
    await waitFor(() => {
      expect(vi.mocked(updateBotRules)).toHaveBeenCalledWith("custom-7", {
        families: { breakout: { lookback: 96 } },
      });
    });
    await waitFor(() => {
      expect(handlers.onSaved).toHaveBeenCalledWith("custom-7");
    });
  });
});
