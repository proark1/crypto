/**
 * One source of truth for "what is wrong right now" with the bot, derived
 * from a StatusResponse. The status card used to inline four near-identical
 * warning boxes, and there was no way to surface the same conditions
 * elsewhere; deriving them here lets the header pill, the dashboard, and the
 * status card all read the same set without restating the rules.
 *
 * Display logic only — it reads status fields and formats a timestamp, never
 * computes on money.
 */
import type { StatusResponse } from "../api/types";
import { formatTime } from "./format";
import type { AlertTone } from "../ui";

export interface StatusAlert {
  /** Stable key for React lists and de-duping. */
  id: string;
  tone: AlertTone;
  title: string;
  body: string;
}

/**
 * The conditions that block or pause trading, in priority order. Mirrors the
 * status card's previous precedence: a tripped breaker supersedes the
 * loss-streak cooldown, while degraded data and an ungated regime are
 * independent of both.
 */
export function deriveStatusAlerts(status: StatusResponse): StatusAlert[] {
  const alerts: StatusAlert[] = [];
  if (status.breakers.tripped_reason !== null) {
    alerts.push({
      id: "breaker",
      tone: "error",
      title: "circuit breaker tripped",
      body: status.breakers.tripped_reason,
    });
  } else if (status.breakers.cooldown_until !== null) {
    alerts.push({
      id: "cooldown",
      tone: "warn",
      title: "loss-streak cooldown",
      body: `entries blocked until ${formatTime(status.breakers.cooldown_until)}`,
    });
  }
  if (!status.data_health.healthy) {
    alerts.push({
      id: "data",
      tone: "error",
      title: "market data degraded",
      body:
        "new entries paused until the feed recovers" +
        (status.data_health.reason !== null ? ` (${status.data_health.reason})` : ""),
    });
  }
  if (status.regime.reason !== null) {
    alerts.push({
      id: "regime",
      tone: "warn",
      title: "regime gate off",
      body: status.regime.reason,
    });
  }
  return alerts;
}
