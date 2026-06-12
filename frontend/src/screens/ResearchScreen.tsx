import { useCallback, useEffect, useRef, useState } from "react";

import {
  acceptFinding,
  cancelEvaluation,
  cancelSweep,
  fetchComparisons,
  fetchEvaluations,
  fetchEvaluationStrategies,
  fetchEvaluationSuggestions,
  fetchFindings,
  fetchImprovementStatus,
  fetchScenarioReplay,
  fetchScenarios,
  fetchStrategyVersions,
  fetchSweeps,
  rejectFinding,
  revertStrategyVersion,
  startComparison,
  startEvaluation,
  startSweep,
} from "../api/client";
import type {
  ComparisonGroupResponse,
  EvaluationRunResponse,
  EvaluationStrategyResponse,
  FindingResponse,
  ImprovementStatusResponse,
  ScenarioReplayResponse,
  ScenarioSummaryResponse,
  StrategyVersionResponse,
  SuggestedEvaluationResponse,
  SweepResponse,
} from "../api/types";
import { formatFractionPercent, formatMoney, formatTime } from "../lib/format";
import { Alert, GLOSSARY, InfoTooltip, StatTile, type GlossaryTerm } from "../ui";
import { ComparisonPanel } from "../components/ComparisonPanel";
import { FindingsPanel } from "../components/FindingsPanel";
import { ImprovementsPanel } from "../components/ImprovementsPanel";
import { ImproverStatusCard } from "../components/ImproverStatusCard";
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

/** Before the selector's options load (or while offline) the form stays
 * usable: the incumbent always exists on the backend. */
const FALLBACK_STRATEGIES: EvaluationStrategyResponse[] = [
  {
    id: "production",
    label: "production (default)",
    description: "the strategy the bot trades right now",
    kind: "production",
  },
];

/** The research workspace splits into three jobs so the page is not one long
 * scroll: run and read evaluations, compare strategies head to head, and tune
 * the production strategy (sweeps and the version history they promote). */
type ResearchTab = "evaluate" | "compare" | "tune";
const RESEARCH_TABS: { id: ResearchTab; label: string }[] = [
  { id: "evaluate", label: "Evaluate" },
  { id: "compare", label: "Compare" },
  { id: "tune", label: "Tune" },
];

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

