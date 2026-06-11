/**
 * A small status pill. Badges were defined three different ways across the
 * app (leaderboard cells, detail headers, replay chips) with drifting tones
 * and sizes; this is the single one. An optional leading icon keeps the
 * meaning legible without relying on the tone colour alone.
 */
import type { ReactNode } from "react";

export type BadgeTone = "sky" | "violet" | "zinc" | "amber" | "emerald" | "red";

const TONES: Record<BadgeTone, string> = {
  sky: "bg-sky-100 text-sky-700 dark:bg-sky-500/20 dark:text-sky-400",
  violet: "bg-violet-100 text-violet-700 dark:bg-violet-500/20 dark:text-violet-400",
  zinc: "bg-zinc-200 text-zinc-600 dark:bg-zinc-700/60 dark:text-zinc-300",
  amber: "bg-amber-500/20 text-amber-600 dark:text-amber-400",
  emerald: "bg-emerald-500/20 text-emerald-600 dark:text-emerald-400",
  red: "bg-red-500/20 text-red-600 dark:text-red-400",
};

export function Badge(props: {
  children: ReactNode;
  tone?: BadgeTone;
  /** Native tooltip; a tap-friendly explanation should use InfoTooltip. */
  title?: string;
  icon?: ReactNode;
}) {
  return (
    <span
      title={props.title}
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${TONES[props.tone ?? "zinc"]}`}
    >
      {props.icon}
      {props.children}
    </span>
  );
}
