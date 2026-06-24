import type { CampaignSnapshotResponse } from "../api/types";
import { formatTime } from "../lib/format";
import { Badge, Card } from "../ui";

/**
 * Past research campaigns, newest first — the durable §12.7 record. The backend
 * persists each finished campaign; the live card above shows only the current
 * one. Each entry is a campaign's net story: what it tuned, how many promotions
 * landed, why it stopped, and the holdout's honest out-of-sample read. The
 * round-by-round parameter diffs live in the research timeline (Progress tab);
 * this is the campaign-level summary, so the loop's history is scrollable
 * instead of vanishing when the next campaign starts.
 */
export function CampaignHistoryCard(props: { campaigns: CampaignSnapshotResponse[] }) {
  if (props.campaigns.length === 0) {
    return null; // nothing finished yet (or still loading) — keep the tab quiet
  }
  return (
    <Card padding="md">
      <h3 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">past campaigns</h3>
      <p className="mt-0.5 text-xs text-zinc-500">
        finished research campaigns, newest first — what each one tuned and how it landed. The
        round-by-round parameter changes are on the Progress tab.
      </p>
      <ul className="mt-3 space-y-2">
        {props.campaigns.map((campaign, index) => (
          <li
            key={`${campaign.finished_at ?? ""}-${campaign.target}-${String(index)}`}
            className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950/60 p-3 text-sm"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-zinc-900 dark:text-zinc-100">
                {campaign.target}
              </span>
              <span className="text-xs text-zinc-500">
                on {campaign.symbol}, {campaign.timeframe}
              </span>
              {!campaign.promotions_enabled && <Badge tone="zinc">evidence only</Badge>}
              <Badge tone={campaign.promotions > 0 ? "emerald" : "zinc"}>
                {campaign.promotions} promoted
              </Badge>
              {campaign.finished_at !== null && (
                <span className="ml-auto text-xs text-zinc-500">
                  {formatTime(campaign.finished_at)}
                </span>
              )}
            </div>
            {campaign.stop_reason !== null && (
              <p className="mt-1 text-xs text-zinc-500">{campaign.stop_reason}</p>
            )}
            {campaign.holdout_read !== null && (
              <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
                <span className="font-semibold text-zinc-700 dark:text-zinc-300">holdout:</span>{" "}
                {campaign.holdout_read.explanation}
              </p>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}
