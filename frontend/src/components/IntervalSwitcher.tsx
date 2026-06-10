import type { ChartInterval } from "../api/types";

const INTERVALS: { value: ChartInterval; label: string }[] = [
  { value: "1m", label: "1m" },
  { value: "1h", label: "1H" },
  { value: "1d", label: "1D" },
  { value: "1w", label: "1W" },
  { value: "1M", label: "1M" },
];

/** Chart timeframe selector: raw minutes or calendar buckets. */
export function IntervalSwitcher(props: {
  selected: ChartInterval;
  disabled?: boolean;
  onSelect: (interval: ChartInterval) => void;
}) {
  return (
    <nav aria-label="chart interval" className="flex gap-1 rounded-lg bg-zinc-900 p-1">
      {INTERVALS.map((interval) => (
        <button
          key={interval.value}
          type="button"
          disabled={props.disabled}
          aria-pressed={props.selected === interval.value}
          onClick={() => {
            props.onSelect(interval.value);
          }}
          className={`rounded-md px-2.5 py-1 text-xs font-semibold ${
            props.selected === interval.value
              ? "bg-zinc-700 text-zinc-100"
              : "text-zinc-400 hover:text-zinc-200"
          }`}
        >
          {interval.label}
        </button>
      ))}
    </nav>
  );
}
