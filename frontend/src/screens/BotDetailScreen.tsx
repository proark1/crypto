import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  deleteBot,
  fetchBot,
  fetchBotOptions,
  fetchDecisions,
  fetchFills,
  killBot,
  pauseBot,
  resumeBot,
} from "../api/client";
import type {
  BotDetailResponse,
  BotOptionsResponse,
  BotStrategyResponse,
  DecisionResponse,
  FillResponse,
  StrategyFamilyOption,
} from "../api/types";
import { FillsTable } from "../components/FillsTable";
import {
  formatFractionPercent,
  formatTime,
  humanizeParamName,
  signClass,
  trimAmount,
  truncateAmount,
} from "../lib/format";

const POLL_INTERVAL_MS = 5000;

const MAIN_BOT_HINT =
  "this is the bot that would eventually trade real money; the others are experiments competing against it";

/** Decision outcomes in plain words for non-technical readers. */
const OUTCOME_PLAIN: Record<string, string> = {
  submitted: "executed",
  vetoed: "blocked by a safety check",
  gated: "blocked by a safety check (market conditions)",
  paused: "skipped — trading was paused",
};

function plainOutcome(outcome: string): string {
  return OUTCOME_PLAIN[outcome] ?? outcome.replace(/_/g, " ");
}

function Badge(props: {
  tone: "sky" | "violet" | "zinc" | "amber";
  title?: string;
  children: string;
}) {
  const tones = {
    sky: "bg-sky-100 text-sky-700 dark:bg-sky-500/20 dark:text-sky-400",
    violet: "bg-violet-100 text-violet-700 dark:bg-violet-500/20 dark:text-violet-400",
    zinc: "bg-zinc-200 text-zinc-600 dark:bg-zinc-700/60 dark:text-zinc-300",
    amber: "bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-400",
  } as const;
  return (
    <span
      title={props.title}
      className={`rounded px-2 py-0.5 text-xs font-bold uppercase ${tones[props.tone]}`}
    >
      {props.children}
    </span>
  );
}

function StatCard(props: { label: string; value: string; hint?: string; valueClass?: string }) {
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <div className="text-xs uppercase tracking-wide text-zinc-500">{props.label}</div>
      <div
        className={`mt-1 text-lg font-semibold ${
          props.valueClass ?? "text-zinc-900 dark:text-zinc-100"
        }`}
      >
        {props.value}
      </div>
      {props.hint !== undefined && (
        <div className="mt-0.5 text-xs text-zinc-500">{props.hint}</div>
      )}
    </div>
  );
}

