import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { IntervalSwitcher } from "./IntervalSwitcher";

describe("IntervalSwitcher", () => {
  it("offers minute, hour, day, week, and month timeframes", () => {
    render(<IntervalSwitcher selected="1m" onSelect={() => undefined} />);
    for (const label of ["1m", "1H", "1D", "1W", "1M"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
  });

  it("marks the selected interval and reports clicks on the others", () => {
    const onSelect = vi.fn();
    render(<IntervalSwitcher selected="1d" onSelect={onSelect} />);

    expect(screen.getByText("1D").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("1H").getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(screen.getByText("1W"));
    expect(onSelect).toHaveBeenCalledWith("1w");
  });

  it("is inert while disabled", () => {
    const onSelect = vi.fn();
    render(<IntervalSwitcher selected="1m" disabled onSelect={onSelect} />);
    fireEvent.click(screen.getByText("1H"));
    expect(onSelect).not.toHaveBeenCalled();
  });
});
