import type { CompetitionResponse } from "../api/types";
import { formatFractionPercent, signClass, truncateAmount } from "../lib/format";

/** A money cell: truncated for the eye, or a muted dash when unknown. */
function amountCell(amount: string | null): string {
  return amount === null ? "—" : truncateAmount(amount);
}

/**
 * The strategy competition leaderboard: five paper bots — the production
 * regime router plus four single-strategy challengers — trade the same
 * coins from separate paper accounts so their results compare fairly.
 * The backend ranks them best equity first; the rank column numbers that
 * order. Display formatting only — no arithmetic on the amount strings.
 */
export function CompetitionCard(props: { competition: CompetitionResponse | null }) {
  if (props.competition === null) {
    return null;
  }
  const { competition } = props;
  const quote = competition.quote_currency;
  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-5">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h2 className="text-sm font-bold uppercase tracking-wide text-zinc-400">
          strategy competition
        </h2>
        <span className="text-xs text-zinc-500">
          five paper bots trade the same coins, each its own account — best account first
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-zinc-500">
            <tr>
              <th className="py-1 pr-3">#</th>
              <th className="py-1 pr-3">strategy</th>
              <th className="py-1 pr-3">equity ({quote})</th>
              <th className="py-1 pr-3">return</th>
              <th className="py-1 pr-3">realized pnl ({quote})</th>
              <th className="py-1 pr-3" title="positions currently held">
                open
              </th>
              <th className="py-1" title="completed round trips (entries made)">
                trades
              </th>
            </tr>
          </thead>
          <tbody>
            {competition.competitors.map((competitor, index) => (
              <tr key={competitor.bot_id} className="border-t border-zinc-800/60 text-zinc-300">
                <td className="py-1.5 pr-3 text-zinc-500">{index + 1}</td>
                <td className="py-1.5 pr-3">
                  <div className="flex items-center gap-2">
                    <span
                      className="font-semibold text-zinc-100"
                      title={competitor.description}
                    >
                      {competitor.label}
                    </span>
                    {competitor.is_production && (
                      <span
                        title="the configuration the real bot trades with"
                        className="rounded bg-sky-500/20 px-1.5 py-0.5 text-[10px] font-bold uppercase text-sky-400"
                      >
                        production
                      </span>
                    )}
                    {competitor.breaker_tripped_reason !== null && (
                      <span
                        title={`circuit breaker tripped — ${competitor.breaker_tripped_reason}`}
                        className="cursor-help text-amber-400"
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
                  className={`py-1.5 pr-3 font-mono ${
                    competitor.equity_quote === null ? "text-zinc-600" : "text-zinc-100"
                  }`}
                >
                  {amountCell(competitor.equity_quote)}
                </td>
                <td
                  className={`py-1.5 pr-3 font-mono ${signClass(competitor.return_fraction)}`}
                >
                  {formatFractionPercent(competitor.return_fraction)}
                </td>
                <td
                  className={`py-1.5 pr-3 font-mono ${signClass(competitor.realized_pnl_quote)}`}
                >
                  {amountCell(competitor.realized_pnl_quote)}
                </td>
                <td className="py-1.5 pr-3">{competitor.open_positions}</td>
                <td className="py-1.5" title="completed round trips (entries made)">
                  {competitor.exit_fills}{" "}
                  <span className="text-xs text-zinc-500">
                    ({competitor.entry_fills} entries)
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
