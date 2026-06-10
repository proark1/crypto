import { useState } from "react";

/**
 * Runtime add/remove of traded coins. Removal asks for confirmation and
 * targets the currently selected coin — the one whose data is on screen —
 * so there is never ambiguity about what is being stopped.
 */
export function CoinManager(props: {
  selected: string;
  disabled?: boolean;
  onAdd: (symbol: string) => void;
  onRemove: (symbol: string) => void;
}) {
  const [draft, setDraft] = useState("");
  return (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        const symbol = draft.trim().toUpperCase();
        if (symbol !== "") {
          props.onAdd(symbol);
          setDraft("");
        }
      }}
    >
      <input
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value);
        }}
        disabled={props.disabled}
        placeholder="add coin, e.g. ETH/USDT"
        className="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-sm text-zinc-100 placeholder:text-zinc-600"
      />
      <button
        type="submit"
        disabled={props.disabled}
        className="rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-semibold text-zinc-200 hover:bg-zinc-700"
      >
        add coin
      </button>
      <button
        type="button"
        disabled={props.disabled}
        onClick={() => {
          if (window.confirm(`Stop trading ${props.selected}? Its history stays.`)) {
            props.onRemove(props.selected);
          }
        }}
        className="ml-auto rounded-lg border border-red-900 px-3 py-1.5 text-sm font-semibold text-red-400 hover:bg-red-950/40"
      >
        remove {props.selected}
      </button>
    </form>
  );
}
