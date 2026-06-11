import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConfirmButton } from "./ConfirmButton";

describe("ConfirmButton", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("requires two clicks before firing the action", () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton label="stop" confirmLabel="sell & stop" onConfirm={onConfirm} />);
    fireEvent.click(screen.getByText("stop"));
    expect(onConfirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("sell & stop"));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("can be cancelled from the armed state without firing", () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton label="stop" confirmLabel="sell & stop" onConfirm={onConfirm} />);
    fireEvent.click(screen.getByText("stop"));
    fireEvent.click(screen.getByText("cancel"));
    expect(onConfirm).not.toHaveBeenCalled();
    // Back to the resting label.
    expect(screen.getByText("stop")).toBeDefined();
  });

  it("auto-cancels the armed state after the timeout so it never lingers", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton
        label="stop"
        confirmLabel="sell & stop"
        onConfirm={onConfirm}
        timeoutMs={4000}
      />,
    );
    fireEvent.click(screen.getByText("stop"));
    expect(screen.getByText("sell & stop")).toBeDefined();
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(screen.queryByText("sell & stop")).toBeNull();
    expect(screen.getByText("stop")).toBeDefined();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("stops click propagation so a surrounding row is not also triggered", () => {
    const onConfirm = vi.fn();
    const onRowClick = vi.fn();
    render(
      <div onClick={onRowClick}>
        <ConfirmButton label="stop" confirmLabel="sell & stop" onConfirm={onConfirm} />
      </div>,
    );
    fireEvent.click(screen.getByText("stop"));
    fireEvent.click(screen.getByText("sell & stop"));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onRowClick).not.toHaveBeenCalled();
  });
});
