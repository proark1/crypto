import { describe, expect, it } from "vitest";

import type { StatusResponse } from "../api/types";
import { deriveStatusAlerts } from "./statusAlerts";

const HEALTHY: StatusResponse = {
  mode: "paper",
  paused: false,
  protective_stop_quote: null,
  regime: { enabled: true, symbol: "BTC/USDT", label: "trending", reasons: [], reason: null },
  data_health: { healthy: true, reason: null },
  symbol: "BTC/USDT",
  symbols: ["BTC/USDT"],
  exchange_id: "binance",
  quote_currency: "USDT",
  quote_balance: "10000",
  realized_pnl_quote: "0",
  position: null,
  last_candle_close_time: "2026-01-02T00:01:00+00:00",
  mark_price_quote: "67000",
  equity_quote: "10000",
  breakers: { tripped_reason: null, cooldown_until: null, entries_today: 0 },
};

describe("deriveStatusAlerts", () => {
  it("returns nothing when all is well", () => {
    expect(deriveStatusAlerts(HEALTHY)).toEqual([]);
  });

  it("surfaces a tripped breaker as an error and suppresses the cooldown", () => {
    const alerts = deriveStatusAlerts({
      ...HEALTHY,
      breakers: {
        tripped_reason: "daily loss limit",
        cooldown_until: "2026-01-02T04:00:00+00:00",
        entries_today: 2,
      },
    });
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toMatchObject({ id: "breaker", tone: "error" });
    expect(alerts[0]?.body).toContain("daily loss limit");
  });

  it("shows the cooldown only when no breaker is tripped", () => {
    const alerts = deriveStatusAlerts({
      ...HEALTHY,
      breakers: {
        tripped_reason: null,
        cooldown_until: "2026-01-02T04:00:00+00:00",
        entries_today: 3,
      },
    });
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toMatchObject({ id: "cooldown", tone: "warn" });
  });

  it("reports degraded data and an ungated regime independently", () => {
    const alerts = deriveStatusAlerts({
      ...HEALTHY,
      data_health: { healthy: false, reason: "backfill failed" },
      regime: {
        enabled: false,
        symbol: null,
        label: null,
        reasons: [],
        reason: "no reference market",
      },
    });
    expect(alerts.map((alert) => alert.id)).toEqual(["data", "regime"]);
    expect(alerts[0]?.body).toContain("backfill failed");
    expect(alerts[1]?.body).toContain("no reference market");
  });
});
