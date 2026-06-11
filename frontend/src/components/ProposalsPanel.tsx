import type { ProposalResponse } from "../api/types";
import { formatTime, trimAmount } from "../lib/format";

/**
 * Co-pilot inbox (ARCHITECTURE.md 6.2): proposals waiting for the user's
 * decision, with the strategy's reasons verbatim and the expiry visible.
 * Renders nothing when there is nothing to decide.
 */
export function ProposalsPanel(props: {
  proposals: ProposalResponse[];
  disabled?: boolean;
  onApprove: (signalId: string) => void;
  onReject: (signalId: string) => void;
}) {
  if (props.proposals.length === 0) {
    return null;
  }
  const disabled = props.disabled ?? false;
  return (
    <section className="rounded-xl border border-amber-700/60 bg-amber-950/20">
      <h3 className="border-b border-amber-900/40 px-4 py-3 text-xs uppercase tracking-wide text-amber-400">
        <span className="font-bold">awaiting your approval</span>
        <span className="ml-2 normal-case tracking-normal text-amber-500/80">
          — co-pilot mode: these trades happen only if you approve them before they expire
        </span>
      </h3>
      <ul>
        {props.proposals.map((proposal) => (
          <li key={proposal.signal_id} className="px-4 py-3">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span
                className={`text-sm font-bold uppercase ${
                  proposal.side === "buy" ? "text-emerald-400" : "text-red-400"
                }`}
              >
                {proposal.side}
              </span>
              <span className="text-sm text-zinc-200">{proposal.symbol}</span>
              <span className="text-sm text-zinc-400">
                @ ~{trimAmount(proposal.proposal_price_quote)} · stop{" "}
                {trimAmount(proposal.stop_price_quote)}
              </span>
              <span className="ml-auto text-xs text-amber-500">
                expires {formatTime(proposal.expires_at)}
              </span>
            </div>
            <ul className="mt-1 text-sm text-zinc-400">
              {proposal.reasons.map((reason, index) => (
                <li key={`${String(index)}-${reason}`}>· {reason}</li>
              ))}
            </ul>
            <div className="mt-2 flex gap-3">
              <button
                onClick={() => {
                  props.onApprove(proposal.signal_id);
                }}
                disabled={disabled}
                className="rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
              >
                approve
              </button>
              <button
                onClick={() => {
                  props.onReject(proposal.signal_id);
                }}
                disabled={disabled}
                className="rounded-lg border border-red-600 px-4 py-1.5 text-sm font-semibold text-red-400 hover:bg-red-600/10 disabled:opacity-50"
              >
                reject
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
