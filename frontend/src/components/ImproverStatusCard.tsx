import type { ImprovementStatusResponse } from "../api/types";
import { formatTime } from "../lib/format";
import { Badge, Card } from "../ui";

/**
 * What the self-improvement loop is doing right now. The loop itself runs in
 * the backend on a schedule (evaluate → mine findings → sweep challengers →
 * promote only a statistically validated winner); this card only makes that
 * work visible — last cycle's outcome in the loop's own words, and when it
 * wakes next — so "is the bot learning?" has an answer on screen.
 */
export function ImproverStatusCard(props: { status: ImprovementStatusResponse | null }) {
  const status = props.status;
  if (status === null) {
    return null; // not loaded yet — the research poll fills it in
  }
  const startedAt = status.last_cycle_started_at;
  const finishedAt = status.last_cycle_finished_at;
  const cycleInProgress = startedAt !== null && (finishedAt === null || finishedAt < startedAt);
  return (
    <Card padding="md">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">
          automated improver
        </h3>
        <Badge tone={status.enabled ? "emerald" : "zinc"}>
          {status.enabled ? "on" : "off"}
        </Badge>
        {cycleInProgress && <Badge tone="amber">cycle in progress</Badge>}
      </div>
      {status.enabled ? (
        <>
          <p className="mt-0.5 text-xs text-zinc-500">
            every {status.interval_hours}h the bot re-evaluates itself over{" "}
            {status.history_days} days of {status.timeframe} candles, sweeps challengers aimed
            at the latest findings, and switches settings only when a winner is statistically
            validated (paper trading only).
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
                <dt className="text-zinc-500">next cycle:</dt>
                <dd>≈ {formatTime(status.next_cycle_at)}</dd>
              </div>
            )}
          </dl>
        </>
      ) : (
        <p className="mt-0.5 text-xs text-zinc-500">
          the bot is not tuning itself (TRADEBOT_AUTO_IMPROVE_ENABLED=false); manual sweeps
          below still work, and promotions stay paper-only either way.
        </p>
      )}
    </Card>
  );
}
