import { useState } from "react";

import type { CompetitionResponse, CompetitorResponse } from "../api/types";
import { formatFractionPercent, signClass, truncateAmount } from "../lib/format";

/** A money cell: truncated for the eye, or a muted dash when unknown. */
function amountCell(amount: string | null): string {
  return amount === null ? "—" : truncateAmount(amount);
}

const MAIN_BOT_HINT =
  "this is the bot that would eventually trade real money; the others are experiments competing against it";

function Badge(props: { tone: "sky" | "violet" | "zinc"; title?: string; children: string }) {
  const tones = {
    sky: "bg-sky-100 text-sky-700 dark:bg-sky-500/20 dark:text-sky-400",
    violet: "bg-violet-100 text-violet-700 dark:bg-violet-500/20 dark:text-violet-400",
    zinc: "bg-zinc-200 text-zinc-600 dark:bg-zinc-700/60 dark:text-zinc-300",
  } as const;
  return (
    <span
      title={props.title}
      className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${tones[props.tone]}`}
    >
      {props.children}
    </span>
  );
}

/** The pause/resume + stop quick controls for one leaderboard row. The
 * stop button asks for an inline confirm because it sells the bot's
 * holdings; clicks never bubble into the row's open-details navigation. */
function RowControls(props: {
  competitor: CompetitorResponse;
  disabled: boolean;
  confirmingStop: boolean;
  onPause: () => void;
  onResume: () => void;
  onKill: () => void;
  onConfirmStop: (confirming: boolean) => void;
}) {
  const { competitor } = props;
  if (props.confirmingStop) {
    return (
      <span className="flex items-center justify-end gap-1.5">
        <button
          type="button"
          disabled={props.disabled}
          onClick={(event) => {
            event.stopPropagation();
            props.onConfirmStop(false);
            props.onKill();
          }}
          className="rounded-md bg-red-600 px-2 py-1 text-xs font-bold text-white hover:bg-red-500 disabled:opacity-50"
        >
          sell &amp; stop
        </button>
        <button
          type="button"
          disabled={props.disabled}
          onClick={(event) => {
            event.stopPropagation();
            props.onConfirmStop(false);
          }}
          className="rounded-md border border-zinc-300 px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
        >
          cancel
        </button>
      </span>
    );
  }
  return (
    <span className="flex items-center justify-end gap-1.5">
      {competitor.paused ? (
        <button
          type="button"
          disabled={props.disabled}
          title="let this bot trade again"
          onClick={(event) => {
            event.stopPropagation();
            props.onResume();
          }}
          className="rounded-md bg-emerald-600 px-2 py-1 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
        >
          resume
        </button>
      ) : (
        <button
          type="button"
          disabled={props.disabled}
          title="pause this bot — it stops opening trades; protective stops keep running"
          onClick={(event) => {
            event.stopPropagation();
            props.onPause();
          }}
          className="rounded-md border border-zinc-300 px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800 disabled:opacity-50"
        >
          pause
        </button>
      )}
      <button
        type="button"
        disabled={props.disabled}
        title="stop this bot and sell its holdings at the next price — asks to confirm first"
        onClick={(event) => {
          event.stopPropagation();
          props.onConfirmStop(true);
        }}
        className="rounded-md border border-red-300 px-2 py-1 text-xs font-semibold text-red-600 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950/40 disabled:opacity-50"
      >
        stop
      </button>
    </span>
  );
}

/**
 * The strategy competition leaderboard: paper bots — the production regime
 * router (the "main bot") plus built-in and user-built challengers — trade
 * the same coins from separate paper accounts so their results compare
 * fairly. The backend ranks them best equity first; the rank column numbers
 * that order. Rows open the bot's detail page. Display formatting only —
 * no arithmetic on the amount strings.
 */
export function CompetitionCard(props: {
  competition: CompetitionResponse | null;
  disabled?: boolean;
  onSelectBot: (botId: string) => void;
  onCreateBot: () => void;
  onPauseBot: (botId: string) => void;
  onResumeBot: (botId: string) => void;
  onKillBot: (botId: string) => void;
}) {
  const [confirmingStopId, setConfirmingStopId] = useState<string | null>(null);
  if (props.competition === null) {
    return null;
  }
  const { competition } = props;
  const disabled = props.disabled ?? false;
  const quote = competition.quote_currency;
  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <h2 className="text-sm font-bold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
            bot competition
          </h2>
          <span className="text-xs text-zinc-500">
            these paper bots trade the same coins, each from its own account — best account
            first; click a bot for details
          </span>
        </div>
        <button
          type="button"
          onClick={props.onCreateBot}
          className="ml-auto whitespace-nowrap rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-500"
        >
          create a bot
        </button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-zinc-500">
            <tr>
              <th className="py-1 pr-3">#</th>
              <th className="py-1 pr-3">bot</th>
              <th className="py-1 pr-3" title="what the account is worth right now">
                equity ({quote})
              </th>
              <th className="py-1 pr-3" title="profit or loss since the start, in percent">
                return
              </th>
              <th className="py-1 pr-3" title="profit or loss from finished trades">
                realized pnl ({quote})
              </th>
              <th className="py-1 pr-3" title="positions currently held">
                open
              </th>
              <th className="py-1 pr-3" title="completed round trips (entries made)">
                trades
              </th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {competition.competitors.map((competitor, index) => (
              <tr
                key={competitor.bot_id}
                onClick={() => {
                  props.onSelectBot(competitor.bot_id);
                }}
                className={`cursor-pointer border-t border-zinc-200/70 text-zinc-700 transition-colors hover:bg-zinc-50 dark:border-zinc-800/60 dark:text-zinc-300 dark:hover:bg-zinc-800/40 ${
                  competitor.paused ? "opacity-60" : ""
                }`}
              >
                <td className="py-2 pr-3 text-zinc-500">{index + 1}</td>
                <td className="py-2 pr-3">
                  <div className="flex items-center gap-2">
                    <span
                      className="font-semibold text-zinc-900 dark:text-zinc-100"
                      title={competitor.description}
                    >
                      {competitor.label}
                    </span>
                    {competitor.is_production && (
                      <Badge tone="sky" title={MAIN_BOT_HINT}>
                        main bot
                      </Badge>
                    )}
                    {competitor.kind === "custom" && (
                      <Badge tone="violet" title="a bot you built from rules">
                        custom
                      </Badge>
                    )}
                    {competitor.paused && (
                      <Badge tone="zinc" title="paused — not opening trades right now">
                        paused
                      </Badge>
                    )}
                    {competitor.breaker_tripped_reason !== null && (
                      <span
                        title={`circuit breaker tripped — ${competitor.breaker_tripped_reason}`}
                        className="cursor-help text-amber-600 dark:text-amber-400"
                      >
                        ⚠
                      </span>
                    )}
                  </div>
                  <span className="block max-w-xs truncate text-xs text-zinc-500">
                    {competitor.description}
                  </span>
                </td>
                <td
                  className={`py-2 pr-3 font-mono ${
                    competitor.equity_quote === null
                      ? "text-zinc-400 dark:text-zinc-600"
                      : "text-zinc-900 dark:text-zinc-100"
                  }`}
                >
                  {amountCell(competitor.equity_quote)}
                </td>
                <td className={`py-2 pr-3 font-mono ${signClass(competitor.return_fraction)}`}>
                  {formatFractionPercent(competitor.return_fraction)}
                </td>
                <td
                  className={`py-2 pr-3 font-mono ${signClass(competitor.realized_pnl_quote)}`}
                >
                  {amountCell(competitor.realized_pnl_quote)}
                </td>
                <td className="py-2 pr-3">{competitor.open_positions}</td>
                <td className="py-2 pr-3" title="completed round trips (entries made)">
                  {competitor.exit_fills}{" "}
                  <span className="text-xs text-zinc-500">
                    ({competitor.entry_fills} entries)
                  </span>
                </td>
                <td className="py-2 text-right">
                  <RowControls
                    competitor={competitor}
                    disabled={disabled}
                    confirmingStop={confirmingStopId === competitor.bot_id}
                    onPause={() => {
                      props.onPauseBot(competitor.bot_id);
                    }}
                    onResume={() => {
                      props.onResumeBot(competitor.bot_id);
                    }}
                    onKill={() => {
                      props.onKillBot(competitor.bot_id);
                    }}
                    onConfirmStop={(confirming) => {
                      setConfirmingStopId(confirming ? competitor.bot_id : null);
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
