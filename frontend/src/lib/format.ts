/**
 * Display-only formatting. Amounts arrive as Decimal-exact strings; we
 * trim noise for the eye but never compute with them.
 */

export function trimAmount(amount: string): string {
  if (!amount.includes(".")) {
    return amount;
  }
  const trimmed = amount.replace(/0+$/, "").replace(/\.$/, "");
  return trimmed === "" || trimmed === "-" || trimmed === "-0" ? "0" : trimmed;
}

/**
 * Truncate an exact Decimal string for headline metrics, where the full
 * 24-place precision overflows its card. Keeps at least `maxDecimals`
 * places, extending to the first significant digit so a small-but-real
 * PnL never displays as zero. Pure string slicing — never float math.
 */
export function truncateAmount(amount: string, maxDecimals = 2): string {
  const dot = amount.indexOf(".");
  if (dot === -1) {
    return amount;
  }
  const integerIsZero = /^-?0*$/.test(amount.slice(0, dot));
  const firstSignificant = amount.slice(dot + 1).search(/[1-9]/);
  // A sub-cent value with no integer digits keeps its first significant
  // decimal: a real-but-tiny PnL must never display as a flat zero.
  const keep =
    integerIsZero && firstSignificant !== -1
      ? Math.max(maxDecimals, firstSignificant + 1)
      : maxDecimals;
  return trimAmount(amount.slice(0, dot + 1 + keep));
}

/**
 * Render a backend fraction (e.g. "0.0123") as a signed percentage
 * ("+1.23%"). Fractions are ratios, not money, so parsing them for
 * display is allowed — money strings never come through here.
 */
export function formatFractionPercent(fraction: string | null): string {
  if (fraction === null) {
    return "—";
  }
  const parsed = Number(fraction);
  if (Number.isNaN(parsed)) {
    return "—";
  }
  const percent = (parsed * 100).toFixed(2);
  return parsed > 0 ? `+${percent}%` : `${percent}%`;
}

export function signClass(amount: string | null): string {
  if (amount === null) {
    return "text-zinc-600 dark:text-zinc-400";
  }
  if (amount.startsWith("-")) {
    return "text-red-600 dark:text-red-400";
  }
  return trimAmount(amount) === "0"
    ? "text-zinc-600 dark:text-zinc-400"
    : "text-emerald-600 dark:text-emerald-400";
}

/** Indicator acronyms that should stay uppercase when humanizing keys. */
const PARAM_ACRONYMS = new Set(["ema", "sma", "rsi", "atr", "adx", "macd", "bb", "vwap"]);

/**
 * Turn a snake_case parameter name into readable words for non-technical
 * users, keeping indicator acronyms loud: fast_ema_period → "fast EMA period".
 */
export function humanizeParamName(name: string): string {
  return name
    .split("_")
    .map((word) => (PARAM_ACRONYMS.has(word.toLowerCase()) ? word.toUpperCase() : word))
    .join(" ");
}

export function formatTime(iso: string | null): string {
  if (iso === null) {
    return "—";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return date.toLocaleString(undefined, {
    dateStyle: "short",
    timeStyle: "medium",
  });
}
