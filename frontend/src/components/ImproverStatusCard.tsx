import type { ImprovementStatusResponse } from "../api/types";
import { formatTime } from "../lib/format";
import { Badge, Card } from "../ui";

/**
 * What the self-improvement loop is doing right now, in plain words. The loop
 * itself runs in the backend on a schedule (evaluate → mine findings → sweep
 * challengers → promote only a statistically validated winner); this card just
 * makes that work visible — what it last did, and when it wakes next — so "is
 * the bot learning?" has an answer on screen. It shows on the dashboard (a
 * glanceable summary that links into Research) and on the Tune tab (beside the
 * sweeps and the version journal it drives).
 */
export function ImproverStatusCard(props: {
  status: ImprovementStatusResponse | null;
  /** When set, a footer link opens the Research view that holds the version
   * journal and research timeline. Omitted on the Tune tab — which already is
   * that detail — and shown on the dashboard summary. */
  onOpenDetails?: () => void;
}) {
  const status = props.status;
  if (status === null) {
    return null; // not loaded yet — the poll fills it in
  }
  const startedAt = status.last_cycle_started_at;
  const finishedAt = status.last_cycle_finished_at;
  const cycleInProgress = startedAt !== null && (finishedAt === null || finishedAt < startedAt);
  return (
    <Card padding="md">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">self-improvement</h3>
        <Badge tone={status.enabled ? "emerald" : "zinc"}>
          {status.enabled ? "on" : "off"}
        </Badge>
        {cycleInProgress && <Badge tone="amber">running now…</Badge>}
      </div>
      {status.enabled ? (
        <>
          <p className="mt-0.5 text-xs text-zinc-500">
            every {status.interval_hours}h the bot studies its own recent results, tests
            targeted tweaks to its settings on {status.history_days} days of past{" "}
            {status.timeframe} data, and adopts one only when it statistically beats what it
            trades today — paper trading only, and every change is logged and reversible.
          </p>
          <dl className="mt-3 space-y-1 text-sm text-zinc-700 dark:text-zinc-300">
            <div className="flex flex-wrap gap-x-2">
              <dt className="text-zinc-500">last cycle:</dt>
              <dd>
                {status.last_outcome ??
                  "none yet — the first cycle runs after the bot has been up for a full interval"}
              </dd>
            </div>
            {finishedAt !== null && !cycleInProgress && (
              <div className="flex flex-wrap gap-x-2">
                <dt className="text-zinc-500">finished:</dt>
                <dd>{formatTime(finishedAt)}</dd>
              </div>
            )}
            {status.next_cycle_at !== null && (
              <div className="flex flex-wrap gap-x-2">
                <dt className="text-zinc-500">next check:</dt>
                <dd>≈ {formatTime(status.next_cycle_at)}</dd>
              </div>
            )}
          </dl>
        </>
      ) : (
        <p className="mt-0.5 text-xs text-zinc-500">
          the bot is not tuning itself (TRADEBOT_AUTO_IMPROVE_ENABLED=false); manual sweeps in
          Research still work, and promotions stay paper-only either way.
        </p>
      )}
      {props.onOpenDetails !== undefined && (
        <button
          type="button"
          onClick={props.onOpenDetails}
          className="mt-3 rounded text-xs font-semibold text-emerald-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 dark:text-emerald-300"
        >
          see what it has changed →
        </button>
      )}
    </Card>
  );
}
