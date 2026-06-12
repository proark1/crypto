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
  resetBotCapital,
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
  formatMoney,
  formatTime,
  humanizeParamName,
  signClass,
  trimAmount,
  truncateAmount,
} from "../lib/format";
import { useMediaQuery } from "../lib/useMediaQuery";
import {
  Alert,
  ArrowLeftIcon,
  Badge,
  Button,
  Card,
  ConfirmButton,
  PauseIcon,
  PlayIcon,
  SectionHeader,
  StatTile,
} from "../ui";

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

/** One headline metric in its own bordered tile. */
function StatCard(props: { label: string; value: string; hint?: string; valueClass?: string }) {
  return (
    <Card padding="md">
      <StatTile
        label={props.label}
        value={props.value}
        hint={props.hint}
        valueClass={props.valueClass}
      />
    </Card>
  );
}

/** A valid capital draft is a number > 0. Capital is money, but parsing the
 * draft only to validate (never to compute) is allowed — the exact value is
 * sent as a string and the backend does all the arithmetic. */
function capitalDraftError(draft: string): string | null {
  const text = draft.trim();
  if (text === "" || Number.isNaN(Number(text))) {
    return "enter a number";
  }
  if (Number(text) <= 0) {
    return "must be greater than zero";
  }
  return null;
}

/** Per-bot settings: reset the starting capital. Resetting wipes the bot's
 * trade history and restarts it from the new balance, so it is gated on the
 * bot being flat and confirmed before it runs. */
