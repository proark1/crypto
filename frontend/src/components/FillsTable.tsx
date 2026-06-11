import type { FillResponse } from "../api/types";
import { formatTime, trimAmount } from "../lib/format";
import { useMediaQuery } from "../lib/useMediaQuery";
import { ArrowDownIcon, ArrowUpIcon, Card, SectionHeader } from "../ui";

/** The buy/sell direction, colour paired with an arrow so it reads at a
 * glance and survives a grayscale view. */
function Side(props: { side: string }) {
  const buy = props.side === "buy";
  return (
    <span
      className={`inline-flex items-center gap-1 font-bold uppercase ${
        buy ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"
      }`}
    >
      {buy ? (
        <ArrowUpIcon className="h-3.5 w-3.5" />
      ) : (
        <ArrowDownIcon className="h-3.5 w-3.5" />
      )}
      {props.side}
    </span>
  );
}

/**
 * The trade journal: every executed buy and sell, newest first. Renders a
 * table on desktop and stacked cards on phones so the seven columns never
 * force a horizontal scroll. Display formatting only.
 */
export function FillsTable(props: { fills: FillResponse[] }) {
  const isMobile = useMediaQuery("(max-width: 639px)");
  if (props.fills.length === 0) {
    return (
      <Card padding="lg">
        <span className="text-sm text-zinc-500">
          no trades yet — executed buys and sells will appear here
        </span>
      </Card>
    );
  }
  const newestFirst = [...props.fills].reverse();
  return (
    <Card padding="none">
      <SectionHeader
        title="Trades"
        description="every executed buy and sell across all coins, newest first"
        className="border-b border-zinc-200 px-4 py-3 dark:border-zinc-800"
      />
      {isMobile ? (
        <ul className="divide-y divide-zinc-200/70 dark:divide-zinc-800/60">
          {newestFirst.map((fill, index) => (
            <li key={`${fill.client_order_id}-${String(index)}`} className="px-4 py-3">
              <div className="flex items-center justify-between">
                <Side side={fill.side} />
                <span className="text-sm text-zinc-700 dark:text-zinc-300">{fill.symbol}</span>
              </div>
              <div className="mt-1 font-mono text-sm text-zinc-900 dark:text-zinc-100">
                {trimAmount(fill.quantity_base)} @ {trimAmount(fill.price_quote)}
              </div>
              <div className="mt-0.5 text-xs text-zinc-500">
                {formatTime(fill.filled_at)} · fee {trimAmount(fill.fee_quote)}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-zinc-200 text-xs uppercase tracking-wide text-zinc-500 dark:border-zinc-800">
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
              {newestFirst.map((fill, index) => (
                <tr
                  key={`${fill.client_order_id}-${String(index)}`}
                  className="border-b border-zinc-200/70 dark:border-zinc-800/50"
                >
                  <td className="px-4 py-2 text-zinc-600 dark:text-zinc-400">
                    {formatTime(fill.filled_at)}
                  </td>
                  <td className="px-4 py-2 text-zinc-700 dark:text-zinc-300">{fill.symbol}</td>
                  <td className="px-4 py-2">
                    <Side side={fill.side} />
                  </td>
                  <td className="px-4 py-2 text-zinc-900 dark:text-zinc-100">
                    {trimAmount(fill.quantity_base)}
                  </td>
                  <td className="px-4 py-2 text-zinc-900 dark:text-zinc-100">
                    {trimAmount(fill.price_quote)}
                  </td>
                  <td className="px-4 py-2 text-zinc-600 dark:text-zinc-400">
                    {trimAmount(fill.fee_quote)}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-zinc-500">
                    {fill.client_order_id}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
