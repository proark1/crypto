import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { StrategyVersionResponse } from "../api/types";
import { ImprovementsPanel } from "./ImprovementsPanel";

const VERSIONS: StrategyVersionResponse[] = [
  {
    id: 3,
    family: "trend_following",
    params: { fast_ema_period: 10, slow_ema_period: 30 },
    source_sweep_id: 12,
    note: "auto-promoted: faster_cross survived walk-forward",
    activated_at: "2026-06-11T00:00:00+00:00",
  },
  {
    id: 1,
    family: "trend_following",
    params: { fast_ema_period: 20, slow_ema_period: 50 },
    source_sweep_id: null,
    note: null,
    activated_at: "2026-06-01T00:00:00+00:00",
  },
];

describe("ImprovementsPanel", () => {
  it("explains the loop and shows an empty state before any promotion", () => {
    render(<ImprovementsPanel versions={[]} onRevert={() => undefined} />);
    expect(screen.getByText(/no promotions yet/)).toBeTruthy();
    expect(screen.getByText(/paper trading only/)).toBeTruthy();
  });

  it("marks the newest version per family active and links its sweep", () => {
    render(<ImprovementsPanel versions={VERSIONS} onRevert={() => undefined} />);
    expect(screen.getByText("active")).toBeTruthy();
    expect(screen.getByText("sweep #12")).toBeTruthy();
    expect(screen.getByText(/fast_ema_period=10/)).toBeTruthy();
  });

  it("offers revert only on inactive versions and reports the click", () => {
    const onRevert = vi.fn();
    render(<ImprovementsPanel versions={VERSIONS} onRevert={onRevert} />);

    // getByText doubles as the singleness assertion: it throws if the
    // active version also offered a revert button.
    fireEvent.click(screen.getByText("revert to this version"));
    expect(onRevert).toHaveBeenCalledWith(1);
  });
});
