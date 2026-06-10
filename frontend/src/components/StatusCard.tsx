import type { StatusResponse } from "../api/types";
import { formatTime, signClass, trimAmount } from "../lib/format";

function Metric(props: { label: string; value: string; valueClass?: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-zinc-500">{props.label}</div>
      <div className={`text-lg font-semibold ${props.valueClass ?? "text-zinc-100"}`}>
        {props.value}
      </div>
    </div>
  );
}

export function StatusCard(props: { status: StatusResponse }) {
  const { status } = props;
  const quote = status.quote_currency;
  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-5">
      <div className="mb-4 flex items-center gap-3">
        <h2 className="text-xl font-bold text-zinc-100">{status.symbol}</h2>
        <span className="rounded bg-amber-500/20 px-2 py-0.5 text-xs font-bold uppercase text-amber-400">
          {status.mode}
        </span>
        {status.paused && (
          <span className="rounded bg-red-500/20 px-2 py-0.5 text-xs font-bold uppercase text-red-400">
            paused
          </span>
        )}
        <span className="ml-auto text-sm text-zinc-500">
          {status.exchange_id} · last candle {formatTime(status.last_candle_close_time)}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Metric
          label={`equity (${quote})`}
          value={status.equity_quote === null ? "unknown" : trimAmount(status.equity_quote)}
        />
        <Metric label={`balance (${quote})`} value={trimAmount(status.quote_balance)} />
        <Metric
          label={`realized pnl (${quote})`}
          value={trimAmount(status.realized_pnl_quote)}
          valueClass={signClass(status.realized_pnl_quote)}
        />
        <Metric
          label="mark price"
          value={status.mark_price_quote === null ? "—" : trimAmount(status.mark_price_quote)}
        />
      </div>
      <div className="mt-4 border-t border-zinc-800 pt-4">
        {status.position === null ? (
          <div className="text-sm text-zinc-500">flat — no open position</div>
        ) : (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Metric label="position" value={trimAmount(status.position.quantity_base)} />
            <Metric
              label="avg entry"
              value={trimAmount(status.position.average_entry_price_quote)}
            />
            <Metric
              label={`unrealized pnl (${quote})`}
              value={
                status.position.unrealized_pnl_quote === null
                  ? "unknown"
                  : trimAmount(status.position.unrealized_pnl_quote)
              }
              valueClass={signClass(status.position.unrealized_pnl_quote)}
            />
          </div>
        )}
      </div>
    </section>
  );
}
