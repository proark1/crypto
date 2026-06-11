import type { CompetitionResponse, CompetitorResponse } from "../api/types";
import { formatFractionPercent, signClass, truncateAmount } from "../lib/format";
import { useMediaQuery } from "../lib/useMediaQuery";
import { AlertTriangleIcon, Badge, Button, Card, ConfirmButton, SectionHeader } from "../ui";

/** A money cell: truncated for the eye, or a muted dash when unknown. */
function amountCell(amount: string | null): string {
  return amount === null ? "—" : truncateAmount(amount);
}

const MAIN_BOT_HINT =
  "this is the bot that would eventually trade real money; the others are experiments competing against it";

/** The provenance badges for one bot, shared by the table and the cards. */
function BotBadges(props: { competitor: CompetitorResponse }) {
  const { competitor } = props;
  return (
    <>
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
          <AlertTriangleIcon className="inline h-4 w-4" />
        </span>
      )}
    </>
  );
}

/** Pause/resume + stop for one bot. The stop sells the bot's holdings, so it
 * asks for an inline confirm; every click stops propagating so it never opens
 * the row's detail navigation. */
function BotControls(props: {
  competitor: CompetitorResponse;
  disabled: boolean;
  onPause: () => void;
  onResume: () => void;
  onKill: () => void;
}) {
  const { competitor } = props;
  return (
    <span className="flex items-center justify-end gap-1.5">
      {competitor.paused ? (
        <Button
          variant="primary"
          size="sm"
          disabled={props.disabled}
          title="let this bot trade again"
          onClick={(event) => {
            event.stopPropagation();
            props.onResume();
          }}
        >
          resume
        </Button>
      ) : (
        <Button
          variant="secondary"
          size="sm"
          disabled={props.disabled}
          title="pause this bot — it stops opening trades; protective stops keep running"
          onClick={(event) => {
            event.stopPropagation();
            props.onPause();
          }}
        >
          pause
        </Button>
      )}
      <ConfirmButton
        size="sm"
        label="stop"
        confirmLabel="sell & stop"
        title="stop this bot and sell its holdings at the next price — asks to confirm first"
        disabled={props.disabled}
        onConfirm={props.onKill}
      />
    </span>
  );
}

/** One leaderboard row for the desktop table. */
function BotRow(props: {
  competitor: CompetitorResponse;
  rank: number;
  disabled: boolean;
  onSelect: () => void;
  onPause: () => void;
  onResume: () => void;
  onKill: () => void;
}) {
  const { competitor } = props;
  return (
    <tr
      onClick={props.onSelect}
      className={`cursor-pointer border-t border-zinc-200/70 text-zinc-700 transition-colors hover:bg-zinc-50 dark:border-zinc-800/60 dark:text-zinc-300 dark:hover:bg-zinc-800/40 ${
        competitor.paused ? "opacity-60" : ""
      }`}
    >
      <td className="py-2 pr-3 text-zinc-500">{props.rank}</td>
      <td className="py-2 pr-3">
        <div className="flex items-center gap-2">
          <span
            className="font-semibold text-zinc-900 dark:text-zinc-100"
            title={competitor.description}
          >
            {competitor.label}
          </span>
          <BotBadges competitor={competitor} />
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
      <td className={`py-2 pr-3 font-mono ${signClass(competitor.realized_pnl_quote)}`}>
        {amountCell(competitor.realized_pnl_quote)}
      </td>
      <td className="py-2 pr-3">{competitor.open_positions}</td>
      <td className="py-2 pr-3" title="completed round trips (entries made)">
        {competitor.exit_fills}{" "}
        <span className="text-xs text-zinc-500">({competitor.entry_fills} entries)</span>
      </td>
      <td className="py-2 text-right">
        <BotControls
          competitor={competitor}
          disabled={props.disabled}
          onPause={props.onPause}
          onResume={props.onResume}
          onKill={props.onKill}
        />
      </td>
    </tr>
  );
}

/** A labelled stat for the stacked mobile card, so each number keeps its
 * meaning without a column header to lean on. */
