import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Alert } from "./Alert";
import { Badge } from "./Badge";
import { Button } from "./Button";
import { InfoTooltip } from "./InfoTooltip";
import { StatTile } from "./StatTile";

describe("Button", () => {
  it("defaults to type=button so it never accidentally submits a form", () => {
    const onSubmit = vi.fn((event: { preventDefault: () => void }) => {
      event.preventDefault();
    });
    render(
      <form onSubmit={onSubmit}>
        <Button>go</Button>
      </form>,
    );
    fireEvent.click(screen.getByText("go"));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("still submits when explicitly typed as submit", () => {
    const onSubmit = vi.fn((event: { preventDefault: () => void }) => {
      event.preventDefault();
    });
    render(
      <form onSubmit={onSubmit}>
        <Button type="submit">go</Button>
      </form>,
    );
    fireEvent.click(screen.getByText("go"));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

describe("StatTile", () => {
  it("colours the value by tone and shows label, value and hint", () => {
    render(<StatTile label="net P/L" value="-12.34" tone="bad" hint="closed trades" />);
    expect(screen.getByText("net P/L")).toBeDefined();
    expect(screen.getByText("closed trades")).toBeDefined();
    const value = screen.getByText("-12.34");
    expect(value.className).toContain("text-red-600");
  });
});

describe("Badge", () => {
  it("renders its label inside a pill", () => {
    render(<Badge tone="sky">main bot</Badge>);
    expect(screen.getByText("main bot")).toBeDefined();
  });
});

describe("Alert", () => {
  it("renders a bold title lead-in and body together", () => {
    render(
      <Alert tone="error" title="circuit breaker tripped">
        daily loss limit
      </Alert>,
    );
    expect(screen.getByText("circuit breaker tripped")).toBeDefined();
    expect(screen.getByText(/daily loss limit/)).toBeDefined();
  });
});

describe("InfoTooltip", () => {
  it("reveals its text on tap and hides it again", () => {
    render(<InfoTooltip text="one R is the amount risked" />);
    const trigger = screen.getByRole("button", { name: /one R is the amount risked/ });
    expect(screen.queryByRole("tooltip")).toBeNull();
    fireEvent.click(trigger);
    expect(screen.getByRole("tooltip")).toBeDefined();
    fireEvent.click(trigger);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});
