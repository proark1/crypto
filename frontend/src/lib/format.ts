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

export function signClass(amount: string | null): string {
  if (amount === null) {
    return "text-zinc-400";
  }
  if (amount.startsWith("-")) {
    return "text-red-400";
  }
  return trimAmount(amount) === "0" ? "text-zinc-400" : "text-emerald-400";
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
