import type { ProposalResponse } from "../api/types";
import { formatTime, trimAmount } from "../lib/format";
import { ArrowDownIcon, ArrowUpIcon, Button } from "../ui";

/**
 * Co-pilot inbox (ARCHITECTURE.md 6.2): proposals waiting for the user's
 * decision, with the strategy's reasons verbatim and the expiry visible.
 * Keeps its deliberate amber "needs you" treatment — distinct from the
 * neutral cards — while routing the actions through the shared Button.
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
    <section className="rounded-xl border border-amber-300 bg-amber-50/70 dark:border-amber-700/60 dark:bg-amber-950/20">
      <h3 className="border-b border-amber-200 px-4 py-3 text-xs uppercase tracking-wide text-amber-600 dark:border-amber-900/40 dark:text-amber-400">
        <span className="font-bold">awaiting your approval</span>
        <span className="ml-2 normal-case tracking-normal text-amber-700/80 dark:text-amber-500/80">
          — co-pilot mode: these trades happen only if you approve them before they expire
        </span>
      </h3>
      <ul>
        {props.proposals.map((proposal) => {
          const buy = proposal.side === "buy";
          return (
            <li key={proposal.signal_id} className="px-4 py-3">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span
                  className={`inline-flex items-center gap-1 text-sm font-bold uppercase ${
                    buy
                      ? "text-emerald-600 dark:text-emerald-400"
                      : "text-red-600 dark:text-red-400"
                  }`}
                >
                  {buy ? (
                    <ArrowUpIcon className="h-3.5 w-3.5" />
                  ) : (
                    <ArrowDownIcon className="h-3.5 w-3.5" />
                  )}
                  {proposal.side}
                </span>
                <span className="text-sm text-zinc-800 dark:text-zinc-200">
                  {proposal.symbol}
                </span>
                <span className="text-sm text-zinc-600 dark:text-zinc-400">
                  @ ~{trimAmount(proposal.proposal_price_quote)} · stop{" "}
                  {trimAmount(proposal.stop_price_quote)}
                </span>
                <span className="ml-auto text-xs text-amber-600 dark:text-amber-500">
                  expires {formatTime(proposal.expires_at)}
                </span>
              </div>
              <ul className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
                {proposal.reasons.map((reason, index) => (
                  <li key={`${String(index)}-${reason}`}>· {reason}</li>
                ))}
              </ul>
              <div className="mt-2 flex gap-3">
                <Button
                  size="sm"
                  disabled={disabled}
                  onClick={() => {
                    props.onApprove(proposal.signal_id);
                  }}
                >
                  approve
                </Button>
                <Button
                  variant="dangerOutline"
                  size="sm"
                  disabled={disabled}
                  onClick={() => {
                    props.onReject(proposal.signal_id);
                  }}
                >
                  reject
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
