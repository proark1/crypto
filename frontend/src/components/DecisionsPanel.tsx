import type { DecisionResponse } from "../api/types";
import { formatTime, trimAmount } from "../lib/format";

const OUTCOME_STYLES: Record<string, string> = {
  submitted: "bg-emerald-500/20 text-emerald-600 dark:text-emerald-400",
  vetoed: "bg-amber-500/20 text-amber-600 dark:text-amber-400",
  gated: "bg-sky-500/20 text-sky-600 dark:text-sky-400",
  paused: "bg-zinc-500/20 text-zinc-600 dark:text-zinc-400",
};

/** Hover meaning per outcome chip, in plain words. */
const OUTCOME_MEANINGS: Record<string, string> = {
  submitted: "passed every risk check and became an order",
  vetoed: "blocked by a risk rule — the reasons below say which",
  gated: "blocked by the market-regime gate (conditions too hostile to enter)",
  paused: "skipped because trading was paused at the time",
};

/**
 * The decision pipeline (ARCHITECTURE.md 6.2): every signal the strategy
 * emitted, what happened to it, and the reasons — shown verbatim. This is
 * where trust is built: the bot never acts (or declines to act) without an
 * explanation on screen.
 */
export function DecisionsPanel(props: { decisions: DecisionResponse[] }) {
  if (props.decisions.length === 0) {
    return (
      <section className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900 p-5 text-sm text-zinc-500">
        no signals yet — the strategy speaks only on EMA crosses
      </section>
    );
  }
  return (
    <section className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <h3 className="border-b border-zinc-200 dark:border-zinc-800 px-4 py-3 text-xs uppercase tracking-wide text-zinc-500">
        <span className="font-bold">decisions</span>
        <span className="ml-2 normal-case tracking-normal">
          — every signal and what the bot did about it, reasons included; hover a chip for its
          meaning
        </span>
      </h3>
      <ul>
        {props.decisions.map((decision) => (
          <li
            key={decision.signal_id + decision.outcome}
            className="border-b border-zinc-200/70 dark:border-zinc-800/50 px-4 py-3 last:border-b-0"
          >
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span
                title={OUTCOME_MEANINGS[decision.outcome]}
                className={`rounded px-2 py-0.5 text-xs font-bold uppercase ${
                  OUTCOME_STYLES[decision.outcome] ??
                  "bg-zinc-500/20 text-zinc-600 dark:text-zinc-400"
                }`}
              >
                {decision.outcome}
              </span>
              <span
                className={`text-sm font-bold uppercase ${
                  decision.side === "buy"
                    ? "text-emerald-600 dark:text-emerald-400"
                    : "text-red-600 dark:text-red-400"
                }`}
              >
                {decision.side}
              </span>
              <span className="text-sm text-zinc-700 dark:text-zinc-300">
                {decision.strategy_name}
              </span>
              <span className="ml-auto text-xs text-zinc-500">
                {formatTime(decision.created_at)}
              </span>
            </div>
            <ul className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
              {decision.reasons.map((reason, index) => (
                <li key={`${String(index)}-${reason}`}>· {reason}</li>
              ))}
              <li className="text-xs text-zinc-500">
                stop {trimAmount(decision.stop_price_quote)}
              </li>
            </ul>
          </li>
        ))}
      </ul>
    </section>
  );
}
