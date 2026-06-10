# Live Trading Checklist

Live mode is refused by the worker today (`Worker.__init__` raises unless the
mode is paper) — **on purpose**. This document is the gate: every item below
is checked off before that guard is relaxed, and the guard is the last thing
removed, not the first. None of these are nice-to-haves; each one corresponds
to a way real money is lost that paper trading never exercises.

## Hard blockers (Phase 3 work)

### 1. Live execution adapter
- [ ] CCXT-based live adapter implementing the same `ExecutionAdapter`
      interface as the simulator — strategy and risk code must not change
      (CLAUDE.md invariant 3).
- [ ] **Decimal-safe boundary**: CCXT parses numbers to floats. Fill prices,
      quantities, and fees must be re-read from the exchange's raw string
      payload (`order["info"]`), never from CCXT's float fields, before they
      touch the portfolio.
- [ ] Lot-size, price-tick, and min-notional rounding using exchange market
      metadata, applied in the adapter with the rounded values journaled.
- [ ] Idempotent order submission via client order IDs, so a timeout + retry
      can never double-buy.

### 2. Exchange-native protective stops
- [ ] Every entry places a resting stop-loss order on the exchange
      (CLAUDE.md invariant 5) — the bot process dying must not mean the
      position is unprotected.
- [ ] Stop replacement (e.g. after scaling in) is cancel-then-place with the
      gap handled: the position is never left stopless, and a fill during the
      replacement window is detected by reconciliation.

### 3. Partial fills and order lifecycle
- [ ] Order state machine: submitted → partially filled → filled / canceled /
      rejected / expired, driven by exchange updates, surviving restarts.
- [ ] Fault-injection tests against the mock exchange: disconnect mid-order,
      partial fill then cancel, duplicate fill events, restart with an open
      order (CLAUDE.md testing requirements).

### 4. Reconciliation
- [ ] On startup and periodically: fetch exchange balances and open orders,
      compare to the journal, and **halt loudly on mismatch** — never trade
      on books that disagree with the exchange.
- [ ] Manual-intervention runbook for each mismatch class (orphan exchange
      order, missing fill, balance drift).

### 5. Fail-closed journaling
- [ ] In live mode, decision/fill journaling failures stop trading instead of
      logging and continuing (paper mode's best-effort journaling is not
      acceptable when the journal is the audit trail for real money).

### 6. Runaway brakes (risk manager completion)
- [ ] Circuit breakers: max daily loss, max drawdown from equity peak — trip
      to halted, require explicit human reset.
- [ ] Loss-streak cooldown and max-trades-per-day cap.
- [ ] All of the above live in the risk manager and therefore work
      identically in paper mode — ship them **before** the soak so the soak
      exercises them.

### 7. Operational safety
- [ ] Dead-man's switch: external monitor (e.g. healthchecks.io) that alerts
      when the worker stops pinging — a dead bot with open positions is an
      emergency, not a log line.
- [ ] Nightly Postgres backups, restore tested once.
- [ ] Exchange API key is **spot-trade-only**: withdrawals disabled, IP
      allowlist if the platform offers stable egress IPs.
- [ ] Kill switch drill performed against live (tiny position): halt, flatten,
      verify flat on the exchange UI.

### 8. Control-plane hardening
- [ ] Dashboard logout (clear stored token) and a documented token-rotation
      procedure.
- [ ] CORS restricted from `*` to the dashboard's exact origin
      (`TRADEBOT_API_CORS_ORIGINS`).
- [ ] Rate limiting / lockout on repeated bad tokens.

## The paper soak (Phase 2 exit criterion)

Run the bot in paper mode, unattended, for **at least 2 weeks** before any
live-mode work lands. The soak is evaluated, not just survived:

1. **Uptime**: no crash-loops; every restart (deploys included) replayed the
   journal and resumed with the correct position and balance.
2. **Data integrity**: no candle gaps (`/status` last-candle freshness stays
   under ~2 minutes); no quarantined-candle warnings beyond exchange blips.
3. **Signal fidelity**: signals produced live match a backtest run over the
   same period's persisted candles — same one-code-path guarantee, verified.
4. **Order plumbing**: every decision has a journal row; every fill matched a
   submitted order; pause/resume/kill and co-pilot approve/reject each
   exercised at least once during the soak.
5. **Honest PnL review**: paper PnL after pessimistic fills is documented —
   not as a profit promise, but to confirm sizing, stops, and fees behave as
   the backtest predicted.

Only when the soak report is clean **and** every hard blocker above is checked
does live trading begin — with a deliberately tiny balance first.
