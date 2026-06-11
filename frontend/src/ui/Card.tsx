/**
 * The one card surface for the whole app. Every section used to hand-roll
 * the same `rounded-xl border ... bg-white ... shadow-sm` chrome, which is
 * why nothing read as more or less important than anything else; centralising
 * it here lets a later pass tune the surface (or introduce elevation levels)
 * in one place.
 */
import type { ElementType, ReactNode } from "react";

type Padding = "none" | "sm" | "md" | "lg";

const PADDING: Record<Padding, string> = {
  none: "",
  sm: "p-3",
  md: "p-4",
  lg: "p-5",
};

export function Card(props: {
  children: ReactNode;
  /** Defaults to `section` — the semantic wrapper most callers want. */
  as?: ElementType;
  padding?: Padding;
  className?: string;
}) {
  const Tag = props.as ?? "section";
  const padding = PADDING[props.padding ?? "md"];
  return (
    <Tag
      className={`rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900 ${padding} ${props.className ?? ""}`}
    >
      {props.children}
    </Tag>
  );
}
