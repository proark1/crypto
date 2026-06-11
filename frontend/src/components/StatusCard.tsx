import type { StatusResponse } from "../api/types";
import { formatTime, signClass, trimAmount, truncateAmount } from "../lib/format";

function Metric(props: { label: string; value: string; hint?: string; valueClass?: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-zinc-500">{props.label}</div>
      <div
        className={`text-lg font-semibold ${props.valueClass ?? "text-zinc-900 dark:text-zinc-100"}`}
      >
        {props.value}
      </div>
      {props.hint !== undefined && <div className="text-xs text-zinc-500">{props.hint}</div>}
    </div>
  );
}

export function StatusCard(props: { status: StatusResponse }) {
  const { status } = props;
  const quote = status.quote_currency;
  return (
    <section className="rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900 p-5">
      <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">{status.symbol}</h2>
        <span
          title={
            status.mode === "live"
              ? "trading real money"
              : "practice mode — simulated money, real prices"
          }
          className="rounded bg-amber-500/20 px-2 py-0.5 text-xs font-bold uppercase text-amber-600 dark:text-amber-400"
        >
          {status.mode}
        </span>
        {status.paused && (
          <span className="rounded bg-red-500/20 px-2 py-0.5 text-xs font-bold uppercase text-red-600 dark:text-red-400">
            paused
          </span>
        )}
        {status.regime.enabled && status.regime.label !== null && (
          <span
            className={`rounded px-2 py-0.5 text-xs font-bold uppercase ${
              status.regime.label === "risk_off" || status.regime.label === "warming_up"
                ? "bg-red-500/20 text-red-600 dark:text-red-400"
                : "bg-sky-500/20 text-sky-600 dark:text-sky-400"
            }`}
            title={status.regime.reasons.join("; ")}
          >
            regime: {status.regime.label.replace("_", " ")}
          </span>
        )}
        <span className="w-full text-sm text-zinc-500 sm:ml-auto sm:w-auto sm:text-right">
          {status.exchange_id} · last candle {formatTime(status.last_candle_close_time)}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Metric
          label={`equity (${quote})`}
          value={status.equity_quote === null ? "unknown" : truncateAmount(status.equity_quote)}
          hint="cash plus open positions, priced now"
        />
        <Metric
          label={`balance (${quote})`}
          value={truncateAmount(status.quote_balance)}
          hint="cash not in any position"
        />
        <Metric
          label={`realized pnl (${quote})`}
          value={truncateAmount(status.realized_pnl_quote)}
          valueClass={signClass(status.realized_pnl_quote)}
          hint="profit or loss from closed trades"
        />
        <Metric
          label="mark price"
          value={
            status.mark_price_quote === null ? "—" : truncateAmount(status.mark_price_quote)
          }
          hint="latest price of the coin"
        />
      </div>
      {status.breakers.tripped_reason !== null && (
        <div className="mt-4 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-300">
          <span className="font-bold uppercase">circuit breaker tripped</span> —{" "}
          {status.breakers.tripped_reason}
        </div>
      )}
      {status.breakers.tripped_reason === null && status.breakers.cooldown_until !== null && (
        <div className="mt-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300">
          <span className="font-bold uppercase">loss-streak cooldown</span> — entries blocked
          until {formatTime(status.breakers.cooldown_until)}
        </div>
      )}
      <div className="mt-4 border-t border-zinc-200 dark:border-zinc-800 pt-4">
        {status.position === null ? (
          <div className="text-sm text-zinc-500">flat — no open position</div>
        ) : (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Metric
              label="position"
              value={trimAmount(status.position.quantity_base)}
              hint="how much of the coin the bot holds"
            />
            <Metric
              label="avg entry"
              value={truncateAmount(status.position.average_entry_price_quote)}
              hint="average price it was bought at"
            />
            <Metric
              label={`unrealized pnl (${quote})`}
              value={
                status.position.unrealized_pnl_quote === null
                  ? "unknown"
                  : truncateAmount(status.position.unrealized_pnl_quote)
              }
              valueClass={signClass(status.position.unrealized_pnl_quote)}
              hint="paper profit — not locked in until sold"
            />
            <Metric
              label="protective stop"
              value={
                status.protective_stop_quote === null
                  ? "not armed"
                  : truncateAmount(status.protective_stop_quote)
              }
              valueClass={
                status.protective_stop_quote === null
                  ? "text-red-600 dark:text-red-400"
                  : "text-zinc-900 dark:text-zinc-100"
              }
              hint="auto-sell level guarding the position"
            />
          </div>
        )}
      </div>
    </section>
  );
}
