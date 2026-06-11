import type { StrategyVersionResponse } from "../api/types";
import { formatTime } from "../lib/format";
import { Badge, Button, Card } from "../ui";

/**
 * The strategy-settings journal: every configuration the bot has traded,
 * newest first. Versions arrive from the automated improvement loop (only
 * statistically validated sweep winners are ever promoted) or from a
 * manual revert — the human override that always stays available.
 */
export function ImprovementsPanel(props: {
  versions: StrategyVersionResponse[];
  disabled?: boolean;
  onRevert: (versionId: number) => void;
}) {
  // Versions arrive newest first across all families; the newest of each
  // family is what that family trades right now.
  const activeIds = new Set<number>();
  const seenFamilies = new Set<string>();
  for (const version of props.versions) {
    if (!seenFamilies.has(version.family)) {
      seenFamilies.add(version.family);
      activeIds.add(version.id);
    }
  }
  return (
    <Card padding="md">
      <h3 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">
        automated improvements
      </h3>
      <p className="mt-0.5 text-xs text-zinc-500">
        The bot tunes itself: on a schedule it tests variants of its current settings and
        switches only when a challenger is statistically <em>validated</em> on untouched data
        (paper trading only — going live always stays your decision). Every change lands here
        with the sweep that earned it; revert any version if you disagree.
      </p>
      {props.versions.length === 0 ? (
        <p className="mt-3 text-sm text-zinc-500">
          no promotions yet — the bot is trading its default settings until a challenger proves
          itself
        </p>
      ) : (
        <ul className="mt-3 space-y-2">
          {props.versions.map((version) => (
            <li
              key={version.id}
              className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950/60 p-3 text-sm"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-zinc-900 dark:text-zinc-100">
                  v{version.id} · {version.family.replace(/_/g, " ")}
                </span>
                {activeIds.has(version.id) && <Badge tone="emerald">active</Badge>}
                {version.source_sweep_id !== null && (
                  <Badge tone="zinc">sweep #{version.source_sweep_id}</Badge>
                )}
                <span className="ml-auto text-xs text-zinc-500">
                  {formatTime(version.activated_at)}
                </span>
              </div>
              <div className="mt-1 font-mono text-xs text-zinc-600 dark:text-zinc-400">
                {Object.entries(version.params)
                  .map(([key, value]) => `${key}=${String(value)}`)
                  .join(" · ")}
              </div>
              {version.note && <p className="mt-1 text-xs text-zinc-500">{version.note}</p>}
              {!activeIds.has(version.id) && (
                <div className="mt-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={props.disabled}
                    onClick={() => {
                      props.onRevert(version.id);
                    }}
                  >
                    revert to this version
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
