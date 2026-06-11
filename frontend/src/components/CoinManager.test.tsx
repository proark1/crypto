import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CoinManager } from "./CoinManager";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CoinManager", () => {
  it("submits a trimmed, uppercased symbol and clears the input", () => {
    const onAdd = vi.fn();
    render(<CoinManager selected="BTC/USDT" onAdd={onAdd} onRemove={() => undefined} />);

    const input = screen.getByPlaceholderText(/add coin/);
    fireEvent.change(input, { target: { value: "  eth/usdt " } });
    fireEvent.click(screen.getByText("add coin"));

    expect(onAdd).toHaveBeenCalledWith("ETH/USDT");
    expect((input as HTMLInputElement).value).toBe("");
  });

  it("ignores an empty submit", () => {
    const onAdd = vi.fn();
    render(<CoinManager selected="BTC/USDT" onAdd={onAdd} onRemove={() => undefined} />);
    fireEvent.click(screen.getByText("add coin"));
    expect(onAdd).not.toHaveBeenCalled();
  });

  it("removes the selected coin only after the inline confirm step", () => {
    const onRemove = vi.fn();
    render(<CoinManager selected="BTC/USDT" onAdd={() => undefined} onRemove={onRemove} />);

    // First click only arms the confirm; nothing is removed yet.
    fireEvent.click(screen.getByText("remove BTC/USDT"));
    expect(onRemove).not.toHaveBeenCalled();

    // The armed state asks to confirm; the second click commits.
    fireEvent.click(screen.getByText(/stop BTC\/USDT/));
    expect(onRemove).toHaveBeenCalledWith("BTC/USDT");
  });
});
