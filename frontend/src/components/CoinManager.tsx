/**
 * Runtime add/remove of traded coins. Removal uses the shared inline confirm
 * (not a blocking window.confirm, which interrupts the page and reads poorly
 * on touch) and targets the currently selected coin — the one whose data is on
 * screen — so there is never ambiguity about what is being stopped.
 */
import { useState } from "react";

import { Button, ConfirmButton } from "../ui";

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
        className="w-full min-w-0 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm text-zinc-900 placeholder:text-zinc-400 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100 dark:placeholder:text-zinc-600 sm:w-56"
      />
      <Button type="submit" variant="secondary" size="sm" disabled={props.disabled}>
        add coin
      </Button>
      <span className="ml-auto">
        <ConfirmButton
          size="sm"
          label={`remove ${props.selected}`}
          confirmLabel={`stop ${props.selected} — history kept`}
          title={`stop trading ${props.selected}; its history stays`}
          disabled={props.disabled}
          onConfirm={() => {
            props.onRemove(props.selected);
          }}
          stopPropagation={false}
        />
      </span>
    </form>
  );
}
