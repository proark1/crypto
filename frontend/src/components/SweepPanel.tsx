import { useState } from "react";

import type { SweepResponse } from "../api/types";

/**
 * Walk-forward parameter sweeps (ARCHITECTURE.md §12.5). Candidates are
 * scored on a training period; only the winner is checked against the
 * later, untouched validation period — and a winner that collapses there
 * is labeled overfit, in plain words. Like findings, a sweep recommends;
 * changing the live configuration stays a human action.
 */
export function SweepPanel(props: {
  sweeps: SweepResponse[];
  onStart: () => void;
  onCancel: (sweepId: number) => void;
}) {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selected = props.sweeps.find((sweep) => sweep.id === selectedId) ?? props.sweeps[0];

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-semibold text-zinc-100">parameter sweeps (walk-forward)</h3>
        <button
          type="button"
          onClick={props.onStart}
          className="rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-500"
        >
          run sweep
        </button>
        <span className="text-xs text-zinc-500">
          tunes on the first part of history, validates on the untouched rest
        </span>
      </div>

      {props.sweeps.length > 0 && (
        <div className="mt-3 grid gap-4 lg:grid-cols-[14rem_1fr]">
          <ul className="space-y-1">
            {props.sweeps.map((sweep) => (
              <li key={sweep.id}>
                <button
                  type="button"
                  onClick={() => {
                    setSelectedId(sweep.id);
                  }}
                  className={`w-full rounded-lg px-2 py-1.5 text-left text-sm ${
                    selected?.id === sweep.id
                      ? "bg-zinc-800 text-zinc-100"
                      : "text-zinc-400 hover:bg-zinc-800/60"
                  }`}
                >
                  sweep #{sweep.id} · {sweep.status}
                  <span className="block text-xs text-zinc-500">
                    {sweep.symbol} · {sweep.timeframe}
                  </span>
                </button>
                {sweep.status === "running" && (
                  <button
                    type="button"
                    onClick={() => {
                      props.onCancel(sweep.id);
                    }}
                    className="mt-0.5 px-2 text-xs text-red-400 hover:text-red-300"
                  >
                    cancel
                  </button>
                )}
              </li>
            ))}
          </ul>
          <div>{selected && <SweepReport sweep={selected} />}</div>
        </div>
      )}
    </section>
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
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

const NEUTRAL_VERDICT_STYLE = "border-zinc-700 bg-zinc-800/60 text-zinc-300";
const VERDICT_STYLES: Record<string, string> = {
  validated: "border-emerald-700 bg-emerald-900/40 text-emerald-200",
  overfit: "border-red-800 bg-red-900/30 text-red-200",
  baseline_best: NEUTRAL_VERDICT_STYLE,
  insufficient_evidence: "border-amber-800 bg-amber-900/30 text-amber-200",
};

export function SweepReport(props: { sweep: SweepResponse }) {
  const report = props.sweep.report;
  if (!report) {
    return (
      <div className="text-sm text-zinc-500">no report yet — the sweep has not completed</div>
    );
  }
  const verdict = text(report.verdict);
  return (
    <div className="space-y-3">
      <div
        className={`rounded-lg border p-3 text-sm ${
          VERDICT_STYLES[verdict] ?? NEUTRAL_VERDICT_STYLE
        }`}
      >
        <span className="font-semibold uppercase tracking-wide">
          {verdict.replace(/_/g, " ")}
        </span>
        <p className="mt-1">{text(report.explanation)}</p>
      </div>
      <ScoreTable title="training period" data={asRecord(report.training)} />
      <ScoreTable title="validation period (untouched)" data={asRecord(report.validation)} />
    </div>
  );
}

function ScoreTable(props: { title: string; data: Record<string, unknown> | null }) {
  if (!props.data || Object.keys(props.data).length === 0) {
    return null;
  }
  return (
    <div>
      <h4 className="mb-1 text-xs uppercase tracking-wide text-zinc-500">{props.title}</h4>
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-zinc-500">
          <tr>
            <th className="py-1 pr-2">candidate</th>
            <th className="py-1 pr-2">trades</th>
            <th className="py-1 pr-2">expectancy (R)</th>
            <th className="py-1 pr-2">win rate</th>
            <th className="py-1">profit factor</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(props.data).map(([name, raw]) => {
            const row = asRecord(raw);
            return (
              <tr key={name} className="border-t border-zinc-800/60 text-zinc-300">
                <td className="py-1 pr-2">{name.replace(/_/g, " ")}</td>
                <td className="py-1 pr-2">{text(row?.trade_count)}</td>
                <td className="py-1 pr-2">{text(row?.expectancy_r)}</td>
                <td className="py-1 pr-2">{text(row?.win_rate)}</td>
                <td className="py-1">{text(row?.profit_factor)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
