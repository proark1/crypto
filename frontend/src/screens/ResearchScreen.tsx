import { useCallback, useEffect, useState } from "react";

import {
  acceptFinding,
  cancelEvaluation,
  cancelSweep,
  fetchEvaluations,
  fetchEvaluationSuggestions,
  fetchFindings,
  fetchScenarioReplay,
  fetchScenarios,
  fetchStrategyVersions,
  fetchSweeps,
  rejectFinding,
  revertStrategyVersion,
  startEvaluation,
  startSweep,
} from "../api/client";
import type {
  EvaluationRunResponse,
  FindingResponse,
  ScenarioReplayResponse,
  ScenarioSummaryResponse,
  StrategyVersionResponse,
  SuggestedEvaluationResponse,
  SweepResponse,
} from "../api/types";
import { FindingsPanel } from "../components/FindingsPanel";
import { ImprovementsPanel } from "../components/ImprovementsPanel";
import { ScenarioReplay } from "../components/ScenarioReplay";
import { SweepPanel } from "../components/SweepPanel";
import {
  TONE_PANEL_CLASS,
  TONE_TEXT_CLASS,
  VERDICT_CHIP_CLASS,
  VERDICT_LEGEND,
  expectancyTone,
  interpretRun,
  profitFactorTone,
  type Tone,
} from "../lib/interpret";

const POLL_INTERVAL_MS = 3000;
const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"];

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

function Metric(props: { label: string; value: string; hint: string; tone?: Tone }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-zinc-500">{props.label}</div>
      <div className={`text-lg font-semibold ${TONE_TEXT_CLASS[props.tone ?? "neutral"]}`}>
        {props.value}
      </div>
      <div className="text-xs text-zinc-500">{props.hint}</div>
    </div>
  );
}

