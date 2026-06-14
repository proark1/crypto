import { useState } from "react";

import type { BakeOffCellRecord, BakeOffJobResponse } from "../api/types";
import { formatFractionPercent, formatTime, signClass } from "../lib/format";
import { Button, Card } from "../ui";

/** Plain-words names for the bake-off contestants (the production baseline,
 * the ten energy presets, and the reference controls); unknown ids fall back
 * to a readable form so a roster change still renders. Kept in step with
 * backend presets.py. */
const CONTESTANT_LABELS: Record<string, string> = {
  production: "Production (baseline)",
  trend_calm: "Trend (calm)",
  trend_bold: "Trend (bold)",
  reversion_calm: "Mean reversion (calm)",
  reversion_bold: "Mean reversion (bold)",
  breakout_calm: "Breakout (calm)",
  breakout_bold: "Breakout (bold)",
  momentum_calm: "Momentum (calm)",
  momentum_bold: "Momentum (bold)",
  squeeze_calm: "Squeeze (calm)",
  squeeze_bold: "Squeeze (bold)",
  ensemble_confluence: "Ensemble (confluence)",
  ensemble_breadth: "Ensemble (breadth)",
  random_entry: "Random entry (control)",
};

export function contestantLabel(botId: string): string {
  return CONTESTANT_LABELS[botId] ?? botId.replace(/_/g, " ");
}

const MEDALS: Record<number, string> = { 1: "🥇 ", 2: "🥈 ", 3: "🥉 " };

/** Column header for one grid cell, e.g. "1h · 100d". */
function cellLabel(cell: BakeOffCellRecord): string {
  return `${cell.timeframe} · ${String(cell.history_days)}d`;
}

function statusLine(job: BakeOffJobResponse): string {
  if (job.status === "running" || job.status === "pending") {
    return `running · ${String(job.cells_done)}/${String(job.cells_total)} cells`;
  }
  return `${job.status} · ${String(job.cells_done)}/${String(job.cells_total)} cells`;
}

/** The leaderboard: contestants ranked by average return (best first, as the
 * backend sorts them), with the medals the comparison panel uses. */
