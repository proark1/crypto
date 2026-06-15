import type { CampaignStatusResponse } from "../api/types";
import { Badge, Card } from "../ui";
import { SettingMove } from "./SettingMove";

/**
 * What the §12.7 research-campaign loop is doing right now, in plain words. The
 * loop runs in the backend: it sweeps challengers, promotes only ones that beat
 * the live settings out of sample, climbs from each, and refines — back to back
 * until a budget is spent. This card makes that visible on the Tune tab: the
 * round-by-round climb, how many promotions landed, and the holdout's honest
 * read of the net move. Flip the loop on or off from Settings.
 */
export function CampaignStatusCard(props: { status: CampaignStatusResponse | null }) {
  const status = props.status;
  if (status === null) {
    return null; // not loaded yet — the poll fills it in
  }
  const campaign = status.campaign;
  const running = campaign !== null && campaign.status === "running";
  return (
    <Card padding="md">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">
          research campaigns
        </h3>
        <Badge tone={status.enabled ? "emerald" : "zinc"}>
          {status.enabled ? "on" : "off"}
        </Badge>
        {running && <Badge tone="amber">running now…</Badge>}
      </div>
      {!status.enabled ? (
        <p className="mt-0.5 text-xs text-zinc-500">
          the campaign loop is off — flip it on in Settings to run continuous research;
          promotions stay paper-only either way.
        </p>
      ) : campaign === null ? (
        <p className="mt-0.5 text-xs text-zinc-500">
          none yet — the first campaign starts after the next cooldown.
        </p>
      ) : (
        <>
          <p className="mt-0.5 text-xs text-zinc-500">
            tuning{" "}
            <span className="font-medium text-zinc-700 dark:text-zinc-300">
              {campaign.target}
            </span>{" "}
            on {campaign.symbol}: sweep, promote what beats the live settings out of sample,
            refine, repeat — up to {status.max_rounds} rounds or {status.max_hours}h.
          </p>
          <dl className="mt-3 space-y-1 text-sm text-zinc-700 dark:text-zinc-300">
            <div className="flex flex-wrap gap-x-2">
              <dt className="text-zinc-500">status:</dt>
              <dd>
                {campaign.status}
                {campaign.stop_reason !== null ? ` — ${campaign.stop_reason}` : ""}
              </dd>
            </div>
            <div className="flex flex-wrap gap-x-2">
              <dt className="text-zinc-500">promotions:</dt>
              <dd>{campaign.promotions}</dd>
            </div>
          </dl>
          {campaign.rounds.length > 0 && (
            <ol className="mt-3 space-y-1.5 text-xs text-zinc-600 dark:text-zinc-400">
              {campaign.rounds.map((round) => (
                <li key={round.index} className="flex flex-col gap-0.5">
                  <div className="flex gap-2">
                    <span className="shrink-0 text-zinc-400">round {round.index + 1}</span>
                    <span>{round.note}</span>
                  </div>
                  {round.changes.length > 0 && (
                    <ul className="ml-[4.5rem] space-y-0.5">
                      {round.changes.map((change) => (
                        <li key={change.field} className="font-mono text-[11px] text-zinc-500">
                          <span className="text-zinc-400">{change.field}</span>{" "}
                          <SettingMove before={change.before} after={change.after} />
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ol>
          )}
          {campaign.holdout_read !== null && (
            <p className="mt-3 text-xs text-zinc-600 dark:text-zinc-400">
              <span className="font-semibold text-zinc-700 dark:text-zinc-300">holdout:</span>{" "}
              {campaign.holdout_read.explanation}
            </p>
          )}
        </>
      )}
    </Card>
  );
}
