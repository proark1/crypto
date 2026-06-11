import type { DecisionResponse } from "../api/types";
import { formatTime, trimAmount } from "../lib/format";
import { ArrowDownIcon, ArrowUpIcon, Badge, type BadgeTone, Card, SectionHeader } from "../ui";

/** Each outcome's badge tone, paired with a plain-words meaning shown on the
 * chip's tooltip. */
const OUTCOME_TONE: Record<string, BadgeTone> = {
  submitted: "emerald",
  vetoed: "amber",
  gated: "sky",
  paused: "zinc",
};

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
      <Card padding="lg">
        <span className="text-sm text-zinc-500">
          no signals yet — the strategy speaks only on EMA crosses
        </span>
      </Card>
    );
  }
  return (
    <Card padding="none">
      <SectionHeader
        title="Decisions"
        description="every signal and what the bot did about it, reasons included"
        className="border-b border-zinc-200 px-4 py-3 dark:border-zinc-800"
      />
      <ul>
        {props.decisions.map((decision) => {
          const buy = decision.side === "buy";
          return (
            <li
              key={decision.signal_id + decision.outcome}
              className="border-b border-zinc-200/70 px-4 py-3 last:border-b-0 dark:border-zinc-800/50"
            >
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <Badge
                  tone={OUTCOME_TONE[decision.outcome] ?? "zinc"}
                  title={OUTCOME_MEANINGS[decision.outcome]}
                >
                  {decision.outcome}
                </Badge>
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
          );
        })}
      </ul>
    </Card>
  );
}