function Leaderboard(props: { job: BakeOffJobResponse }) {
  const ranking = props.job.results?.ranking ?? [];
  if (ranking.length === 0) {
    return (
      <p className="mt-3 text-xs text-zinc-500">
        no cell has finished yet — the leaderboard fills in as each one completes
      </p>
    );
  }
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-zinc-200 text-xs text-zinc-500 dark:border-zinc-700">
            <th className="py-1 pr-3 font-medium">#</th>
            <th className="py-1 pr-3 font-medium">contestant</th>
            <th className="py-1 pr-3 font-medium">avg return</th>
            <th className="py-1 pr-3 font-medium">cells</th>
            <th className="py-1 pr-3 font-medium">trades</th>
          </tr>
        </thead>
        <tbody>
          {ranking.map((entry, index) => (
            <tr
              key={entry.bot_id}
              className="border-b border-zinc-100 last:border-0 dark:border-zinc-800"
            >
              <td className="py-1 pr-3 tabular-nums">{`${MEDALS[index + 1] ?? ""}${String(index + 1)}`}</td>
              <td className="py-1 pr-3">{contestantLabel(entry.bot_id)}</td>
              <td
                className={`py-1 pr-3 tabular-nums ${signClass(entry.average_return_fraction)}`}
              >
                {formatFractionPercent(entry.average_return_fraction)}
              </td>
              <td className="py-1 pr-3 tabular-nums text-zinc-500">{entry.cells_scored}</td>
              <td className="py-1 pr-3 tabular-nums text-zinc-500">{entry.total_trades}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The per-cell grid: a row per contestant, a column per grid cell, each
 * showing that contestant's return there — or "—" for a cell with too little
 * history to trade. The detail behind the leaderboard's averages. */
function CellGrid(props: { job: BakeOffJobResponse }) {
  const cells = props.job.results?.cells ?? [];
  const ranking = props.job.results?.ranking ?? [];
  if (cells.length === 0) {
    return null;
  }
  // Row order follows the leaderboard when it exists, else the roster.
  const botIds =
    ranking.length > 0 ? ranking.map((entry) => entry.bot_id) : props.job.contestants;
  return (
    <div className="mt-4 overflow-x-auto">
      <h4 className="mb-1 text-xs font-semibold text-zinc-600 dark:text-zinc-300">
        per-cell returns
      </h4>
      <table className="w-full text-left text-xs">
        <thead>
          <tr className="border-b border-zinc-200 text-zinc-500 dark:border-zinc-700">
            <th className="py-1 pr-3 font-medium">contestant</th>
            {cells.map((cell) => (
              <th key={cellLabel(cell)} className="py-1 pr-3 font-medium whitespace-nowrap">
                {cellLabel(cell)}
                {cell.status !== "completed" && (
                  <span className="ml-1 text-zinc-400" title="too little history to trade">
                    ∅
                  </span>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {botIds.map((botId) => (
            <tr
              key={botId}
              className="border-b border-zinc-100 last:border-0 dark:border-zinc-800"
            >
              <td className="py-1 pr-3 whitespace-nowrap">{contestantLabel(botId)}</td>
              {cells.map((cell) => {
                const result = cell.results[botId];
                return (
                  <td
                    key={cellLabel(cell)}
                    className={`py-1 pr-3 tabular-nums ${
                      result ? signClass(result.return_fraction) : "text-zinc-400"
                    }`}
                  >
                    {result ? formatFractionPercent(result.return_fraction) : "—"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The bake-off: one button runs the whole contestant roster across the grid
 * of timeframes and history windows, then ranks them by the money they made.
 * Recommends nothing — what trades stays a human call.
 */
export function BakeOffPanel(props: {
  jobs: BakeOffJobResponse[];
  onStart: () => void;
  startDisabled: boolean;
  /** Controlled selection: when the parent passes these, it owns the
   * selected job (e.g. to jump to a bake-off it just started). Omit both and
   * the panel manages selection itself, defaulting to the newest job. */
  selectedId?: number | null;
  onSelectId?: (id: number) => void;
}) {
  const [internalSelectedId, setInternalSelectedId] = useState<number | null>(null);
  const selectedId = props.selectedId !== undefined ? props.selectedId : internalSelectedId;
  const setSelectedId: (id: number) => void = props.onSelectId ?? setInternalSelectedId;
  const selected = props.jobs.find((job) => job.id === selectedId) ?? props.jobs[0];

  return (
    <Card padding="md">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          strategy bake-off
        </h3>
        <Button size="sm" onClick={props.onStart} disabled={props.startDisabled}>
          run bake-off
        </Button>
        <span className="text-xs text-zinc-500">
          runs every contestant across a grid of timeframes and history windows, then ranks them
          by the money they made — fully automated, saved as it goes
        </span>
      </div>
      <p className="mt-1 text-xs text-zinc-500">
        ten energy presets (each family at a calm and a bold temper), two ensembles (confluence
        and breadth), the live bot as a baseline, and a random-entry control as the noise floor;
        cells with too little history to trade are skipped, and the winner is the one with the
        highest average return across the cells it could trade
      </p>

      {props.jobs.length === 0 ? (
        <p className="mt-3 text-xs text-zinc-500">
          no bake-off yet — press <span className="font-semibold">run bake-off</span> to start
          one
        </p>
      ) : (
        selected !== undefined && (
          <div className="mt-3 grid gap-4 lg:grid-cols-[12rem_1fr]">
            <ul className="space-y-1">
              {props.jobs.map((job) => (
                <li key={job.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedId(job.id);
                    }}
                    className={`w-full rounded-md px-2 py-1 text-left text-xs ${
                      job.id === selected.id
                        ? "bg-zinc-200 dark:bg-zinc-800"
                        : "text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-900"
                    }`}
                  >
                    <div className="font-medium">{`bake-off #${String(job.id)}`}</div>
                    <div className="text-zinc-400">{formatTime(job.created_at)}</div>
                  </button>
                </li>
              ))}
            </ul>
            {/* min-w-0: let this 1fr track shrink below the wide per-cell
                table's intrinsic width so the table scrolls inside its own
                overflow-x-auto instead of pushing past the card. */}
            <div className="min-w-0">
              <div className="text-xs text-zinc-500">{statusLine(selected)}</div>
              <Leaderboard job={selected} />
              <CellGrid job={selected} />
            </div>
          </div>
        )
      )}
    </Card>
  );
}
