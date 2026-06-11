/**
 * A banner for a condition the user should notice: an error, a warning that
 * trading is gated, or a neutral notice. Status cards previously stacked up to
 * four bespoke red/amber boxes; routing them through one component keeps the
 * colour, icon, and emphasis consistent and lets a later pass prioritise them
 * into a single area. Each tone pairs a colour with an icon so the severity
 * survives a grayscale screenshot.
 */
import type { ReactNode } from "react";

import { AlertTriangleIcon, CheckIcon, InfoIcon, XIcon } from "./icons";

export type AlertTone = "error" | "warn" | "info" | "success";

const TONES: Record<AlertTone, { panel: string; icon: ReactNode }> = {
  error: {
    panel: "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300",
    icon: <XIcon className="mt-0.5 h-4 w-4 shrink-0" />,
  },
  warn: {
    panel: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
    icon: <AlertTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />,
  },
  info: {
    panel:
      "border-zinc-300 bg-white text-zinc-700 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300",
    icon: <InfoIcon className="mt-0.5 h-4 w-4 shrink-0" />,
  },
  success: {
    panel: "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    icon: <CheckIcon className="mt-0.5 h-4 w-4 shrink-0" />,
  },
};

export function Alert(props: {
  children: ReactNode;
  tone?: AlertTone;
  /** Bold lead-in shown before the body (e.g. "Circuit breaker tripped"). */
  title?: string;
}) {
  const tone = TONES[props.tone ?? "info"];
  return (
    <div className={`flex gap-2 rounded-lg border px-4 py-2 text-sm ${tone.panel}`}>
      {tone.icon}
      <div>
        {props.title !== undefined && (
          <span className="font-bold uppercase">{props.title}</span>
        )}
        {props.title !== undefined && " — "}
        {props.children}
      </div>
    </div>
  );
}