function BreakdownTable(props: { title: string; data: Record<string, unknown> | null }) {
  if (!props.data || Object.keys(props.data).length === 0) {
    return null;
  }
  return (
    <div>
      <h4 className="mb-1 text-xs uppercase tracking-wide text-zinc-500">{props.title}</h4>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-zinc-500">
            <tr>
              <th className="py-1 pr-2">condition</th>
              <th className="py-1 pr-2">scenarios</th>
              <th className="py-1 pr-2">trades</th>
              <th className="py-1 pr-2">expectancy (R)</th>
              <th className="py-1">win rate</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(props.data).map(([label, raw]) => {
              const row = asRecord(raw);
              const tone = expectancyTone(row?.expectancy_r);
              return (
                <tr key={label} className="border-t border-zinc-800/60 text-zinc-300">
                  <td className="py-1 pr-2">{label.replace(/_/g, " ")}</td>
                  <td className="py-1 pr-2">{text(row?.scenario_count)}</td>
                  <td className="py-1 pr-2">{text(row?.trade_count)}</td>
                  <td
                    className={`py-1 pr-2 ${tone === "neutral" ? "" : TONE_TEXT_CLASS[tone]}`}
                  >
                    {text(row?.expectancy_r)}
                  </td>
                  <td className="py-1">{text(row?.win_rate)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function RunReport(props: { run: EvaluationRunResponse }) {
  const summary = props.run.summary;
  if (!summary) {
    return (
      <div className="text-sm text-zinc-500">no report yet — the run has not completed</div>
    );
  }
  const verdicts = asRecord(summary.verdicts) ?? {};
  const reading = interpretRun(summary);
  return (
    <div className="space-y-4">
      <div className={`rounded-lg border p-3 ${TONE_PANEL_CLASS[reading.tone]}`}>
        <div className="text-sm font-bold">{reading.headline}</div>
        <p className="mt-1 text-sm opacity-90">{reading.explanation}</p>
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Metric
          label="expectancy (R)"
          value={text(summary.expectancy_r)}
          tone={expectancyTone(summary.expectancy_r)}
          hint="average R per trade — above 0 makes money"
        />
        <Metric
          label="profit factor"
          value={text(summary.profit_factor)}
          tone={profitFactorTone(summary.profit_factor)}
          hint="wins ÷ losses — above 1.0 makes money"
        />
        <Metric
          label="win rate"
          value={text(summary.win_rate)}
          hint="alone says little — win size matters more"
        />
        <Metric
          label="trades / scenarios"
          value={`${text(summary.trade_count)} / ${text(summary.scenario_count)}`}
          hint="sample size — small samples lie"
        />
      </div>
      <div>
        <h4 className="mb-1 text-xs uppercase tracking-wide text-zinc-500">
          how each scenario was graded — hover a chip for its meaning
        </h4>
        <div className="flex flex-wrap gap-2">
          {Object.entries(verdicts).map(([verdict, count]) => {
            const legend = VERDICT_LEGEND[verdict];
            return (
              <span
                key={verdict}
                title={legend?.meaning}
                className={`rounded px-2 py-0.5 text-xs ${
                  VERDICT_CHIP_CLASS[legend?.tone ?? "neutral"]
                }`}
              >
                {verdict.replace(/_/g, " ")}: {text(count)}
              </span>
            );
          })}
        </div>
      </div>
      <div className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
        <h4 className="text-xs uppercase tracking-wide text-zinc-500">what to do next</h4>
        <ol className="mt-1 list-decimal space-y-1 pl-5 text-sm text-zinc-300">
          {reading.nextSteps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <BreakdownTable title="by trend" data={asRecord(summary.by_trend)} />
        <BreakdownTable title="by volatility" data={asRecord(summary.by_volatility)} />
        <BreakdownTable title="by event" data={asRecord(summary.by_event)} />
        <BreakdownTable title="by timeframe" data={asRecord(summary.by_timeframe)} />
      </div>
    </div>
  );
}

export function ScenarioTable(props: {
  scenarios: ScenarioSummaryResponse[];
  onReplay: (scenarioId: number) => void;
}) {
  if (props.scenarios.length === 0) {
    return null;
  }
  return (
    <div>
      <h4 className="mb-1 text-xs uppercase tracking-wide text-zinc-500">
        scenarios — pick one to replay it blind
      </h4>
      <div className="max-h-80 overflow-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-zinc-500">
            <tr>
              <th className="py-1 pr-2">#</th>
              <th className="py-1 pr-2">conditions</th>
              <th className="py-1 pr-2">decision</th>
              <th className="py-1 pr-2">verdict</th>
              <th className="py-1 pr-2">R</th>
              <th className="py-1">timing</th>
            </tr>
          </thead>
          <tbody>
            {props.scenarios.map((scenario) => (
              <tr
                key={scenario.scenario_id}
                onClick={() => {
                  props.onReplay(scenario.scenario_id);
                }}
                className="cursor-pointer border-t border-zinc-800/60 text-zinc-300 hover:bg-zinc-800/60"
              >
                <td className="py-1 pr-2">{scenario.scenario_id}</td>
                <td className="py-1 pr-2 text-xs">
                  {[scenario.trend, scenario.volatility, ...scenario.events]
                    .join(" · ")
                    .replace(/_/g, " ")}
                </td>
                <td className="py-1 pr-2">{scenario.decision}</td>
                <td className="py-1 pr-2">{scenario.verdict.replace(/_/g, " ")}</td>
                <td className="py-1 pr-2">{scenario.r_multiple ?? "—"}</td>
                <td className="py-1">{scenario.timing?.replace(/_/g, " ") ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ResearchScreen() {
  const [runs, setRuns] = useState<EvaluationRunResponse[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [days, setDays] = useState("90");
  const [count, setCount] = useState("400");
  const [timeframe, setTimeframe] = useState("1h");
  const [notice, setNotice] = useState<string | null>(null);
  const [scenarios, setScenarios] = useState<ScenarioSummaryResponse[]>([]);
  const [findings, setFindings] = useState<FindingResponse[]>([]);
  const [replay, setReplay] = useState<ScenarioReplayResponse | null>(null);
  const [sweeps, setSweeps] = useState<SweepResponse[]>([]);
  const [versions, setVersions] = useState<StrategyVersionResponse[]>([]);
  const [suggestions, setSuggestions] = useState<SuggestedEvaluationResponse[]>([]);

  const refresh = useCallback(async () => {
    try {
      setRuns(await fetchEvaluations());
      setSweeps(await fetchSweeps());
      setVersions(await fetchStrategyVersions());
    } catch {
      // The overview screen owns auth/error UX; research polls quietly.
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => {
      clearInterval(timer);
    };
  }, [refresh]);

  useEffect(() => {
    // Stored history depth moves a day at a time; once per visit is fresh
    // enough, and it keeps the poll loop free of per-coin depth queries.
    fetchEvaluationSuggestions().then(setSuggestions, () => undefined);
  }, []);

  const selected = runs.find((run) => run.id === selectedId) ?? runs[0] ?? null;
  const selectedRunId = selected?.id ?? null;
  const selectedRunStatus = selected?.status ?? null;

  // The scenario list refreshes when the selection changes or the run
  // reaches a terminal status — not on every poll, which would re-download
  // hundreds of rows every few seconds while a run is in flight.
  useEffect(() => {
    setReplay(null);
    setScenarios([]);
    setFindings([]);
    if (selectedRunId === null) {
      return;
    }
    // The overview screen owns auth/error UX; research polls quietly.
    fetchScenarios(selectedRunId).then(setScenarios, () => undefined);
    fetchFindings(selectedRunId).then(setFindings, () => undefined);
  }, [selectedRunId, selectedRunStatus]);

  const openReplay = (scenarioId: number) => {
    fetchScenarioReplay(scenarioId).then(setReplay, (caught: unknown) => {
      setNotice(caught instanceof Error ? caught.message : "failed to load the replay");
    });
  };

  const runSuggestion = (suggestion: SuggestedEvaluationResponse) => {
    startEvaluation({
      symbols: [suggestion.symbol],
      timeframes: [suggestion.timeframe],
      history_days: suggestion.history_days,
      scenario_count: suggestion.scenario_count,
    }).then(
      (started) => {
        setNotice(started.detail);
        setSelectedId(started.run_id);
        void refresh();
      },
      (caught: unknown) => {
        setNotice(caught instanceof Error ? caught.message : "failed to start run");
      },
    );
  };

  const handleStartSweep = () => {
    startSweep({ timeframe, history_days: Number(days) || 90 }).then(
      (started) => {
        setNotice(started.detail);
        void refresh();
      },
      (caught: unknown) => {
        setNotice(caught instanceof Error ? caught.message : "failed to start the sweep");
      },
    );
  };

  const decideFinding = (decide: (findingId: number) => Promise<FindingResponse>) => {
    return (findingId: number) => {
      decide(findingId).then(
        (updated) => {
          setFindings((current) =>
            current.map((finding) => (finding.id === updated.id ? updated : finding)),
          );
        },
        (caught: unknown) => {
          setNotice(caught instanceof Error ? caught.message : "failed to record the verdict");
        },
      );
    };
  };

  return (
    <div className="space-y-4">
      {suggestions.length > 0 && (
        <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
          <h3 className="text-xs uppercase tracking-wide text-zinc-500">
            suggested evaluations — fitted to each coin&apos;s stored history
          </h3>
          <div className="mt-2 grid gap-3 sm:grid-cols-3">
            {suggestions.map((suggestion) => (
              <div
                key={`${suggestion.symbol}-${suggestion.timeframe}`}
                className="flex flex-col justify-between rounded-lg border border-zinc-800 bg-zinc-950/60 p-3"
              >
                <div>
                  <div className="text-sm font-semibold text-zinc-100">
                    {suggestion.title} · {suggestion.symbol}
                  </div>
                  <div className="mt-0.5 text-xs text-zinc-500">
                    {suggestion.timeframe} · {suggestion.history_days} days · ~
                    {suggestion.expected_candles.toLocaleString()} candles
                  </div>
                  <p className="mt-1 text-xs text-zinc-400">{suggestion.rationale}</p>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    runSuggestion(suggestion);
                  }}
                  className="mt-3 self-start rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
                >
                  run
                </button>
              </div>
            ))}
          </div>
        </section>
      )}
      <form
        className="flex flex-wrap items-end gap-3 rounded-xl border border-zinc-800 bg-zinc-900 p-4"
        onSubmit={(event) => {
          event.preventDefault();
          void (async () => {
            try {
              const started = await startEvaluation({
                timeframes: [timeframe],
                history_days: Number(days) || 90,
                scenario_count: Number(count) || 200,
              });
              setNotice(started.detail);
              setSelectedId(started.run_id);
              await refresh();
            } catch (caught) {
              setNotice(caught instanceof Error ? caught.message : "failed to start run");
            }
          })();
        }}
      >
        <label className="text-xs text-zinc-400">
          history (days)
          <input
            value={days}
            onChange={(event) => {
              setDays(event.target.value);
            }}
            className="mt-1 block w-24 rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-100"
          />
        </label>
        <label className="text-xs text-zinc-400">
          scenarios per coin
          <input
            value={count}
            onChange={(event) => {
              setCount(event.target.value);
            }}
            className="mt-1 block w-28 rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-100"
          />
        </label>
        <label className="text-xs text-zinc-400">
          timeframe
          <select
            value={timeframe}
            onChange={(event) => {
              setTimeframe(event.target.value);
            }}
            className="mt-1 block rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-100"
          >
            {TIMEFRAMES.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-500"
        >
          start evaluation
        </button>
        {notice && <span className="text-sm text-zinc-400">{notice}</span>}
      </form>

      <div className="grid gap-4 lg:grid-cols-[16rem_1fr]">
        <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
          {runs.length === 0 && <div className="text-sm text-zinc-500">no runs yet</div>}
          <ul className="space-y-1">
            {runs.map((run) => (
              <li key={run.id}>
                <button
                  type="button"
                  onClick={() => {
                    setSelectedId(run.id);
                  }}
                  className={`w-full rounded-lg px-2 py-1.5 text-left text-sm ${
                    selected?.id === run.id
                      ? "bg-zinc-800 text-zinc-100"
                      : "text-zinc-400 hover:bg-zinc-800/60"
                  }`}
                >
                  run #{run.id} · {run.status}
                  <span className="block text-xs text-zinc-500">
                    {run.symbols.join(", ")} · {run.timeframes.join(", ")} · {run.progress_done}
                    /{run.progress_total}
                  </span>
                </button>
                {run.status === "running" && (
                  <button
                    type="button"
                    onClick={() => {
                      void cancelEvaluation(run.id).then(refresh, refresh);
                    }}
                    className="mt-0.5 px-2 text-xs text-red-400 hover:text-red-300"
                  >
                    cancel
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
        <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
          {replay ? (
            <ScenarioReplay
              replay={replay}
              onBack={() => {
                setReplay(null);
              }}
            />
          ) : selected ? (
            <div className="space-y-4">
              <RunReport run={selected} />
              <FindingsPanel
                findings={findings}
                onAccept={decideFinding(acceptFinding)}
                onReject={decideFinding(rejectFinding)}
                onReplayEvidence={openReplay}
              />
              <ScenarioTable scenarios={scenarios} onReplay={openReplay} />
            </div>
          ) : (
            <div className="text-sm text-zinc-500">start a run to see its report here</div>
          )}
        </section>
      </div>

      <ImprovementsPanel
        versions={versions}
        onRevert={(versionId) => {
          revertStrategyVersion(versionId).then(
            (result) => {
              setNotice(result.detail);
              void refresh();
            },
            (caught: unknown) => {
              setNotice(caught instanceof Error ? caught.message : "failed to revert");
            },
          );
        }}
      />
      <SweepPanel
        sweeps={sweeps}
        onStart={handleStartSweep}
        onCancel={(sweepId) => {
          void cancelSweep(sweepId).then(refresh, refresh);
        }}
      />
    </div>
  );
}
