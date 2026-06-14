import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { requestResearchAdvice } from "../api/client";
import { AdvisorPanel } from "./AdvisorPanel";

vi.mock("../api/client", () => ({ requestResearchAdvice: vi.fn() }));

describe("AdvisorPanel", () => {
  it("shows the diagnosis and hypotheses when advice is available", async () => {
    vi.mocked(requestResearchAdvice).mockResolvedValue({
      available: true,
      advice: {
        diagnosis: "Edge comes from calm uptrends; volatile chop bleeds it.",
        hypotheses: [
          {
            title: "Gate breakouts on a pullback",
            family: "breakout",
            rationale: "Late entries lose about 0.6R each.",
            parameter_hint: "add a 1-2 candle pullback filter",
          },
        ],
      },
    });
    render(<AdvisorPanel runId={7} />);
    fireEvent.click(screen.getByRole("button", { name: /suggest experiments/i }));

    await waitFor(() => {
      expect(
        screen.getByText("Edge comes from calm uptrends; volatile chop bleeds it."),
      ).toBeDefined();
    });
    expect(screen.getByText("Gate breakouts on a pullback")).toBeDefined();
    expect(screen.getByText(/add a 1-2 candle pullback filter/)).toBeDefined();
    expect(vi.mocked(requestResearchAdvice)).toHaveBeenCalledWith(7);
  });

  it("explains how to enable the advisor when it is unavailable", async () => {
    vi.mocked(requestResearchAdvice).mockResolvedValue({ available: false, advice: null });
    render(<AdvisorPanel runId={7} />);
    fireEvent.click(screen.getByRole("button", { name: /suggest experiments/i }));

    await waitFor(() => {
      expect(screen.getByText(/advisor is off or unavailable/i)).toBeDefined();
    });
  });

  it("surfaces an error without crashing", async () => {
    vi.mocked(requestResearchAdvice).mockRejectedValue(new Error("offline"));
    render(<AdvisorPanel runId={7} />);
    fireEvent.click(screen.getByRole("button", { name: /suggest experiments/i }));

    await waitFor(() => {
      expect(screen.getByText("offline")).toBeDefined();
    });
  });
});
