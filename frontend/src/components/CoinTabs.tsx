/**
 * Coin switcher for multi-symbol bots. Renders nothing when only one coin
 * is configured — a single-coin dashboard should not grow chrome for a
 * feature it does not use.
 */
export function CoinTabs(props: {
  symbols: string[];
  selected: string;
  disabled?: boolean;
  onSelect: (symbol: string) => void;
}) {
  if (props.symbols.length < 2) {
    return null;
  }
  return (
    <nav className="flex flex-wrap gap-2" aria-label="coins">
      {props.symbols.map((symbol) => {
        const active = symbol === props.selected;
        return (
          <button
            key={symbol}
            type="button"
            disabled={props.disabled}
            aria-current={active ? "page" : undefined}
            onClick={() => {
              props.onSelect(symbol);
            }}
            className={`rounded-lg px-3 py-1.5 text-sm font-semibold transition-colors ${
              active
                ? "border border-emerald-600 bg-emerald-600 text-white"
                : "border border-zinc-200 bg-white text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
            }`}
          >
            {symbol}
          </button>
        );
      })}
    </nav>
  );
}
