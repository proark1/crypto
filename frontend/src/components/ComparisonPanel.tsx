import { useState } from "react";

import type { ComparisonGroupResponse, EvaluationRunResponse } from "../api/types";
import { formatTime } from "../lib/format";

/** Plain-words names for the competing bot ids; unknown ids fall back to
 * their underscores-stripped form so a new challenger still renders. */
const STRATEGY_LABELS: Record<string, string> = {
  production: "Regime router",
  trend_following: "Trend following",
  mean_reversion: "Mean reversion",
  breakout: "Breakout",
  momentum: "Momentum",
};

export function strategyLabel(strategy: string): string {
  return STRATEGY_LABELS[strategy] ?? strategy.replace(/_/g, " ");
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

function text(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "—";
}

function asPercent(value: unknown): string {
  // Ratios (win rate as a fraction), never money: parsing is display-only.
  const parsed = asNumber(value);
  return parsed === null ? "—" : `${(parsed * 100).toFixed(1)}%`;
}

interface MetricRow {
  label: string;
  hint: string;
  key: string;
  /** Render the fraction as a percentage. */
  percent?: boolean;
  /** Mark the highest completed value in the row as the winner. */
  highlightBest?: boolean;
}

const METRIC_ROWS: MetricRow[] = [
  { label: "trades", hint: "graded sample size — small samples lie", key: "trade_count" },
  {
    label: "expectancy (R)",
    hint: "average R per trade — above 0 makes money",
    key: "expectancy_r",
    highlightBest: true,
  },
  {
    label: "win rate",
    hint: "alone says little — win size matters more",
    key: "win_rate",
    percent: true,
    highlightBest: true,
  },
  {
    label: "profit factor",
    hint: "wins ÷ losses — above 1.0 makes money",
    key: "profit_factor",
    highlightBest: true,
  },
  { label: "avg win (R)", hint: "average winning trade", key: "average_win_r" },
  { label: "avg loss (R)", hint: "average losing trade", key: "average_loss_r" },
];

function metricValue(run: EvaluationRunResponse, key: string): unknown {
  return run.status === "completed" && run.summary !== null ? run.summary[key] : null;
}

function statusCell(run: EvaluationRunResponse): string {
  if (run.status === "running" || run.status === "pending") {
    return `${run.status} · ${String(run.progress_done)}/${String(run.progress_total)}`;
  }
  return run.status;
}

/**
 * One comparison batch rendered side by side: a column per strategy, the
 * same scenarios graded for each, so differences are the strategies' own.
 */
function ComparisonTable(props: { group: ComparisonGroupResponse }) {
  const runs = props.group.runs;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-zinc-500">
          <tr>
            <th className="py-1 pr-3" />
            {runs.map((run) => (
              <th key={run.id} className="py-1 pr-3 font-semibold text-zinc-300">
                {strategyLabel(run.strategy)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr className="border-t border-zinc-800/60 text-zinc-400">
            <td className="py-1 pr-3 text-xs uppercase tracking-wide text-zinc-500">status</td>
            {runs.map((run) => (
              <td key={run.id} className="py-1 pr-3 text-xs">
                {statusCell(run)}
              </td>
            ))}
          </tr>
          {METRIC_ROWS.map((row) => {
            // The winner is the highest value among completed runs only;
            // a run still in flight never wins or loses a row.
            const values = runs.map((run) => asNumber(metricValue(run, row.key)));
            const finite = values.filter((value): value is number => value !== null);
            const best =
              row.highlightBest === true && finite.length > 1 ? Math.max(...finite) : null;
            return (
              <tr key={row.key} className="border-t border-zinc-800/60 text-zinc-300">
                <td
                  className="py-1 pr-3 text-xs uppercase tracking-wide text-zinc-500"
                  title={row.hint}
                >
                  {row.label}
                </td>
                {runs.map((run, index) => {
                  const raw = metricValue(run, row.key);
                  const isBest = best !== null && values[index] === best;
                  return (
                    <td
                      key={run.id}
                      className={`py-1 pr-3 ${isBest ? "font-semibold text-emerald-300" : ""}`}
                    >
                      {row.percent === true ? asPercent(raw) : text(raw)}
                      {isBest && (
                        <span className="ml-1 text-xs" title="best of the completed runs">
                          ★
                        </span>
                      )}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Strategy comparison: one evaluation run per competing strategy over the
 * identical scenario set, so the only difference between columns is the
 * strategy itself. Recommends nothing — what trades stays a human call.
 */
export function ComparisonPanel(props: {
  groups: ComparisonGroupResponse[];
  onStart: () => void;
  startDisabled: boolean;
}) {
  const [selectedGroupId, setSelectedGroupId] = useState<number | null>(null);
  const selected =
    props.groups.find((group) => group.group_id === selectedGroupId) ?? props.groups[0];

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-semibold text-zinc-100">strategy comparison</h3>
        <button
          type="button"
          onClick={props.onStart}
          disabled={props.startDisabled}
          className="rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          compare all strategies
        </button>
        <span className="text-xs text-zinc-500">
          replays the same past moments once per strategy — five columns, identical scenarios,
          so the differences are the strategies&apos; own
        </span>
      </div>

      {props.groups.length > 0 && selected !== undefined && (
        <div className="mt-3 grid gap-4 lg:grid-cols-[14rem_1fr]">
          <ul className="space-y-1">
            {props.groups.map((group) => (
              <li key={group.group_id}>
                <button
                  type="button"
                  onClick={() => {
                    setSelectedGroupId(group.group_id);
                  }}
                  className={`w-full rounded-lg px-2 py-1.5 text-left text-sm ${
                    selected.group_id === group.group_id
                      ? "bg-zinc-800 text-zinc-100"
                      : "text-zinc-400 hover:bg-zinc-800/60"
                  }`}
                >
                  comparison #{group.group_id}
                  <span className="block text-xs text-zinc-500">
                    {formatTime(group.created_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <ComparisonTable group={selected} />
        </div>
      )}
    </section>
  );
}
