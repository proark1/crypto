import type { FillResponse } from "../api/types";
import { formatTime, trimAmount } from "../lib/format";

export function FillsTable(props: { fills: FillResponse[] }) {
  if (props.fills.length === 0) {
    return (
      <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-5 text-sm text-zinc-500">
        no trades yet — executed buys and sells will appear here
      </section>
    );
  }
  return (
    <section className="overflow-x-auto rounded-xl border border-zinc-800 bg-zinc-900">
      <h3 className="border-b border-zinc-800 px-4 py-3 text-xs uppercase tracking-wide text-zinc-500">
        <span className="font-bold">trades</span>
        <span className="ml-2 normal-case tracking-normal">
          — every executed buy and sell across all coins, newest first
        </span>
      </h3>
      <table className="w-full text-left text-sm">
        <thead className="border-b border-zinc-800 text-xs uppercase tracking-wide text-zinc-500">
          <tr>
            <th className="px-4 py-3">time</th>
            <th className="px-4 py-3">coin</th>
            <th className="px-4 py-3">side</th>
            <th className="px-4 py-3">quantity</th>
            <th className="px-4 py-3">price</th>
            <th className="px-4 py-3">fee</th>
            <th className="px-4 py-3">order</th>
          </tr>
        </thead>
        <tbody>
          {[...props.fills].reverse().map((fill, index) => (
            <tr
              key={`${fill.client_order_id}-${String(index)}`}
              className="border-b border-zinc-800/50"
            >
              <td className="px-4 py-2 text-zinc-400">{formatTime(fill.filled_at)}</td>
              <td className="px-4 py-2 text-zinc-300">{fill.symbol}</td>
              <td
                className={`px-4 py-2 font-bold uppercase ${
                  fill.side === "buy" ? "text-emerald-400" : "text-red-400"
                }`}
              >
                {fill.side}
              </td>
              <td className="px-4 py-2 text-zinc-100">{trimAmount(fill.quantity_base)}</td>
              <td className="px-4 py-2 text-zinc-100">{trimAmount(fill.price_quote)}</td>
              <td className="px-4 py-2 text-zinc-400">{trimAmount(fill.fee_quote)}</td>
              <td className="px-4 py-2 font-mono text-xs text-zinc-500">
                {fill.client_order_id}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
