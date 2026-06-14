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
 * Group the integer part of an exact amount string with thousands
 * separators for readability (1234567.89 → "1,234,567.89"). Pure string
 * work on the Decimal-exact value — no parsing, no float math — so it is
 * safe on money. Trailing precision is trimmed first via `truncateAmount`.
 */
export function formatMoney(amount: string, maxDecimals = 2): string {
  const trimmed = truncateAmount(amount, maxDecimals);
  const negative = trimmed.startsWith("-");
  const unsigned = negative ? trimmed.slice(1) : trimmed;
  const dot = unsigned.indexOf(".");
  const whole = dot === -1 ? unsigned : unsigned.slice(0, dot);
  const fraction = dot === -1 ? "" : unsigned.slice(dot);
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}${grouped}${fraction}`;
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
  // A sub-basis-point fraction rounds to "0.00" (or "-0.00"): show a plain,
  // unsigned zero rather than implying a direction on an apparent nothing.
  if (Number(percent) === 0) {
    return "0.00%";
  }
  return parsed > 0 ? `+${percent}%` : `${percent}%`;
}

/**
 * Compare two exact Decimal-string amounts, returning -1, 0, or 1 (a<b, a=b,
 * a>b). Money is never coerced to a JS number for ordering — two balances
 * differing only beyond float precision would otherwise rank wrong. Pure
 * string work on the Decimal-exact values.
 */
export function compareDecimalStrings(a: string, b: string): number {
  const left = a.trim();
  const right = b.trim();
  const signA = left.startsWith("-") ? -1 : 1;
  const signB = right.startsWith("-") ? -1 : 1;
  if (signA !== signB) {
    return signA < signB ? -1 : 1;
  }
  const magnitude = compareUnsignedDecimal(
    signA === -1 ? left.slice(1) : left,
    signB === -1 ? right.slice(1) : right,
  );
  return signA === -1 ? -magnitude : magnitude;
}

function compareUnsignedDecimal(a: string, b: string): number {
  const dotA = a.indexOf(".");
  const dotB = b.indexOf(".");
  const intA = (dotA === -1 ? a : a.slice(0, dotA)).replace(/^0+(?=\d)/, "");
  const intB = (dotB === -1 ? b : b.slice(0, dotB)).replace(/^0+(?=\d)/, "");
  if (intA.length !== intB.length) {
    return intA.length < intB.length ? -1 : 1;
  }
  if (intA !== intB) {
    return intA < intB ? -1 : 1;
  }
  const fracA = dotA === -1 ? "" : a.slice(dotA + 1);
  const fracB = dotB === -1 ? "" : b.slice(dotB + 1);
  const width = Math.max(fracA.length, fracB.length);
  const paddedA = fracA.padEnd(width, "0");
  const paddedB = fracB.padEnd(width, "0");
  if (paddedA === paddedB) {
    return 0;
  }
  return paddedA < paddedB ? -1 : 1;
}

/**
 * Dense ranking of exact money-string amounts, highest first (1 = best):
 * tied values share a rank and the next distinct value takes the next rank.
 * Nulls (e.g. a run still in flight) rank nowhere. Ordering is Decimal-exact
 * — never via float coercion.
 */
export function rankMoneyDescending(values: (string | null)[]): (number | null)[] {
  const present = values.filter(
    (value): value is string => value !== null && value.trim() !== "",
  );
  const sorted = [...present].sort((a, b) => compareDecimalStrings(b, a));
  const unique: string[] = [];
  let previous: string | null = null;
  for (const value of sorted) {
    if (previous === null || compareDecimalStrings(previous, value) !== 0) {
      unique.push(value);
      previous = value;
    }
  }
  return values.map((value) =>
    value === null || value.trim() === ""
      ? null
      : unique.findIndex((entry) => compareDecimalStrings(entry, value) === 0) + 1,
  );
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
