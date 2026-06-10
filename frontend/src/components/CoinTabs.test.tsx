import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CoinTabs } from "./CoinTabs";

describe("CoinTabs", () => {
  it("renders nothing for a single-coin bot", () => {
    const { container } = render(
      <CoinTabs symbols={["BTC/USDT"]} selected="BTC/USDT" onSelect={() => undefined} />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("marks the selected coin and reports clicks on the others", () => {
    const onSelect = vi.fn();
    render(
      <CoinTabs symbols={["BTC/USDT", "ETH/USDT"]} selected="ETH/USDT" onSelect={onSelect} />,
    );

    expect(screen.getByText("ETH/USDT").getAttribute("aria-current")).toBe("page");
    expect(screen.getByText("BTC/USDT").getAttribute("aria-current")).toBeNull();

    fireEvent.click(screen.getByText("BTC/USDT"));
    expect(onSelect).toHaveBeenCalledWith("BTC/USDT");
  });

  it("is inert while a command is pending", () => {
    const onSelect = vi.fn();
    render(
      <CoinTabs
        symbols={["BTC/USDT", "ETH/USDT"]}
        selected="BTC/USDT"
        disabled
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByText("ETH/USDT"));
    expect(onSelect).not.toHaveBeenCalled();
  });
});
