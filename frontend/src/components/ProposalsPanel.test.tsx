import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ProposalResponse } from "../api/types";
import { ProposalsPanel } from "./ProposalsPanel";

const PROPOSAL: ProposalResponse = {
  signal_id: "trend_following:BTC/USDT:2026-01-02T00:07:00+00:00",
  symbol: "BTC/USDT",
  side: "buy",
  strategy_name: "trend_following",
  proposal_price_quote: "100.50000000",
  stop_price_quote: "95.00000000",
  reasons: ["fast EMA(20) crossed above slow EMA(50)"],
  created_at: "2026-01-02T00:07:00+00:00",
  expires_at: "2026-01-02T00:22:00+00:00",
};

describe("ProposalsPanel", () => {
  it("renders nothing when there is nothing to decide", () => {
    const { container } = render(
      <ProposalsPanel proposals={[]} onApprove={vi.fn()} onReject={vi.fn()} />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("shows the proposal with reasons and routes approve/reject", () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(<ProposalsPanel proposals={[PROPOSAL]} onApprove={onApprove} onReject={onReject} />);

    expect(screen.getByText(/awaiting your approval/i)).toBeDefined();
    expect(screen.getByText(/fast EMA\(20\)/)).toBeDefined();
    fireEvent.click(screen.getByText("approve"));
    expect(onApprove).toHaveBeenCalledWith(PROPOSAL.signal_id);
    fireEvent.click(screen.getByText("reject"));
    expect(onReject).toHaveBeenCalledWith(PROPOSAL.signal_id);
  });

  it("disables both buttons while a command is pending", () => {
    render(
      <ProposalsPanel proposals={[PROPOSAL]} disabled onApprove={vi.fn()} onReject={vi.fn()} />,
    );
    expect(screen.getByText<HTMLButtonElement>("approve").disabled).toBe(true);
    expect(screen.getByText<HTMLButtonElement>("reject").disabled).toBe(true);
  });
});