function CardStat(props: { label: string; value: string; valueClass?: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-zinc-500">{props.label}</div>
      <div
        className={`font-mono text-sm ${props.valueClass ?? "text-zinc-900 dark:text-zinc-100"}`}
      >
        {props.value}
      </div>
    </div>
  );
}

/** One leaderboard entry as a stacked card for narrow screens, replacing the
 * eight-column horizontal scroll the table would force on a phone. */
function BotCard(props: {
  competitor: CompetitorResponse;
  rank: number;
  quote: string;
  disabled: boolean;
  onSelect: () => void;
  onPause: () => void;
  onResume: () => void;
  onKill: () => void;
}) {
  const { competitor } = props;
  return (
    <div
      onClick={props.onSelect}
      className={`cursor-pointer rounded-lg border border-zinc-200 p-3 dark:border-zinc-800 ${
        competitor.paused ? "opacity-60" : ""
      }`}
    >
      <div className="flex items-start gap-2">
        <span className="text-sm text-zinc-500">{props.rank}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-zinc-900 dark:text-zinc-100">
              {competitor.label}
            </span>
            <BotBadges competitor={competitor} />
          </div>
          <p className="truncate text-xs text-zinc-500">{competitor.description}</p>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2">
        <CardStat
          label={`equity (${props.quote})`}
          value={amountCell(competitor.equity_quote)}
        />
        <CardStat
          label="return"
          value={formatFractionPercent(competitor.return_fraction)}
          valueClass={signClass(competitor.return_fraction)}
        />
        <CardStat
          label={`realized (${props.quote})`}
          value={amountCell(competitor.realized_pnl_quote)}
          valueClass={signClass(competitor.realized_pnl_quote)}
        />
        <CardStat label="open" value={String(competitor.open_positions)} />
        <CardStat
          label="trades"
          value={`${String(competitor.exit_fills)} (${String(competitor.entry_fills)} in)`}
        />
      </div>
      <div className="mt-3">
        <BotControls
          competitor={competitor}
          disabled={props.disabled}
          onPause={props.onPause}
          onResume={props.onResume}
          onKill={props.onKill}
        />
      </div>
    </div>
  );
}

/**
 * The strategy competition leaderboard: paper bots — the production regime
 * router (the "main bot") plus built-in and user-built challengers — trade the
 * same coins from separate paper accounts so their results compare fairly. The
 * backend ranks them best equity first; the rank column numbers that order.
 * Rows open the bot's detail page. A dense table on desktop, stacked cards on
 * phones (no horizontal scroll). Display formatting only — no arithmetic on
 * the amount strings.
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
  const isMobile = useMediaQuery("(max-width: 639px)");
  if (props.competition === null) {
    return null;
  }
  const { competition } = props;
  const disabled = props.disabled ?? false;
  const quote = competition.quote_currency;

  return (
    <Card padding="lg">
      <SectionHeader
        title="Bot competition"
        description="paper bots trading the same coins, each from its own account — best account first; tap a bot for details"
        action={
          <Button size="sm" onClick={props.onCreateBot}>
            create a bot
          </Button>
        }
      />
      {isMobile ? (
        <div className="space-y-2">
          {competition.competitors.map((competitor, index) => (
            <BotCard
              key={competitor.bot_id}
              competitor={competitor}
              rank={index + 1}
              quote={quote}
              disabled={disabled}
              onSelect={() => {
                props.onSelectBot(competitor.bot_id);
              }}
              onPause={() => {
                props.onPauseBot(competitor.bot_id);
              }}
              onResume={() => {
                props.onResumeBot(competitor.bot_id);
              }}
              onKill={() => {
                props.onKillBot(competitor.bot_id);
              }}
            />
          ))}
        </div>
      ) : (
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
                <BotRow
                  key={competitor.bot_id}
                  competitor={competitor}
                  rank={index + 1}
                  disabled={disabled}
                  onSelect={() => {
                    props.onSelectBot(competitor.bot_id);
                  }}
                  onPause={() => {
                    props.onPauseBot(competitor.bot_id);
                  }}
                  onResume={() => {
                    props.onResumeBot(competitor.bot_id);
                  }}
                  onKill={() => {
                    props.onKillBot(competitor.bot_id);
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
