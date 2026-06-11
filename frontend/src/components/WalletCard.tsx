import type { WalletResponse } from "../api/types";
import { signClass, trimAmount, truncateAmount } from "../lib/format";

/**
 * What the paper account holds right now: free quote currency plus every
 * coin position, valued at the latest mark. Quote amounts truncate for
 * the eye; coin quantities keep full precision — 0.0334 BTC rounded to
 * 0.03 would misstate the holding by 10%.
 */
export function WalletCard(props: { wallet: WalletResponse | null }) {
  if (props.wallet === null) {
    return null;
  }
  const { wallet } = props;
  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-5">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h2 className="text-sm font-bold uppercase tracking-wide text-zinc-400">wallet</h2>
        <span className="text-xs text-zinc-600">
          what the account holds, valued at the latest prices
        </span>
        <div className="ml-auto text-sm text-zinc-400">
          total ≈{" "}
          <span className="font-semibold text-zinc-100">
            {wallet.equity_quote === null
              ? "unknown"
              : `${truncateAmount(wallet.equity_quote)} ${wallet.quote_currency}`}
          </span>
        </div>
      </div>
      <ul className="divide-y divide-zinc-800/60">
        {wallet.holdings.map((holding) => (
          <li key={holding.asset} className="flex items-baseline gap-3 py-2">
            <span className="w-14 font-bold text-zinc-100">{holding.asset}</span>
            <span className="font-mono text-sm text-zinc-200">
              {holding.symbol === null
                ? truncateAmount(holding.quantity)
                : trimAmount(holding.quantity)}
            </span>
            {holding.symbol !== null && (
              <span className="text-xs text-zinc-500">
                ≈{" "}
                {holding.value_quote === null
                  ? "unpriced"
                  : `${truncateAmount(holding.value_quote)} ${wallet.quote_currency}`}
                {holding.mark_price_quote !== null &&
                  ` @ ${truncateAmount(holding.mark_price_quote)}`}
              </span>
            )}
            {holding.unrealized_pnl_quote !== null && (
              <span className={`ml-auto text-xs ${signClass(holding.unrealized_pnl_quote)}`}>
                {truncateAmount(holding.unrealized_pnl_quote)} unrealized
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