/** A collapsible raw-parameter table with humanized names. */
function ParamTable(props: { params: Record<string, unknown> }) {
  const entries = Object.entries(props.params);
  if (entries.length === 0) {
    return null;
  }
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-xs font-semibold text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300">
        advanced settings ({entries.length})
      </summary>
      <table className="mt-1 w-full text-left text-xs">
        <tbody>
          {entries.map(([key, value]) => (
            <tr key={key} className="border-t border-zinc-200/70 dark:border-zinc-800/60">
              <td className="py-1 pr-3 text-zinc-600 dark:text-zinc-400">
                {humanizeParamName(key)}
              </td>
              <td className="py-1 font-mono text-zinc-800 dark:text-zinc-200">
                {String(value)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function familyMeta(
  options: BotOptionsResponse | null,
  family: string,
): Pick<StrategyFamilyOption, "label" | "description"> {
  const match = options?.families.find((candidate) => candidate.family === family);
  return {
    label: match?.label ?? humanizeParamName(family),
    description: match?.description ?? "",
  };
}

/** Render how a bot trades in plain language, per strategy kind. */
function StrategySection(props: {
  strategy: BotStrategyResponse;
  options: BotOptionsResponse | null;
}) {
  const { strategy, options } = props;
  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <h3 className="text-sm font-bold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
        how it trades
      </h3>
      {strategy.kind === "production" && (
        <div className="mt-2 space-y-3">
          <p className="text-sm text-zinc-700 dark:text-zinc-300">
            Switches between trend following (in trending markets) and mean reversion (in
            calm/sideways markets), based on overall BTC market conditions.
          </p>
          {Object.entries(strategy.families).map(([family, params]) => {
            const meta = familyMeta(options, family);
            return (
              <div
                key={family}
                className="rounded-lg border border-zinc-200/70 bg-zinc-50 p-3 dark:border-zinc-800/60 dark:bg-zinc-950/60"
              >
                <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                  {meta.label}
                </div>
                {meta.description !== "" && (
                  <p className="mt-0.5 text-xs text-zinc-500">{meta.description}</p>
                )}
                <ParamTable params={params} />
              </div>
            );
          })}
        </div>
      )}
      {strategy.kind === "builtin" && (
        <div className="mt-2">
          <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            {familyMeta(options, strategy.family).label}
          </div>
          {familyMeta(options, strategy.family).description !== "" && (
            <p className="mt-0.5 text-sm text-zinc-700 dark:text-zinc-300">
              {familyMeta(options, strategy.family).description}
            </p>
          )}
          <ParamTable params={strategy.params} />
        </div>
      )}
      {strategy.kind === "custom" && (
        <div className="mt-2 space-y-3">
          <p className="text-sm text-zinc-700 dark:text-zinc-300">
            {strategy.rules.entry_mode === "all"
              ? "Buys only when all of its rules agree (trades less, higher conviction)."
              : "Buys when any of its rules fires (trades more)."}
          </p>
          {Object.entries(strategy.rules.families).map(([family, params]) => {
            const meta = familyMeta(options, family);
            return (
              <div
                key={family}
                className="rounded-lg border border-zinc-200/70 bg-zinc-50 p-3 dark:border-zinc-800/60 dark:bg-zinc-950/60"
              >
                <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                  {meta.label}
                </div>
                {meta.description !== "" && (
                  <p className="mt-0.5 text-xs text-zinc-500">{meta.description}</p>
                )}
                <ParamTable params={params} />
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

/**
 * One competing bot in full: header with controls, stat cards, how it
 * trades, open positions, its own trade journal, and its recent decisions
 * in plain words. Polls on the app's usual cadence.
 */
export function BotDetailScreen(props: {
  botId: string;
  /** The app's selected coin — scopes the decision trail. */
  symbol: string | null;
  onBack: () => void;
  onEdit: (botId: string) => void;
  onDeleted: () => void;
}) {
  const [detail, setDetail] = useState<BotDetailResponse | null>(null);
  const [fills, setFills] = useState<FillResponse[]>([]);
  const [decisions, setDecisions] = useState<DecisionResponse[]>([]);
  const [options, setOptions] = useState<BotOptionsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [commandPending, setCommandPending] = useState(false);
  const [confirmingStop, setConfirmingStop] = useState(false);
  const requestIdRef = useRef(0);

  const { botId, symbol } = props;

  const refresh = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    try {
      // Fills and decisions are explainability; their endpoints failing
      // must not blank the whole page.
      const [nextDetail, nextFills, nextDecisions] = await Promise.all([
        fetchBot(botId),
        fetchFills(botId).catch(() => null),
        fetchDecisions(symbol ?? undefined, botId).catch(() => null),
      ]);
      if (requestId !== requestIdRef.current) {
        return;
      }
      setDetail(nextDetail);
      if (nextFills !== null) {
        setFills(nextFills);
      }
      if (nextDecisions !== null) {
        setDecisions(nextDecisions);
      }
      setError(null);
    } catch (caught) {
      if (requestId !== requestIdRef.current) {
        return;
      }
      if (caught instanceof ApiError && caught.status === 404) {
        setError("this bot no longer exists");
      } else {
        setError(caught instanceof Error ? caught.message : "request failed");
      }
    }
  }, [botId, symbol]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => {
      clearInterval(timer);
    };
  }, [refresh]);

  useEffect(() => {
    // Family labels/descriptions are static; fetch them once and degrade
    // to humanized ids if the endpoint is unavailable.
    fetchBotOptions().then(setOptions, () => undefined);
  }, []);

  const runCommand = useCallback(
    async (command: () => Promise<{ detail: string }>) => {
      setCommandPending(true);
      try {
        const result = await command();
        setNotice(result.detail);
        await refresh();
      } catch (caught) {
        setNotice(caught instanceof Error ? caught.message : "command failed");
      } finally {
        setCommandPending(false);
      }
    },
    [refresh],
  );

  const handleDelete = useCallback(async () => {
    setCommandPending(true);
    try {
      const result = await deleteBot(botId);
      setNotice(result.detail);
      props.onDeleted();
    } catch (caught) {
      // The 409 detail ("stop the bot first") surfaces verbatim.
      setNotice(caught instanceof Error ? caught.message : "delete failed");
    } finally {
      setCommandPending(false);
    }
  }, [botId, props]);

  const backButton = (
    <button
      type="button"
      onClick={props.onBack}
      className="rounded-lg border border-zinc-300 px-3 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
    >
      ← back to overview
    </button>
  );

  if (error !== null && detail === null) {
    return (
      <div className="space-y-4">
        {backButton}
        <div className="rounded-lg border border-red-300 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/50 dark:text-red-300">
          {error}
        </div>
      </div>
    );
  }
  if (detail === null) {
    return (
      <div className="space-y-4">
        {backButton}
        <div className="text-sm text-zinc-500">loading…</div>
      </div>
    );
  }

  const { summary, positions, strategy } = detail;
  const holdsSomething = summary.open_positions > 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        {backButton}
        <h2 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">{summary.label}</h2>
        {summary.is_production && (
          <Badge tone="sky" title={MAIN_BOT_HINT}>
            main bot
          </Badge>
        )}
        {summary.kind === "builtin" && (
          <Badge tone="zinc" title="one of the standard challenger bots">
            built-in
          </Badge>
        )}
        {summary.kind === "custom" && (
          <Badge tone="violet" title="a bot you built from rules">
            custom
          </Badge>
        )}
        {summary.paused && (
          <Badge tone="amber" title="paused — not opening trades right now">
            paused
          </Badge>
        )}
      </div>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">{summary.description}</p>

      <div className="flex flex-wrap items-center gap-2">
        {summary.paused ? (
          <button
            type="button"
            disabled={commandPending}
            onClick={() => void runCommand(() => resumeBot(botId))}
            className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            resume
          </button>
        ) : (
          <button
            type="button"
            disabled={commandPending}
            title="stop opening trades — protective stops keep running"
            onClick={() => void runCommand(() => pauseBot(botId))}
            className="rounded-lg bg-zinc-200 px-4 py-2 text-sm font-semibold text-zinc-800 hover:bg-zinc-300 disabled:opacity-50 dark:bg-zinc-700 dark:text-white dark:hover:bg-zinc-600"
          >
            pause
          </button>
        )}
        {confirmingStop ? (
          <>
            <button
              type="button"
              disabled={commandPending}
              onClick={() => {
                setConfirmingStop(false);
                void runCommand(() => killBot(botId));
              }}
              className="rounded-lg bg-red-600 px-4 py-2 text-sm font-bold text-white hover:bg-red-500 disabled:opacity-50"
            >
              confirm: halt the bot and sell its holdings at the next price
            </button>
            <button
              type="button"
              disabled={commandPending}
              onClick={() => {
                setConfirmingStop(false);
              }}
              className="rounded-lg border border-zinc-300 px-4 py-2 text-sm text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
            >
              cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            disabled={commandPending}
            title="halts the bot and sells its holdings at the next price"
            onClick={() => {
              setConfirmingStop(true);
            }}
            className="rounded-lg border border-red-300 px-4 py-2 text-sm font-semibold text-red-600 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950/40 disabled:opacity-50"
          >
            stop
          </button>
        )}
        {summary.kind === "custom" && (
          <>
            <button
              type="button"
              disabled={commandPending}
              onClick={() => {
                props.onEdit(botId);
              }}
              className="rounded-lg border border-zinc-300 px-4 py-2 text-sm text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
            >
              edit rules
            </button>
            <button
              type="button"
              disabled={commandPending || holdsSomething}
              title={
                holdsSomething
                  ? "this bot still holds positions — stop it first, then delete"
                  : "remove this bot and its account for good"
              }
              onClick={() => void handleDelete()}
              className="rounded-lg border border-red-300 px-4 py-2 text-sm text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950/40"
            >
              delete
            </button>
          </>
        )}
      </div>
      {notice !== null && (
        <div className="rounded-lg border border-zinc-300 bg-white px-4 py-2 text-sm text-zinc-700 shadow-sm dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300">
          {notice}
        </div>
      )}
      {summary.breaker_tripped_reason !== null && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-900 dark:text-red-300">
          <span className="font-bold uppercase">circuit breaker tripped</span> —{" "}
          {summary.breaker_tripped_reason}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <StatCard
          label="equity"
          value={
            summary.equity_quote === null ? "unknown" : truncateAmount(summary.equity_quote)
          }
          hint="cash plus open positions, priced now"
        />
        <StatCard
          label="return"
          value={formatFractionPercent(summary.return_fraction)}
          valueClass={signClass(summary.return_fraction)}
          hint={`profit since its ${truncateAmount(summary.initial_balance_quote)} start`}
        />
        <StatCard
          label="realized pnl"
          value={truncateAmount(summary.realized_pnl_quote)}
          valueClass={signClass(summary.realized_pnl_quote)}
          hint="profit or loss from closed trades"
        />
        <StatCard
          label="unrealized pnl"
          value={
            summary.unrealized_pnl_quote === null
              ? "—"
              : truncateAmount(summary.unrealized_pnl_quote)
          }
          valueClass={signClass(summary.unrealized_pnl_quote)}
          hint="paper profit on open positions"
        />
        <StatCard
          label="open positions"
          value={String(summary.open_positions)}
          hint="coins currently held"
        />
        <StatCard
          label="trades"
          value={String(summary.exit_fills)}
          hint={`completed round trips (${String(summary.entry_fills)} entries)`}
        />
      </div>

      <StrategySection strategy={strategy} options={options} />

      <section className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <h3 className="border-b border-zinc-200 px-4 py-3 text-xs uppercase tracking-wide text-zinc-500 dark:border-zinc-800">
          <span className="font-bold">open positions</span>
          <span className="ml-2 normal-case tracking-normal">
            — what this bot holds right now
          </span>
        </h3>
        {positions.length === 0 ? (
          <div className="px-4 py-3 text-sm text-zinc-500">flat — no open positions</div>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-zinc-500">
              <tr>
                <th className="px-4 py-2">coin</th>
                <th className="px-4 py-2">amount</th>
                <th className="px-4 py-2">entry price</th>
                <th className="px-4 py-2">current price</th>
                <th className="px-4 py-2">unrealized pnl</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((position) => (
                <tr
                  key={position.symbol}
                  className="border-t border-zinc-200/70 text-zinc-700 dark:border-zinc-800/60 dark:text-zinc-300"
                >
                  <td className="px-4 py-2 font-semibold text-zinc-900 dark:text-zinc-100">
                    {position.symbol}
                  </td>
                  <td className="px-4 py-2 font-mono">{trimAmount(position.quantity_base)}</td>
                  <td className="px-4 py-2 font-mono">
                    {truncateAmount(position.average_entry_price_quote)}
                  </td>
                  <td className="px-4 py-2 font-mono">
                    {position.mark_price_quote === null
                      ? "—"
                      : truncateAmount(position.mark_price_quote)}
                  </td>
                  <td
                    className={`px-4 py-2 font-mono ${signClass(position.unrealized_pnl_quote)}`}
                  >
                    {position.unrealized_pnl_quote === null
                      ? "—"
                      : truncateAmount(position.unrealized_pnl_quote)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <FillsTable fills={fills} />

      <section className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <h3 className="border-b border-zinc-200 px-4 py-3 text-xs uppercase tracking-wide text-zinc-500 dark:border-zinc-800">
          <span className="font-bold">recent decisions</span>
          <span className="ml-2 normal-case tracking-normal">
            — what this bot wanted to do{symbol === null ? "" : ` on ${symbol}`} and what came
            of it
          </span>
        </h3>
        {decisions.length === 0 ? (
          <div className="px-4 py-3 text-sm text-zinc-500">no decisions yet</div>
        ) : (
          <ul>
            {decisions.map((decision) => (
              <li
                key={decision.signal_id + decision.outcome}
                className="border-b border-zinc-200/70 px-4 py-3 last:border-b-0 dark:border-zinc-800/50"
              >
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span
                    className={`text-sm font-bold uppercase ${
                      decision.side === "buy"
                        ? "text-emerald-600 dark:text-emerald-400"
                        : "text-red-600 dark:text-red-400"
                    }`}
                  >
                    {decision.side}
                  </span>
                  <span className="text-sm text-zinc-700 dark:text-zinc-300">
                    {decision.symbol}
                  </span>
                  <span
                    className={`rounded px-2 py-0.5 text-xs font-semibold ${
                      decision.outcome === "submitted"
                        ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-500/20 dark:text-emerald-400"
                        : "bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-400"
                    }`}
                  >
                    {plainOutcome(decision.outcome)}
                  </span>
                  <span className="ml-auto text-xs text-zinc-500">
                    {formatTime(decision.created_at)}
                  </span>
                </div>
                {decision.reasons.length > 0 && (
                  <ul className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
                    {decision.reasons.map((reason, index) => (
                      <li key={`${String(index)}-${reason}`}>· {reason}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
