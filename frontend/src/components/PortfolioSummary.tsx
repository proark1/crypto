/**
 * The dashboard hero: the headline numbers for the bot that would eventually
 * trade real money (the production "main bot"), so the landing screen answers
 * "how am I doing?" before any scrolling. Reads the production competitor when
 * the leaderboard is loaded and falls back to the live status otherwise.
 * Display formatting only — never arithmetic on the amount strings.
 */
import type { CompetitionResponse, StatusResponse } from "../api/types";
import { formatFractionPercent, signClass, truncateAmount } from "../lib/format";
import { Badge, Card, SectionHeader, StatTile } from "../ui";

function amount(value: string | null): string {
  return value === null ? "—" : truncateAmount(value);
}

export function PortfolioSummary(props: {
  status: StatusResponse | null;
  competition: CompetitionResponse | null;
}) {
  const { status, competition } = props;
  const main = competition?.competitors.find((bot) => bot.is_production) ?? null;
  const quote = competition?.quote_currency ?? status?.quote_currency ?? "";

  // Prefer the production bot's account; fall back to the status feed so the
  // hero still reads before the leaderboard's first poll lands.
  const equity = main?.equity_quote ?? status?.equity_quote ?? null;
  const realized = main?.realized_pnl_quote ?? status?.realized_pnl_quote ?? null;
  const returnFraction = main?.return_fraction ?? null;
  const unrealized = main?.unrealized_pnl_quote ?? null;

  return (
    <Card padding="lg">
      <SectionHeader
        title="Portfolio"
        description="the main bot — the one that would eventually trade real money"
        action={
          status && <Badge tone={status.mode === "live" ? "red" : "amber"}>{status.mode}</Badge>
        }
      />
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <StatTile
          label={`equity (${quote})`}
          value={amount(equity)}
          hint="cash plus open positions, priced now"
        />
        <StatTile
          label="return"
          value={formatFractionPercent(returnFraction)}
          valueClass={signClass(returnFraction)}
          hint="profit or loss since the start"
        />
        <StatTile
          label={`realized P/L (${quote})`}
          value={amount(realized)}
          valueClass={signClass(realized)}
          hint="locked in from closed trades"
        />
        <StatTile
          label={`unrealized P/L (${quote})`}
          value={amount(unrealized)}
          valueClass={signClass(unrealized)}
          hint="paper profit on open positions"
        />
      </div>
    </Card>
  );
}
