import type { StatusResponse } from "../api/types";
import { formatTime, signClass, trimAmount, truncateAmount } from "../lib/format";
import { Badge, Card, StatTile } from "../ui";
import { StatusAlerts } from "./StatusAlerts";

/** The provenance/state badges beside the symbol. The mode is always shown
 * (paper vs live is the most consequential fact); the rest appear only when
 * they apply. */
function StatusBadges(props: { status: StatusResponse }) {
  const { status } = props;
  const regimeLabel = status.regime.label;
  return (
    <>
      <Badge
        tone={status.mode === "live" ? "red" : "amber"}
        title={
          status.mode === "live"
            ? "trading real money"
            : "practice mode — simulated money, real prices"
        }
      >
        {status.mode}
      </Badge>
      {status.paused && (
        <Badge tone="red" title="not opening new positions">
          paused
        </Badge>
      )}
      {status.regime.enabled && regimeLabel !== null && (
        <Badge
          tone={regimeLabel === "risk_off" || regimeLabel === "warming_up" ? "red" : "sky"}
          title={status.regime.reasons.join("; ")}
        >
          regime: {regimeLabel.replace("_", " ")}
        </Badge>
      )}
      {!status.data_health.healthy && (
        <Badge tone="red" title={status.data_health.reason ?? "market data is degraded"}>
          data degraded
        </Badge>
      )}
    </>
  );
}

/**
 * The selected coin's live trading state, in three tiers: the header (symbol,
 * state badges, feed freshness) and any blocking alerts come first so a
 * problem is seen before the numbers; then the account headline metrics; then
 * the open position, or a plain "flat" line. Display formatting only.
 */
export function StatusCard(props: { status: StatusResponse }) {
  const { status } = props;
  const quote = status.quote_currency;
  return (
    <Card padding="lg">
      <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">{status.symbol}</h2>
        <StatusBadges status={status} />
        <span className="w-full text-sm text-zinc-500 sm:ml-auto sm:w-auto sm:text-right">
          {status.exchange_id} · last candle {formatTime(status.last_candle_close_time)}
        </span>
      </div>
      <StatusAlerts status={status} className="mb-4" />
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <StatTile
          label={`equity (${quote})`}
          value={status.equity_quote === null ? "unknown" : truncateAmount(status.equity_quote)}
          hint="cash plus open positions, priced now"
        />
        <StatTile
          label={`balance (${quote})`}
          value={truncateAmount(status.quote_balance)}
          hint="cash not in any position"
        />
        <StatTile
          label={`realized P/L (${quote})`}
          value={truncateAmount(status.realized_pnl_quote)}
          valueClass={signClass(status.realized_pnl_quote)}
          hint="profit or loss from closed trades"
        />
        <StatTile
          label="mark price"
          value={
            status.mark_price_quote === null ? "—" : truncateAmount(status.mark_price_quote)
          }
          hint="latest price of the coin"
        />
      </div>
      <div className="mt-4 border-t border-zinc-200 pt-4 dark:border-zinc-800">
        {status.position === null ? (
          <div className="text-sm text-zinc-500">flat — no open position</div>
        ) : (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <StatTile
              label="position"
              value={trimAmount(status.position.quantity_base)}
              hint="how much of the coin the bot holds"
            />
            <StatTile
              label="avg entry"
              value={truncateAmount(status.position.average_entry_price_quote)}
              hint="average price it was bought at"
            />
            <StatTile
              label={`unrealized P/L (${quote})`}
              value={
                status.position.unrealized_pnl_quote === null
                  ? "unknown"
                  : truncateAmount(status.position.unrealized_pnl_quote)
              }
              valueClass={signClass(status.position.unrealized_pnl_quote)}
              hint="paper profit — not locked in until sold"
            />
            <StatTile
              label="protective stop"
              value={
                status.protective_stop_quote === null
                  ? "not armed"
                  : truncateAmount(status.protective_stop_quote)
              }
              valueClass={
                status.protective_stop_quote === null
                  ? "text-red-600 dark:text-red-400"
                  : undefined
              }
              hint="auto-sell level guarding the position"
            />
          </div>
        )}
      </div>
    </Card>
  );
}