function CapitalSettingsCard(props: {
  currentCapital: string;
  flat: boolean;
  disabled: boolean;
  onReset: (amount: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(props.currentCapital);
  // Keep the field in step with the live value until the user edits it.
  useEffect(() => {
    setDraft(props.currentCapital);
  }, [props.currentCapital]);

  const draftError = capitalDraftError(draft);

  return (
    <Card padding="lg">
      <SectionHeader
        title="Settings"
        description="this bot's starting capital (paper account)"
      />
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        Currently started from{" "}
        <span className="font-medium text-zinc-900 dark:text-zinc-100">
          {formatMoney(props.currentCapital)}
        </span>
        . Changing it <strong>resets this bot&apos;s account</strong> — its balance restarts
        from the new amount and its trade history is cleared. The bot must be flat (no open
        positions or orders) first.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
            Starting capital
          </span>
          <input
            type="number"
            step="any"
            min="0"
            inputMode="decimal"
            aria-label="Starting capital"
            aria-invalid={draftError !== null}
            value={draft}
            disabled={props.disabled || !props.flat}
            onChange={(event) => {
              setDraft(event.target.value);
            }}
            className="mt-1 block w-40 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm text-zinc-900 disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100"
          />
        </label>
        <ConfirmButton
          label="reset capital"
          confirmLabel="confirm: reset account & clear history"
          title={
            props.flat
              ? "reset this bot's balance and wipe its trade history"
              : "the bot must be flat first — stop it, then reset"
          }
          variant="dangerOutline"
          disabled={props.disabled || !props.flat || draftError !== null}
          stopPropagation={false}
          onConfirm={() => void props.onReset(draft.trim())}
        />
      </div>
      {draftError !== null && (
        <span className="mt-1 block text-[11px] text-red-600 dark:text-red-400">
          {draftError}
        </span>
      )}
      {!props.flat && (
        <span className="mt-2 block text-xs text-amber-700 dark:text-amber-400">
          This bot holds a position — stop it (which sells at the next price), then reset.
        </span>
      )}
    </Card>
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

/** One named rule with its description and collapsible parameters. */
function FamilyCard(props: {
  family: string;
  params: Record<string, unknown>;
  options: BotOptionsResponse | null;
}) {
  const meta = familyMeta(props.options, props.family);
  return (
    <div className="rounded-lg border border-zinc-200/70 bg-zinc-50 p-3 dark:border-zinc-800/60 dark:bg-zinc-950/60">
      <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">{meta.label}</div>
      {meta.description !== "" && (
        <p className="mt-0.5 text-xs text-zinc-500">{meta.description}</p>
      )}
      <ParamTable params={props.params} />
    </div>
  );
}

/** Render how a bot trades in plain language, per strategy kind. */
function StrategySection(props: {
  strategy: BotStrategyResponse;
  options: BotOptionsResponse | null;
}) {
  const { strategy, options } = props;
  return (
    <Card padding="lg">
      <SectionHeader title="How it trades" />
      {strategy.kind === "production" && (
        <div className="space-y-3">
          <p className="text-sm text-zinc-700 dark:text-zinc-300">
            Switches between trend following (in trending markets) and mean reversion (in
            calm/sideways markets), based on overall BTC market conditions.
          </p>
          {Object.entries(strategy.families).map(([family, params]) => (
            <FamilyCard key={family} family={family} params={params} options={options} />
          ))}
        </div>
      )}
      {strategy.kind === "builtin" && (
        <div>
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
        <div className="space-y-3">
          <p className="text-sm text-zinc-700 dark:text-zinc-300">
            {strategy.rules.entry_mode === "all"
              ? "Buys only when all of its rules agree (trades less, higher conviction)."
              : "Buys when any of its rules fires (trades more)."}
          </p>
          {Object.entries(strategy.rules.families).map(([family, params]) => (
            <FamilyCard key={family} family={family} params={params} options={options} />
          ))}
        </div>
      )}
    </Card>
  );
}

/**
 * One competing bot in full: header with controls, stat cards, how it trades,
 * open positions, its own trade journal, and its recent decisions in plain
 * words. Positions render as a table on desktop and stacked cards on phones.
 * Polls on the app's usual cadence.
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
  const requestIdRef = useRef(0);
  const isMobile = useMediaQuery("(max-width: 639px)");

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
    // Self-scheduling instead of setInterval: each poll waits for the previous
    // one to settle before the next is queued, so a slow backend can never
    // stack overlapping requests (the request-id guard above is the backstop).
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
    <Button variant="ghost" size="sm" onClick={props.onBack} icon={<ArrowLeftIcon />}>
      back to bots
    </Button>
  );

  if (error !== null && detail === null) {
    return (
      <div className="space-y-4">
        {backButton}
        <Alert tone="error">{error}</Alert>
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
          <Button
            icon={<PlayIcon className="h-4 w-4" />}
            disabled={commandPending}
            onClick={() => void runCommand(() => resumeBot(botId))}
          >
            resume
          </Button>
        ) : (
          <Button
            variant="secondary"
            icon={<PauseIcon className="h-4 w-4" />}
            disabled={commandPending}
            title="stop opening trades — protective stops keep running"
            onClick={() => void runCommand(() => pauseBot(botId))}
          >
            pause
          </Button>
        )}
        <ConfirmButton
          label="stop"
          confirmLabel="confirm: halt the bot and sell its holdings at the next price"
          title="halts the bot and sells its holdings at the next price"
          disabled={commandPending}
          stopPropagation={false}
          onConfirm={() => void runCommand(() => killBot(botId))}
        />
        {summary.kind === "custom" && (
          <>
            <Button
              variant="ghost"
              disabled={commandPending}
              onClick={() => {
                props.onEdit(botId);
              }}
            >
              edit rules
            </Button>
            <Button
              variant="dangerOutline"
              disabled={commandPending || holdsSomething}
              title={
                holdsSomething
                  ? "this bot still holds positions — stop it first, then delete"
                  : "remove this bot and its account for good"
              }
              onClick={() => void handleDelete()}
            >
              delete
            </Button>
          </>
        )}
      </div>
      {notice !== null && <Alert tone="info">{notice}</Alert>}
      {summary.breaker_tripped_reason !== null && (
        <Alert tone="error" title="circuit breaker tripped">
          {summary.breaker_tripped_reason}
        </Alert>
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
          label="realized P/L"
          value={truncateAmount(summary.realized_pnl_quote)}
          valueClass={signClass(summary.realized_pnl_quote)}
          hint="profit or loss from closed trades"
        />
        <StatCard
          label="unrealized P/L"
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

      <CapitalSettingsCard
        currentCapital={summary.initial_balance_quote}
        flat={!holdsSomething}
        disabled={commandPending}
        onReset={(amount) => runCommand(() => resetBotCapital(botId, amount))}
      />

      <StrategySection strategy={strategy} options={options} />

      <Card padding="none">
        <SectionHeader
          title="Open positions"
          description="what this bot holds right now"
          className="border-b border-zinc-200 px-4 py-3 dark:border-zinc-800"
        />
        {positions.length === 0 ? (
          <div className="px-4 py-3 text-sm text-zinc-500">flat — no open positions</div>
        ) : isMobile ? (
          <ul className="divide-y divide-zinc-200/70 dark:divide-zinc-800/60">
            {positions.map((position) => (
              <li key={position.symbol} className="px-4 py-3">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-zinc-900 dark:text-zinc-100">
                    {position.symbol}
                  </span>
                  <span
                    className={`font-mono text-sm ${signClass(position.unrealized_pnl_quote)}`}
                  >
                    {position.unrealized_pnl_quote === null
                      ? "—"
                      : truncateAmount(position.unrealized_pnl_quote)}
                  </span>
                </div>
                <div className="mt-1 text-xs text-zinc-500">
                  {trimAmount(position.quantity_base)} @{" "}
                  {truncateAmount(position.average_entry_price_quote)} · now{" "}
                  {position.mark_price_quote === null
                    ? "—"
                    : truncateAmount(position.mark_price_quote)}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs text-zinc-500">
                <tr>
                  <th className="px-4 py-2">coin</th>
                  <th className="px-4 py-2">amount</th>
                  <th className="px-4 py-2">entry price</th>
                  <th className="px-4 py-2">current price</th>
                  <th className="px-4 py-2">unrealized P/L</th>
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
                    <td className="px-4 py-2 font-mono">
                      {trimAmount(position.quantity_base)}
                    </td>
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
          </div>
        )}
      </Card>

      <FillsTable fills={fills} />

      <Card padding="none">
        <SectionHeader
          title="Recent decisions"
          description={`what this bot wanted to do${symbol === null ? "" : ` on ${symbol}`} and what came of it`}
          className="border-b border-zinc-200 px-4 py-3 dark:border-zinc-800"
        />
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
      </Card>
    </div>
  );
}
