/**
 * A compact top-of-the-leaderboard for the dashboard: the best few paper bots
 * with just their rank, equity, and return, plus a way through to the full
 * competition. The dense, controllable leaderboard lives on the Bots tab; this
 * is the at-a-glance version so the landing screen stays short. Rows open the
 * bot's detail page. Display formatting only.
 */
import type { CompetitionResponse } from "../api/types";
import { formatFractionPercent, signClass, truncateAmount } from "../lib/format";
import { Badge, Button, Card, ChevronRightIcon, SectionHeader } from "../ui";

const TOP_N = 3;

export function MiniLeaderboard(props: {
  competition: CompetitionResponse | null;
  onViewAll: () => void;
  onSelectBot: (botId: string) => void;
}) {
  if (props.competition === null || props.competition.competitors.length === 0) {
    return null;
  }
  const { competition } = props;
  const top = competition.competitors.slice(0, TOP_N);
  return (
    <Card padding="lg">
      <SectionHeader
        title="Leaderboard"
        description="best paper accounts first"
        action={
          <Button
            variant="ghost"
            size="sm"
            onClick={props.onViewAll}
            icon={<ChevronRightIcon />}
          >
            view all
          </Button>
        }
      />
      <ul className="divide-y divide-zinc-200/70 dark:divide-zinc-800/60">
        {top.map((bot, index) => (
          <li key={bot.bot_id}>
            <button
              type="button"
              onClick={() => {
                props.onSelectBot(bot.bot_id);
              }}
              className={`flex w-full items-center gap-3 py-2 text-left hover:bg-zinc-50 dark:hover:bg-zinc-800/40 ${
                bot.paused ? "opacity-60" : ""
              }`}
            >
              <span className="w-4 text-sm text-zinc-500">{index + 1}</span>
              <span className="flex min-w-0 flex-1 items-center gap-2">
                <span className="truncate font-semibold text-zinc-900 dark:text-zinc-100">
                  {bot.label}
                </span>
                {bot.is_production && <Badge tone="sky">main</Badge>}
                {bot.kind === "custom" && <Badge tone="violet">custom</Badge>}
              </span>
              <span className="font-mono text-sm text-zinc-900 dark:text-zinc-100">
                {bot.equity_quote === null ? "—" : truncateAmount(bot.equity_quote)}
              </span>
              <span
                className={`w-20 text-right font-mono text-sm ${signClass(bot.return_fraction)}`}
              >
                {formatFractionPercent(bot.return_fraction)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </Card>
  );
}
