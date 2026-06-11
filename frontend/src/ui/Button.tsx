/**
 * The shared button. Five variants cover every button in the app today
 * (solid emerald primary, subtle zinc secondary, solid-red and outline-red
 * for destructive actions, and a borderless ghost), so individual call sites
 * stop re-deriving Tailwind classes and the styles stay in lock-step. All
 * variants disable to 50% opacity, matching the existing convention that a
 * command in flight greys its trigger so a nervous double-click cannot
 * double-submit.
 */
import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant = "primary" | "secondary" | "danger" | "dangerOutline" | "ghost";
export type ButtonSize = "sm" | "md";

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-emerald-600 text-white hover:bg-emerald-500",
  secondary:
    "bg-zinc-100 text-zinc-800 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-200 dark:hover:bg-zinc-700",
  danger: "bg-red-600 text-white hover:bg-red-500",
  dangerOutline:
    "border border-red-300 text-red-600 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950/40",
  ghost: "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "px-3 py-1.5 text-sm",
  md: "px-4 py-2 text-sm",
};

export function Button(
  props: {
    children: ReactNode;
    variant?: ButtonVariant;
    size?: ButtonSize;
    /** Leading icon; pairs meaning with shape, not colour alone. */
    icon?: ReactNode;
  } & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className">,
) {
  const { children, variant, size, icon, type, ...rest } = props;
  return (
    <button
      // Default to "button": an un-typed button inside a form submits it,
      // which has fired stray actions here before.
      type={type ?? "button"}
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg font-semibold disabled:opacity-50 ${VARIANTS[variant ?? "primary"]} ${SIZES[size ?? "md"]}`}
      {...rest}
    >
      {icon}
      {children}
    </button>
  );
}
