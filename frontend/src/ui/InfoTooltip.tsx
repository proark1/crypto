/**
 * A tap-friendly explanation. The app leaned on the native `title` attribute
 * for nearly all of its help text, which never appears on touch devices —
 * exactly where a trading dashboard most needs to explain itself. This shows
 * a small info affordance that reveals its text on hover, focus, or tap, and
 * dismisses on blur or a second tap, so the explanation is reachable without
 * a mouse and announced to assistive tech.
 */
import { useState } from "react";
import type { ReactNode } from "react";

import { InfoIcon } from "./icons";

export function InfoTooltip(props: {
  /** Accessible label and the revealed text. */
  text: string;
  /** Optional trigger content; defaults to a small info icon. */
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-flex items-center">
      <button
        type="button"
        aria-label={props.text}
        aria-expanded={open}
        className="inline-flex items-center rounded text-zinc-400 hover:text-zinc-600 focus:text-zinc-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 dark:hover:text-zinc-200 dark:focus:text-zinc-200"
        onClick={(event) => {
          event.stopPropagation();
          setOpen((value) => !value);
        }}
        onPointerEnter={() => {
          setOpen(true);
        }}
        onPointerLeave={() => {
          setOpen(false);
        }}
        onFocus={() => {
          setOpen(true);
        }}
        onBlur={() => {
          setOpen(false);
        }}
      >
        {props.children ?? <InfoIcon className="h-3.5 w-3.5" />}
      </button>
      {open && (
        <span
          role="tooltip"
          className="absolute bottom-full left-1/2 z-10 mb-1 w-48 -translate-x-1/2 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs font-normal normal-case text-zinc-700 shadow-md dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200"
        >
          {props.text}
        </span>
      )}
    </span>
  );
}
