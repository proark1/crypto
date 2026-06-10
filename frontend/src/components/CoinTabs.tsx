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
                ? "bg-emerald-600 text-white"
                : "bg-zinc-900 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
            }`}
          >
            {symbol}
          </button>
        );
      })}
    </nav>
  );
}