function Metric(props: {
  label: string;
  value: string;
  hint: string;
  tone?: Tone;
  /** When set, a tap-friendly definition appears beside the label. */
  term?: GlossaryTerm;
}) {
  return (
    <StatTile
      label={
        props.term === undefined ? (
          props.label
        ) : (
          <span className="inline-flex items-center gap-1">
            {props.label}
            <InfoTooltip text={props.term.definition} />
          </span>
        )
      }
      value={props.value}
      hint={props.hint}
      tone={props.tone}
    />
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
                <tr
                  key={label}
                  className="border-t border-zinc-200/70 dark:border-zinc-800/60 text-zinc-700 dark:text-zinc-300"
                >
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
  // The R-multiple metrics below say how well the bot traded; this money
  // band says what a fixed stake would have become, so the run reads in
  // money too. Older runs predate the field — show it only when present.
  const startBalance = summary.starting_balance_quote;
  const finalBalance = summary.final_balance_quote;
  const netPnl = summary.net_pnl_quote;
  const moneyTone: Tone =
    typeof netPnl === "string" ? (netPnl.startsWith("-") ? "bad" : "good") : "neutral";
  return (
    <div className="space-y-4">
      <div className={`rounded-lg border p-3 ${TONE_PANEL_CLASS[reading.tone]}`}>
        <div className="text-sm font-bold">{reading.headline}</div>
        <p className="mt-1 text-sm opacity-90">{reading.explanation}</p>
      </div>
      {typeof finalBalance === "string" && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Metric
            label="starting value"
            value={typeof startBalance === "string" ? formatMoney(startBalance) : "—"}
            hint="the stake every strategy is given to start"
          />
          <Metric
            label="ending value"
            value={formatMoney(finalBalance)}
            tone={moneyTone}
            hint="what that stake would be after these trades (1% risk per trade)"
          />
          <Metric
            label="net P/L"
            value={typeof netPnl === "string" ? formatMoney(netPnl) : "—"}
            tone={moneyTone}
            hint="profit or loss on the stake"
          />
          <Metric
            label="return"
            value={formatFractionPercent(
              typeof summary.return_fraction === "string" ? summary.return_fraction : null,
            )}
            tone={moneyTone}
            hint="ending value vs. starting value"
          />
        </div>
      )}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Metric
          label="expectancy (R)"
          value={text(summary.expectancy_r)}
          tone={expectancyTone(summary.expectancy_r)}
          hint="average R per trade — above 0 makes money"
          term={GLOSSARY.expectancy}
        />
        <Metric
          label="profit factor"
          value={text(summary.profit_factor)}
          tone={profitFactorTone(summary.profit_factor)}
          hint="wins ÷ losses — above 1.0 makes money"
          term={GLOSSARY.profitFactor}
        />
        <Metric
          label="win rate"
          value={text(summary.win_rate)}
          hint="alone says little — win size matters more"
          term={GLOSSARY.winRate}
        />
        <Metric
          label="trades / scenarios"
          value={`${text(summary.trade_count)} / ${text(summary.scenario_count)}`}
          hint="sample size — small samples lie"
          term={GLOSSARY.expectancySample}
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
      <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950/60 p-3">
        <h4 className="text-xs uppercase tracking-wide text-zinc-500">what to do next</h4>
        <ol className="mt-1 list-decimal space-y-1 pl-5 text-sm text-zinc-700 dark:text-zinc-300">
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
                className="cursor-pointer border-t border-zinc-200/70 dark:border-zinc-800/60 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800/60"
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
  // Defaults sized so per-trade stats mean something: a year of regimes,
  // and enough sampled moments that a few-percent entry rate still grades
  // a three-digit trade count.
  const [days, setDays] = useState("365");
  const [count, setCount] = useState("1600");
  const [timeframe, setTimeframe] = useState("1h");
  // Which bot the run grades; "production" (the incumbent) is the backend's
  // default and always exists, so it is a safe initial value even before
  // the selector's options have loaded.
  const [strategy, setStrategy] = useState("production");
  const [strategies, setStrategies] = useState<EvaluationStrategyResponse[]>([]);
  const [improver, setImprover] = useState<ImprovementStatusResponse | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [scenarios, setScenarios] = useState<ScenarioSummaryResponse[]>([]);
  const [findings, setFindings] = useState<FindingResponse[]>([]);
  const [replay, setReplay] = useState<ScenarioReplayResponse | null>(null);
  const [sweeps, setSweeps] = useState<SweepResponse[]>([]);
  const [versions, setVersions] = useState<StrategyVersionResponse[]>([]);
  const [suggestions, setSuggestions] = useState<SuggestedEvaluationResponse[]>([]);
  const [comparisons, setComparisons] = useState<ComparisonGroupResponse[]>([]);
  const [comparisonPending, setComparisonPending] = useState(false);
  // Research deliberately leaves auth/token UX to the overview screen, but a
  // poll that keeps failing must not look like a quiet, up-to-date screen:
  // track the last successful refresh so the UI can show it has gone stale.
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [pollStale, setPollStale] = useState(false);
  const [researchTab, setResearchTab] = useState<ResearchTab>("evaluate");

  const suggestionsLoaded = useRef(false);

  const refresh = useCallback(async () => {
    try {
      setRuns(await fetchEvaluations());
      setSweeps(await fetchSweeps());
      setVersions(await fetchStrategyVersions());
      setComparisons(await fetchComparisons());
      // Cheap and current: the selector follows custom bots being created
      // or deleted, and the improver card follows the loop's cycles.
      setStrategies(await fetchEvaluationStrategies());
      setImprover(await fetchImprovementStatus());
      // Suggestions ride the poll only until the first success: stored
      // history depth moves a day at a time, so once loaded they stay put —
      // but a fetch that failed (token not entered yet, transient outage)
      // must retry, or the panel would sit empty until a full reload.
      if (!suggestionsLoaded.current) {
        setSuggestions(await fetchEvaluationSuggestions());
        suggestionsLoaded.current = true;
      }
      setLastUpdated(Date.now());
      setPollStale(false);
    } catch {
      // Auth/error prompts stay with the overview screen; here we only flag
      // that the data on screen is no longer being refreshed.
      setPollStale(true);
    }
  }, []);

  useEffect(() => {
    // Self-scheduling rather than setInterval: the next poll is queued only
    // after the previous one settles, so slow research endpoints can never
    // stack overlapping requests.
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      await refresh();
      if (!cancelled) {
        timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [refresh]);

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
    // Auth/error prompts stay with the overview screen; a failure here only
    // marks the screen stale (see the banner) rather than disappearing silently.
    fetchScenarios(selectedRunId).then(setScenarios, () => {
      setPollStale(true);
    });
    fetchFindings(selectedRunId).then(setFindings, () => {
      setPollStale(true);
    });
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
      // Suggestions size the window; the bot under test stays whatever the
      // form's selector says, so "evaluate breakout on this coin" is one click.
      strategy,
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

  // The backend rejects a second batch while one is in flight (409); the
  // button disables on what we can see and the 409 detail covers the rest
  // (e.g. a sweep started elsewhere) through the shared notice line.
  const comparisonRunning = comparisons.some((group) =>
    group.runs.some((run) => run.status === "running" || run.status === "pending"),
  );

  const handleStartComparison = () => {
    // Empty body on purpose: the backend's defaults give every strategy
    // the identical, fairly sized scenario set.
    setComparisonPending(true);
    startComparison({}).then(
      (started) => {
        setComparisonPending(false);
        setNotice(started.detail);
        void refresh();
      },
      (caught: unknown) => {
        setComparisonPending(false);
        setNotice(caught instanceof Error ? caught.message : "failed to start the comparison");
      },
    );
  };

  const handleStartSweep = () => {
    // No parameters on purpose: the backend's defaults are sized so the
    // sweep clears its minimum-trades bar — inheriting the evaluation
    // form's values here used to starve every sweep into "insufficient
    // evidence" without the user ever seeing why.
    startSweep().then(
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
      {pollStale && (
        <Alert tone="warn" title="not refreshing">
          the research data below may be out of date
          {lastUpdated !== null
            ? ` (last updated ${formatTime(new Date(lastUpdated).toISOString())})`
            : ""}
          . If this persists, check the connection on the overview screen.
        </Alert>
      )}
      <nav className="flex gap-1 rounded-lg bg-zinc-200/60 p-1 dark:bg-zinc-900">
        {RESEARCH_TABS.map((researchNavTab) => (
          <button
            key={researchNavTab.id}
            type="button"
            onClick={() => {
              setResearchTab(researchNavTab.id);
            }}
            className={`rounded-md px-3 py-1.5 text-sm font-semibold ${
              researchTab === researchNavTab.id
                ? "bg-white text-zinc-900 shadow-sm dark:bg-zinc-700 dark:text-zinc-100"
                : "text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-200"
            }`}
          >
            {researchNavTab.label}
          </button>
        ))}
      </nav>
      {researchTab === "evaluate" && (
        <>
          {suggestions.length > 0 && (
            <section className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900 p-4">
              <h3 className="text-xs uppercase tracking-wide text-zinc-500">
                suggested evaluations — fitted to each coin&apos;s stored history
              </h3>
              <div className="mt-2 grid gap-3 sm:grid-cols-3">
                {suggestions.map((suggestion) => (
                  <div
                    key={`${suggestion.symbol}-${suggestion.timeframe}`}
                    className="flex flex-col justify-between rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950/60 p-3"
                  >
                    <div>
                      <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                        {suggestion.title} · {suggestion.symbol}
                      </div>
                      <div className="mt-0.5 text-xs text-zinc-500">
                        {suggestion.timeframe} · {suggestion.history_days} days · ~
                        {suggestion.expected_candles.toLocaleString()} candles
                      </div>
                      <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
                        {suggestion.rationale}
                      </p>
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
            className="flex flex-wrap items-end gap-3 rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900 p-4"
            onSubmit={(event) => {
              event.preventDefault();
              void (async () => {
                try {
                  const started = await startEvaluation({
                    timeframes: [timeframe],
                    history_days: Number(days) || 365,
                    scenario_count: Number(count) || 1600,
                    strategy,
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
            <p className="w-full text-xs text-zinc-500">
              custom evaluation — replays past moments and grades every decision the chosen bot
              would have made; the suggestions above are usually the better start
            </p>
            <label className="text-xs text-zinc-600 dark:text-zinc-400">
              bot
              <select
                value={strategy}
                onChange={(event) => {
                  setStrategy(event.target.value);
                }}
                className="mt-1 block max-w-48 rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-950 px-2 py-1.5 text-sm text-zinc-900 dark:text-zinc-100"
              >
                {(strategies.length > 0 ? strategies : FALLBACK_STRATEGIES).map((option) => (
                  <option key={option.id} value={option.id} title={option.description}>
                    {option.label}
                  </option>
                ))}
              </select>
              <span className="mt-0.5 block text-[11px] text-zinc-400 dark:text-zinc-600">
                whose strategy the run grades
              </span>
            </label>
            <label className="text-xs text-zinc-600 dark:text-zinc-400">
              history (days)
              <input
                value={days}
                onChange={(event) => {
                  setDays(event.target.value);
                }}
                className="mt-1 block w-24 rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-950 px-2 py-1.5 text-sm text-zinc-900 dark:text-zinc-100"
              />
              <span className="mt-0.5 block text-[11px] text-zinc-400 dark:text-zinc-600">
                more days = more market moods covered
              </span>
            </label>
            <label className="text-xs text-zinc-600 dark:text-zinc-400">
              scenarios per coin
              <input
                value={count}
                onChange={(event) => {
                  setCount(event.target.value);
                }}
                className="mt-1 block w-28 rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-950 px-2 py-1.5 text-sm text-zinc-900 dark:text-zinc-100"
              />
              <span className="mt-0.5 block text-[11px] text-zinc-400 dark:text-zinc-600">
                more scenarios = more trades = trustworthy stats
              </span>
            </label>
            <label className="text-xs text-zinc-600 dark:text-zinc-400">
              timeframe
              <select
                value={timeframe}
                onChange={(event) => {
                  setTimeframe(event.target.value);
                }}
                className="mt-1 block rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-950 px-2 py-1.5 text-sm text-zinc-900 dark:text-zinc-100"
              >
                {TIMEFRAMES.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
              <span className="mt-0.5 block text-[11px] text-zinc-400 dark:text-zinc-600">
                candle size the bot decides on
              </span>
            </label>
            <button
              type="submit"
              className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-500"
            >
              start evaluation
            </button>
            {notice && (
              <span className="text-sm text-zinc-600 dark:text-zinc-400">{notice}</span>
            )}
          </form>

          <div className="grid gap-4 lg:grid-cols-[16rem_1fr]">
            <section className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900 p-3">
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
                          ? "bg-zinc-100 dark:bg-zinc-800 text-zinc-900 dark:text-zinc-100"
                          : "text-zinc-600 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-800/60"
                      }`}
                    >
                      run #{run.id} · {run.status}
                      <span className="block text-xs text-zinc-500">
                        {run.strategy} · {run.symbols.join(", ")} · {run.timeframes.join(", ")}{" "}
                        · {run.progress_done}/{run.progress_total}
                      </span>
                    </button>
                    {run.status === "running" && (
                      <button
                        type="button"
                        onClick={() => {
                          void cancelEvaluation(run.id).then(refresh, refresh);
                        }}
                        className="mt-0.5 px-2 text-xs text-red-600 dark:text-red-400 hover:text-red-500 dark:hover:text-red-300"
                      >
                        cancel
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </section>
            <section className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900 p-4">
              {replay ? (
                <ScenarioReplay
                  replay={replay}
                  onBack={() => {
                    setReplay(null);
                  }}
                />
              ) : selected ? (
                <div className="space-y-4">
                  <div className="text-xs text-zinc-500">
                    run #{selected.id} · bot: {selected.strategy}
                  </div>
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
        </>
      )}

      {researchTab === "compare" && (
        <ComparisonPanel
          groups={comparisons}
          onStart={handleStartComparison}
          startDisabled={comparisonPending || comparisonRunning}
        />
      )}

      {researchTab === "tune" && (
        <>
          <ImproverStatusCard status={improver} />
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
        </>
      )}
    </div>
  );
}
