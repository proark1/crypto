/**
 * A single labelled metric: small caps label, headline value, optional hint.
 * Three separate screens each defined their own near-identical `Metric`
 * component; this is the shared one so a value and its caption line up the
 * same way everywhere. Tone colours the value through the shared tone map.
 */
import type { ReactNode } from "react";

import { TONE_TEXT_CLASS, type Tone } from "./tone";

export function StatTile(props: {
  label: ReactNode;
  value: string;
  hint?: ReactNode;
  /** Colours the value semantically (e.g. P/L). Defaults to neutral. */
  tone?: Tone;
  /** Escape hatch for a one-off value colour when tone does not fit. */
  valueClass?: string;
}) {
  const valueClass = props.valueClass ?? TONE_TEXT_CLASS[props.tone ?? "neutral"];
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-zinc-500">{props.label}</div>
      <div className={`text-lg font-semibold ${valueClass}`}>{props.value}</div>
      {props.hint !== undefined && <div className="text-xs text-zinc-500">{props.hint}</div>}
    </div>
  );
}
