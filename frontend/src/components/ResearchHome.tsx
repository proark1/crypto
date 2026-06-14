import type {
  BakeOffJobResponse,
  ImprovementStatusResponse,
  RoutingCandidacyResponse,
} from "../api/types";
import { formatFractionPercent, signClass } from "../lib/format";
import { Card } from "../ui";
import { contestantLabel } from "./BakeOffPanel";

const TOP_N = 3;

/**
 * A compact landing strip above the research tabs: the three signals a
 * researcher checks first — who is winning the latest bake-off, whether the
 * improvement loop is running, and how many families have earned a routing
 * candidacy — each a shortcut into the tab that owns the detail. It reads the
 * data the research screen already polls (display only, no fetches), so it adds
 * a cockpit without a second source of truth.
 */
export function ResearchHome(props: {
  bakeOffs: BakeOffJobResponse[];
  improver: ImprovementStatusResponse | null;
  candidacies: RoutingCandidacyResponse[];
  onNavigate: (tab: "bakeoff" | "tune" | "compare") => void;
}) {
  const ranking = props.bakeOffs[0]?.results?.ranking ?? [];
  const { improver } = props;
  const candidateCount = props.candidacies.filter((entry) => entry.is_candidate).length;

  const cardButton =
    "w-full text-left transition hover:opacity-80 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 rounded-lg";
  const heading = "text-xs uppercase tracking-wide text-zinc-500";

  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <Card padding="md">
        <button
          type="button"
          onClick={() => {
            props.onNavigate("bakeoff");
          }}
          className={cardButton}
        >
          <h3 className={heading}>Tournament</h3>
          {ranking.length === 0 ? (
            <p className="mt-2 text-xs text-zinc-500">no bake-off yet — run one</p>
          ) : (
            <ol className="mt-2 space-y-1">
              {ranking.slice(0, TOP_N).map((entry, index) => (
                <li
                  key={entry.bot_id}
                  className="flex items-baseline justify-between gap-2 text-sm"
                >
                  <span className="truncate text-zinc-700 dark:text-zinc-300">
                    {index + 1}. {contestantLabel(entry.bot_id)}
                  </span>
                  <span
                    className={`shrink-0 tabular-nums ${signClass(entry.average_return_fraction)}`}
                  >
                    {formatFractionPercent(entry.average_return_fraction)}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </button>
      </Card>

      <Card padding="md">
        <button
          type="button"
          onClick={() => {
            props.onNavigate("tune");
          }}
          className={cardButton}
        >
          <h3 className={heading}>Auto-improve</h3>
          {improver === null ? (
            <p className="mt-2 text-sm text-zinc-500">—</p>
          ) : improver.enabled ? (
            <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">
              on
              {improver.last_outcome !== null && (
                <span className="text-zinc-500"> · last: {improver.last_outcome}</span>
              )}
            </p>
          ) : (
            <p className="mt-2 text-sm text-zinc-500">off</p>
          )}
        </button>
      </Card>

      <Card padding="md">
        <button
          type="button"
          onClick={() => {
            props.onNavigate("compare");
          }}
          className={cardButton}
        >
          <h3 className={heading}>Routing candidates</h3>
          <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">
            {props.candidacies.length === 0 ? (
              <span className="text-zinc-500">no families assessed yet</span>
            ) : (
              <>
                <span className="font-semibold">{candidateCount}</span> of{" "}
                {props.candidacies.length} families flagged
              </>
            )}
          </p>
        </button>
      </Card>
    </div>
  );
}
