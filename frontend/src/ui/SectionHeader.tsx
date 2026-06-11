/**
 * The standard section header: a title, an optional one-line description, and
 * an optional action pinned to the right (a "create" button, a switcher).
 * Section titles were previously a grab-bag of sizes and casings; this fixes
 * the altitude — Title Case, a single weight — so every card reads the same.
 */
import type { ReactNode } from "react";

export function SectionHeader(props: {
  title: string;
  description?: string;
  /** Rendered at the right edge, vertically centred with the title. */
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 ${props.className ?? ""}`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h2 className="text-base font-bold text-zinc-900 dark:text-zinc-100">{props.title}</h2>
        {props.description !== undefined && (
          <span className="text-xs text-zinc-500 dark:text-zinc-400">{props.description}</span>
        )}
      </div>
      {props.action !== undefined && <div className="ml-auto">{props.action}</div>}
    </div>
  );
}
