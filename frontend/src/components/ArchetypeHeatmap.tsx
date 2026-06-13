import type { ComparisonGroupResponse, EvaluationRunResponse } from "../api/types";
import { strategyLabel } from "./ComparisonPanel";

/**
 * The scenario heatmap: a bot lineup down the rows, market archetypes across
 * the columns, each cell that bot's expectancy (R) in that archetype. It
 * answers the question a single ranking can't — *which bot wins in chop, and
 * which dies there?* — by pivoting the comparison's own per-archetype
 * breakdown (no new data, no new run). Recommends nothing; routing stays a
 * human call (ARCHITECTURE.md §13.7).
 */

/** Canonical display order + short headers for the ten archetypes (the
 * backend `Archetype` partition). Trends first, then ranges by volatility,
 * then events. */
const ARCHETYPES: { key: string; label: string; hint: string }[] = [
  { key: "bull", label: "bull", hint: "uptrend" },
  { key: "bear", label: "bear", hint: "downtrend" },
  { key: "range", label: "range", hint: "rangebound, normal volatility" },
  { key: "chop", label: "chop", hint: "rangebound, high volatility" },
  { key: "compression", label: "coil", hint: "rangebound, low volatility (a squeeze setup)" },
  { key: "breakout", label: "b-out", hint: "a breakout that held" },
  { key: "fakeout", label: "fake", hint: "a breakout that failed back" },
  { key: "pump", label: "pump", hint: "a single-candle spike up" },
  { key: "crash", label: "crash", hint: "a single-candle spike down" },
  { key: "recovery", label: "rec", hint: "a post-crash recovery" },
];

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number") {
    return value;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

/** One bot's expectancy (R) in one archetype, or null if it never traded
 * there (or the run is still in flight). */
function expectancyIn(run: EvaluationRunResponse, archetypeKey: string): number | null {
  if (run.status !== "completed" || run.summary === null) {
    return null;
  }
  const byArchetype = asRecord(run.summary.by_archetype);
  const bucket = asRecord(byArchetype?.[archetypeKey]);
  return asNumber(bucket?.expectancy_r);
}

/** A diverging green/red wash by expectancy sign and strength — the heat. */
function heatClass(value: number | null): string {
  if (value === null) {
    return "text-zinc-300 dark:text-zinc-600";
  }
  if (value >= 0.5) {
    return "bg-emerald-500/30 text-emerald-900 dark:text-emerald-100";
  }
  if (value > 0) {
    return "bg-emerald-500/12 text-emerald-800 dark:text-emerald-200";
  }
  if (value <= -0.5) {
    return "bg-red-500/30 text-red-900 dark:text-red-100";
  }
  return "bg-red-500/12 text-red-800 dark:text-red-200";
}

export function ArchetypeHeatmap(props: { group: ComparisonGroupResponse }) {
  const runs = props.group.runs;
  // Only show archetypes that at least one bot actually traded — sampled
  // scenarios rarely cover all ten, and an all-empty column is just noise.
  const columns = ARCHETYPES.filter((archetype) =>
    runs.some((run) => expectancyIn(run, archetype.key) !== null),
  );
  if (columns.length === 0) {
    return null;
  }
  // The winner per column (best expectancy among completed bots) gets a ring,
  // so "best bot in chop" reads at a glance.
  const bestByColumn = new Map<string, number>();
  for (const archetype of columns) {
    const values = runs
      .map((run) => expectancyIn(run, archetype.key))
      .filter((value): value is number => value !== null);
    if (values.length > 0) {
      bestByColumn.set(archetype.key, Math.max(...values));
    }
  }

  return (
    <div className="mt-4">
      <h4 className="mb-1 text-xs font-semibold text-zinc-600 dark:text-zinc-300">
        expectancy (R) by market archetype
      </h4>
      <p className="mb-2 text-xs text-zinc-500">
        which bot wins where — a ring marks the best bot in each archetype; empty cells never
        traded that regime
      </p>
      <div className="overflow-x-auto">
        <table className="text-left text-xs">
          <thead>
            <tr className="text-zinc-500">
              <th className="py-1 pr-3 font-medium">bot</th>
              {columns.map((archetype) => (
                <th
                  key={archetype.key}
                  className="px-2 py-1 text-center font-medium"
                  title={archetype.hint}
                >
                  {archetype.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id} className="border-t border-zinc-200/70 dark:border-zinc-800/60">
                <td className="py-1 pr-3 whitespace-nowrap text-zinc-700 dark:text-zinc-300">
                  {strategyLabel(run.strategy)}
                </td>
                {columns.map((archetype) => {
                  const value = expectancyIn(run, archetype.key);
                  const isBest = value !== null && value === bestByColumn.get(archetype.key);
                  return (
                    <td
                      key={archetype.key}
                      className={`px-2 py-1 text-center tabular-nums ${heatClass(value)} ${
                        isBest ? "ring-1 ring-inset ring-emerald-500 font-semibold" : ""
                      }`}
                    >
                      {value === null ? "—" : value.toFixed(2)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
