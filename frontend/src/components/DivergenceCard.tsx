import type { DivergenceReportResponse } from "../api/types";
import { Badge, Card, StatTile } from "../ui";

function percent(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}

/**
 * Paper-vs-replay drift for the selected coin. Zero is the paper-gate target:
 * live paper fills should match a same-candle replay unless a documented gate
 * or operator action intentionally changed behavior.
 */
export function DivergenceCard(props: { report: DivergenceReportResponse | null }) {
  const { report } = props;
  if (report === null) {
    return null;
  }
  const diverged = report.divergence_fraction > 0;
  return (
    <Card padding="md">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">paper vs replay</h3>
        <Badge tone={diverged ? "amber" : "emerald"}>{diverged ? "drift" : "matched"}</Badge>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatTile
          label="divergence"
          value={percent(report.divergence_fraction)}
          valueClass={diverged ? "text-amber-600 dark:text-amber-400" : undefined}
          hint="share of live/replay fills that did not match by side and candle time"
        />
        <StatTile label="matched" value={String(report.matched_count)} hint="fills matched" />
        <StatTile
          label="live fills"
          value={String(report.live_fill_count)}
          hint="paper fills in the window"
        />
        <StatTile
          label="replay fills"
          value={String(report.replay_fill_count)}
          hint="backtest replay fills in the same window"
        />
      </div>
      {report.mismatches.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs text-zinc-500">
          {report.mismatches.slice(0, 3).map((mismatch) => (
            <li key={mismatch}>{mismatch}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}
