import { useState } from "react";

import type { ScenarioReplayResponse } from "../api/types";
import { toReplayMarkers } from "../lib/chart";
import { CandleChart } from "./CandleChart";

/**
 * Blind-then-reveal replay of one evaluation scenario (ARCHITECTURE.md §12).
 * Opens showing only the window the bot decided on; the user reveals the
 * horizon candle by candle. The grade stays hidden until the full horizon is
 * shown — seeing the verdict before the price action would defeat the point
 * of replaying the decision blind.
 */
export function ScenarioReplay(props: { replay: ScenarioReplayResponse; onBack: () => void }) {
  const { replay } = props;
  const [revealed, setRevealed] = useState(0);
  const horizonTotal = replay.horizon.length;
  const fullyRevealed = revealed >= horizonTotal;
  const candles = [...replay.window, ...replay.horizon.slice(0, revealed)];

  const conditionChips = [
    replay.scenario.scenario_class,
    `trend ${replay.scenario.trend}`,
    `volatility ${replay.scenario.volatility}`,
    ...replay.scenario.events,
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={props.onBack}
          className="rounded-lg border border-zinc-300 dark:border-zinc-700 px-3 py-1.5 text-sm text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800"
        >
          ← back to run
        </button>
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          scenario #{replay.scenario.scenario_id} · {replay.scenario.symbol} ·{" "}
          {replay.scenario.timeframe}
        </h3>
        <div className="flex flex-wrap gap-1">
          {conditionChips.map((chip) => (
            <span
              key={chip}
              className="rounded bg-zinc-100 dark:bg-zinc-800 px-2 py-0.5 text-xs text-zinc-600 dark:text-zinc-400"
            >
              {chip.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      </div>

      <div className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900 p-4">
        <div className="text-xs uppercase tracking-wide text-zinc-500">
          decided blind at {replay.scenario.decision_time}
        </div>
        <div className="mt-1 text-lg font-semibold text-zinc-900 dark:text-zinc-100">
          {replay.scenario.decision.toUpperCase()}
          {replay.confidence !== null && (
            <span className="ml-2 text-sm font-normal text-zinc-600 dark:text-zinc-400">
              confidence {replay.confidence.toFixed(2)}
            </span>
          )}
        </div>
        {replay.reasons.length > 0 && (
          <ul className="mt-2 list-inside list-disc text-sm text-zinc-700 dark:text-zinc-300">
            {replay.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        )}
      </div>

      <CandleChart
        candles={candles}
        markers={toReplayMarkers(replay, revealed)}
        emptyText="no candles stored for this scenario any more"
      />

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={fullyRevealed}
          onClick={() => {
            setRevealed((count) => Math.min(count + 1, horizonTotal));
          }}
          className="rounded-lg bg-zinc-100 dark:bg-zinc-800 px-3 py-1.5 text-sm text-zinc-800 dark:text-zinc-200 hover:bg-zinc-200 dark:hover:bg-zinc-700 disabled:opacity-40"
        >
          reveal next candle
        </button>
        <button
          type="button"
          disabled={fullyRevealed}
          onClick={() => {
            setRevealed(horizonTotal);
          }}
          className="rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-500 disabled:opacity-40"
        >
          reveal all
        </button>
        <button
          type="button"
          disabled={revealed === 0}
          onClick={() => {
            setRevealed(0);
          }}
          className="rounded-lg border border-zinc-300 dark:border-zinc-700 px-3 py-1.5 text-sm text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-40"
        >
          hide the future again
        </button>
        <span className="text-sm text-zinc-500">
          {revealed} / {horizonTotal} future candles revealed
        </span>
      </div>

      {fullyRevealed ? (
        <div className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900 p-4">
          <div className="text-xs uppercase tracking-wide text-zinc-500">the grade</div>
          <div className="mt-1 text-lg font-semibold text-zinc-900 dark:text-zinc-100">
            {replay.scenario.verdict.replace(/_/g, " ")}
            {replay.scenario.timing !== null && (
              <span className="ml-2 text-sm font-normal text-zinc-600 dark:text-zinc-400">
                {replay.scenario.timing.replace(/_/g, " ")}
              </span>
            )}
          </div>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
            <GradeItem label="R multiple" value={replay.scenario.r_multiple} />
            <GradeItem label="PnL (quote)" value={replay.pnl_quote} />
            <GradeItem label="entry" value={replay.entry_price_quote} />
            <GradeItem label="exit" value={replay.exit_price_quote} />
            <GradeItem label="MFE (R)" value={replay.mfe_r} />
            <GradeItem label="MAE (R)" value={replay.mae_r} />
            <GradeItem label="oracle (R)" value={replay.oracle_r} />
            <GradeItem
              label="stop hit"
              value={replay.stop_hit === null ? null : replay.stop_hit ? "yes" : "no"}
            />
          </dl>
        </div>
      ) : (
        <div className="rounded-xl border border-dashed border-zinc-300 dark:border-zinc-700 p-4 text-sm text-zinc-500">
          the grade is hidden until the full horizon is revealed — judge the decision the way
          the bot had to make it
        </div>
      )}
    </div>
  );
}

function GradeItem(props: { label: string; value: string | null }) {
  return (
    <>
      <dt className="text-zinc-500">{props.label}</dt>
      <dd className="text-zinc-800 dark:text-zinc-200">{props.value ?? "—"}</dd>
    </>
  );
}
