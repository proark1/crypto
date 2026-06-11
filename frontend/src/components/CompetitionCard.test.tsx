import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CompetitionResponse, CompetitorResponse } from "../api/types";
import { CompetitionCard } from "./CompetitionCard";

const PRODUCTION: CompetitorResponse = {
  bot_id: "production",
  label: "Regime router",
  description: "picks a strategy to match the market's mood",
  is_production: true,
  kind: "production",
  paused: false,
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
  kind: "builtin",
  paused: false,
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

const CUSTOM_PAUSED: CompetitorResponse = {
  bot_id: "custom-7",
  label: "My experiment",
  description: "buys when any rule fires",
  is_production: false,
  kind: "custom",
  paused: true,
  equity_quote: "9990",
  initial_balance_quote: "10000",
  return_fraction: "-0.001",
  quote_balance: "9990",
  realized_pnl_quote: "-10",
  unrealized_pnl_quote: null,
  open_positions: 0,
  entry_fills: 1,
  exit_fills: 1,
  breaker_tripped_reason: null,
};

const COMPETITION: CompetitionResponse = {
  quote_currency: "USDT",
  competitors: [PRODUCTION, CHALLENGER, CUSTOM_PAUSED],
};

function renderCard(competition: CompetitionResponse | null) {
  const handlers = {
    onSelectBot: vi.fn(),
    onCreateBot: vi.fn(),
    onPauseBot: vi.fn(),
    onResumeBot: vi.fn(),
    onKillBot: vi.fn(),
  };
  const view = render(<CompetitionCard competition={competition} {...handlers} />);
  return { handlers, view };
}

describe("CompetitionCard", () => {
  it("renders nothing until the first snapshot arrives", () => {
    const { view } = renderCard(null);
    expect(view.container.innerHTML).toBe("");
  });

  it("ranks competitors in backend order with equity, return, and trades", () => {
    renderCard(COMPETITION);
    expect(screen.getByText("Regime router")).toBeTruthy();
    expect(screen.getByText("10123.45")).toBeTruthy(); // equity, truncated
    const gain = screen.getByText("+1.23%");
    expect(gain.className).toContain("emerald");
    expect(screen.getByText("98.7")).toBeTruthy(); // realized pnl
    expect(screen.getByText(/\(3 entries\)/)).toBeTruthy(); // entries beside round trips
  });

  it("badges the main bot in plain words and custom bots as custom", () => {
    renderCard(COMPETITION);
    const mainBadge = screen.getByText("main bot");
    expect(mainBadge).toBeTruthy();
    // The tooltip explains the production framing to non-technical users.
    expect(mainBadge.getAttribute("title")).toContain("real money");
    expect(screen.getByText("custom")).toBeTruthy();
  });

  it("shows muted dashes for unknown amounts and a breaker warning", () => {
    renderCard(COMPETITION);
    // The challenger has no equity and no return: both render as a dash.
    expect(screen.getAllByText("—").length).toBe(2);
    const loss = screen.getByText("-42.5");
    expect(loss.className).toContain("red");
    expect(screen.getByTitle(/daily loss limit reached/)).toBeTruthy();
  });

  it("opens the bot's detail page when its row is clicked", () => {
    const { handlers } = renderCard(COMPETITION);
    fireEvent.click(screen.getByText("Breakout"));
    expect(handlers.onSelectBot).toHaveBeenCalledWith("breakout");
  });

  it("pauses a running bot without opening its detail page", () => {
    const { handlers } = renderCard(COMPETITION);
    const pauseButtons = screen.getAllByRole("button", { name: "pause" });
    fireEvent.click(pauseButtons[0] as HTMLElement);
    expect(handlers.onPauseBot).toHaveBeenCalledWith("production");
    expect(handlers.onSelectBot).not.toHaveBeenCalled();
  });

  it("marks a paused bot and offers resume instead of pause", () => {
    const { handlers } = renderCard(COMPETITION);
    expect(screen.getByText("paused")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "resume" }));
    expect(handlers.onResumeBot).toHaveBeenCalledWith("custom-7");
  });

  it("requires a confirm step before stopping a bot", () => {
    const { handlers } = renderCard(COMPETITION);
    const stopButtons = screen.getAllByRole("button", { name: "stop" });
    fireEvent.click(stopButtons[0] as HTMLElement);
    expect(handlers.onKillBot).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "sell & stop" }));
    expect(handlers.onKillBot).toHaveBeenCalledWith("production");
  });

  it("offers a create-a-bot entry point", () => {
    const { handlers } = renderCard(COMPETITION);
    fireEvent.click(screen.getByRole("button", { name: "create a bot" }));
    expect(handlers.onCreateBot).toHaveBeenCalled();
  });
});

describe("CompetitionCard on a narrow screen", () => {
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

  it("renders stacked cards instead of the table and keeps the controls working", () => {
    mockMatchMedia(true);
    const { handlers } = renderCard(COMPETITION);

    // Each bot still appears exactly once — the card layout, not the table.
    expect(screen.getByText("Regime router")).toBeTruthy();
    expect(screen.getByText("10123.45")).toBeTruthy();

    // The stop confirm flow behaves the same as on desktop.
    const stopButtons = screen.getAllByRole("button", { name: "stop" });
    fireEvent.click(stopButtons[0] as HTMLElement);
    expect(handlers.onKillBot).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "sell & stop" }));
    expect(handlers.onKillBot).toHaveBeenCalledWith("production");
  });
});
