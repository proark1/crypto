import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchFills } from "../api/client";
import type { FillResponse } from "../api/types";
import { formatMoney, formatTime, trimAmount } from "../lib/format";
import { useMediaQuery } from "../lib/useMediaQuery";
import { ArrowDownIcon, ArrowUpIcon, Button, Card, SectionHeader } from "../ui";

/** How many older fills to pull per "load older" click. */
const OLDER_PAGE_SIZE = 100;

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
 * table on desktop and stacked cards on phones so the columns never force a
 * horizontal scroll. Display formatting only — the trade value (notional in
 * quote currency) arrives precomputed from the backend.
 *
 * `fills` is the live (polled) window — the newest page — owned by the parent.
 * Older history loads on demand through the `before_id` cursor; pages stay in
 * local state and merge with the live window by id, so a poll refreshing the
 * newest page never discards what the operator paged back to. `bot` scopes the
 * cursor fetch to one competition account (the production journal by default).
 */
export function FillsTable(props: { fills: FillResponse[]; bot?: string }) {
  const isMobile = useMediaQuery("(max-width: 639px)");
  const { fills, bot } = props;
  const [older, setOlder] = useState<FillResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [exhausted, setExhausted] = useState(false);

  // Paged history belongs to one journal scope; a different bot starts over.
  useEffect(() => {
    setOlder([]);
    setExhausted(false);
  }, [bot]);

  // Merge the loaded older pages with the live window, de-duped by id (the
  // live version wins on overlap) and ordered newest-first for display.
  const newestFirst = useMemo(() => {
    const byId = new Map<number, FillResponse>();
    for (const fill of older) {
      byId.set(fill.id, fill);
    }
    for (const fill of fills) {
      byId.set(fill.id, fill);
    }
    return [...byId.values()].sort((a, b) => b.id - a.id);
  }, [older, fills]);

  const oldestId = newestFirst.at(-1)?.id;

  const loadOlder = useCallback(async () => {
    if (oldestId === undefined) {
      return;
    }
    setLoading(true);
    try {
      const page = await fetchFills(bot, { beforeId: oldestId, limit: OLDER_PAGE_SIZE });
      if (page.length < OLDER_PAGE_SIZE) {
        setExhausted(true);
      }
      if (page.length > 0) {
        setOlder((current) => [...current, ...page]);
      }
    } catch {
      // A failed page load must not blank the journal; leave the button so
      // the operator can retry on the next click.
    } finally {
      setLoading(false);
    }
  }, [bot, oldestId]);

  if (newestFirst.length === 0) {
    return (
      <Card padding="lg">
        <span className="text-sm text-zinc-500">
          no trades yet — executed buys and sells will appear here
        </span>
      </Card>
    );
  }

  return (
    <Card padding="none">
      <SectionHeader
        title="Trades"
        description="every executed buy and sell across all coins, newest first"
        className="border-b border-zinc-200 px-4 py-3 dark:border-zinc-800"
      />
      {isMobile ? (
        <ul className="divide-y divide-zinc-200/70 dark:divide-zinc-800/60">
          {newestFirst.map((fill) => (
            <li key={fill.id} className="px-4 py-3">
              <div className="flex items-center justify-between">
                <Side side={fill.side} />
                <span className="text-sm text-zinc-700 dark:text-zinc-300">{fill.symbol}</span>
              </div>
              <div className="mt-1 font-mono text-sm text-zinc-900 dark:text-zinc-100">
                {trimAmount(fill.quantity_base)} @ {trimAmount(fill.price_quote)}
              </div>
              <div className="mt-0.5 text-xs text-zinc-500">
                {formatMoney(fill.value_quote)} value · fee {trimAmount(fill.fee_quote)}
              </div>
              <div className="mt-0.5 text-xs text-zinc-500">{formatTime(fill.filled_at)}</div>
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
                <th className="px-4 py-3">value</th>
                <th className="px-4 py-3">fee</th>
                <th className="px-4 py-3">order</th>
              </tr>
            </thead>
            <tbody>
              {newestFirst.map((fill) => (
                <tr
                  key={fill.id}
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
                  <td className="px-4 py-2 text-zinc-900 dark:text-zinc-100">
                    {formatMoney(fill.value_quote)}
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
      <div className="flex items-center justify-center border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
        {exhausted ? (
          <span className="text-xs text-zinc-500">— start of journal —</span>
        ) : (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void loadOlder()}
            disabled={loading}
          >
            {loading ? "loading…" : "load older trades"}
          </Button>
        )}
      </div>
    </Card>
  );
}
