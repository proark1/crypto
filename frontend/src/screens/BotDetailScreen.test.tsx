import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteBot,
  fetchBot,
  fetchBotOptions,
  fetchDecisions,
  fetchFills,
} from "../api/client";
import type {
  BotDetailResponse,
  BotOptionsResponse,
  CompetitorResponse,
  DecisionResponse,
} from "../api/types";
import { BotDetailScreen } from "./BotDetailScreen";

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {
    readonly status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  deleteBot: vi.fn(),
  fetchBot: vi.fn(),
  fetchBotOptions: vi.fn(),
  fetchDecisions: vi.fn(),
  fetchFills: vi.fn(),
  killBot: vi.fn(),
  pauseBot: vi.fn(),
  resumeBot: vi.fn(),
}));

const OPTIONS: BotOptionsResponse = {
  families: [
    {
      family: "trend_following",
      label: "Trend following",
      description: "buys when a fast average crosses above a slow one (a trend starting)",
      defaults: { fast_ema_period: 20, slow_ema_period: 50 },
    },
    {
      family: "mean_reversion",
      label: "Mean reversion",
      description: "buys dips in calm markets",
      defaults: { rsi_period: 14 },
    },
  ],
  entry_modes: ["any", "all"],
};

function makeSummary(overrides: Partial<CompetitorResponse>): CompetitorResponse {
  return {
    bot_id: "production",
    label: "Regime router",
    description: "picks a strategy to match the market's mood",
    is_production: true,
    kind: "production",
    paused: false,
    equity_quote: "10123.45",
    initial_balance_quote: "10000",
    return_fraction: "0.0123",
    quote_balance: "8000.12",
    realized_pnl_quote: "98.7",
    unrealized_pnl_quote: "24.75",
    open_positions: 1,
    entry_fills: 3,
    exit_fills: 2,
    breaker_tripped_reason: null,
    ...overrides,
  };
}

const PRODUCTION_DETAIL: BotDetailResponse = {
  summary: makeSummary({}),
  positions: [
    {
      symbol: "BTC/USDT",
      quantity_base: "0.0334",
      average_entry_price_quote: "62000.5",
      mark_price_quote: "62551.26",
      unrealized_pnl_quote: "19.8",
    },
  ],
  strategy: {
    kind: "production",
    regime_routed: true,
    families: {
      trend_following: { fast_ema_period: 20, slow_ema_period: 50 },
      mean_reversion: { rsi_period: 14 },
    },
  },
};

const BUILTIN_DETAIL: BotDetailResponse = {
  summary: makeSummary({
    bot_id: "breakout",
    label: "Breakout",
    description: "buys when price escapes a tight range",
    is_production: false,
    kind: "builtin",
  }),
  positions: [],
  strategy: { kind: "builtin", family: "trend_following", params: { fast_ema_period: 12 } },
};

const CUSTOM_DETAIL: BotDetailResponse = {
  summary: makeSummary({
    bot_id: "custom-7",
    label: "My experiment",
    description: "my own recipe",
    is_production: false,
    kind: "custom",
    open_positions: 1,
  }),
  positions: [],
  strategy: {
    kind: "custom",
    rules: {
      entry_mode: "all",
      families: {
        trend_following: { fast_ema_period: 20 },
        mean_reversion: { rsi_period: 14 },
      },
    },
  },
};

const VETOED_DECISION: DecisionResponse = {
  signal_id: "sig-1",
  strategy_name: "trend_following",
  symbol: "BTC/USDT",
  side: "buy",
  stop_price_quote: "95.5",
  reasons: ["risk cap reached"],
  outcome: "vetoed",
  created_at: "2026-06-10T12:00:00+00:00",
};

function renderScreen(botId: string) {
  const handlers = { onBack: vi.fn(), onEdit: vi.fn(), onDeleted: vi.fn() };
  render(<BotDetailScreen botId={botId} symbol="BTC/USDT" {...handlers} />);
  return handlers;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchBotOptions).mockResolvedValue(OPTIONS);
  vi.mocked(fetchFills).mockResolvedValue([]);
  vi.mocked(fetchDecisions).mockResolvedValue([VETOED_DECISION]);
});

describe("BotDetailScreen", () => {
  it("explains the production bot's regime routing in plain words", async () => {
    vi.mocked(fetchBot).mockResolvedValue(PRODUCTION_DETAIL);
    renderScreen("production");
    expect(await screen.findByText("Regime router")).toBeTruthy();
    expect(screen.getByText("main bot")).toBeTruthy();
    expect(screen.getByText(/Switches between trend following/)).toBeTruthy();
    // Stat cards show the headline numbers.
    expect(screen.getByText("10123.45")).toBeTruthy();
    expect(screen.getByText("+1.23%")).toBeTruthy();
    // Open positions table lists the holding.
    expect(screen.getByText("0.0334")).toBeTruthy();
    // Decisions arrive in plain words, not pipeline jargon.
    expect(screen.getByText("blocked by a safety check")).toBeTruthy();
    expect(screen.getByText(/risk cap reached/)).toBeTruthy();
  });

  it("describes a built-in bot through its family description", async () => {
    vi.mocked(fetchBot).mockResolvedValue(BUILTIN_DETAIL);
    renderScreen("breakout");
    expect(await screen.findByText("built-in")).toBeTruthy();
    expect(screen.getByText(/buys when a fast average crosses above a slow one/)).toBeTruthy();
    // Humanized parameter names appear in the collapsible table.
    expect(screen.getByText("fast EMA period")).toBeTruthy();
  });

  it("explains a custom bot's entry mode and blocks delete while it holds positions", async () => {
    vi.mocked(fetchBot).mockResolvedValue(CUSTOM_DETAIL);
    renderScreen("custom-7");
    expect(await screen.findByText("custom")).toBeTruthy();
    expect(screen.getByText(/Buys only when all of its rules agree/)).toBeTruthy();
    const deleteButton = screen.getByRole("button", { name: "delete" });
    expect((deleteButton as HTMLButtonElement).disabled).toBe(true);
    expect(deleteButton.getAttribute("title")).toContain("stop it first");
    expect(vi.mocked(deleteBot)).not.toHaveBeenCalled();
  });

  it("offers edit rules for custom bots and routes back through onEdit", async () => {
    vi.mocked(fetchBot).mockResolvedValue(CUSTOM_DETAIL);
    const handlers = renderScreen("custom-7");
    fireEvent.click(await screen.findByRole("button", { name: "edit rules" }));
    expect(handlers.onEdit).toHaveBeenCalledWith("custom-7");
  });

  it("requires a confirm step before stop & flatten", async () => {
    vi.mocked(fetchBot).mockResolvedValue(PRODUCTION_DETAIL);
    renderScreen("production");
    fireEvent.click(await screen.findByRole("button", { name: "stop" }));
    expect(
      screen.getByRole("button", {
        name: /confirm: halt the bot and sell its holdings at the next price/,
      }),
    ).toBeTruthy();
  });
});
