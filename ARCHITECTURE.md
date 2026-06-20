# Crypto Spot Trading Bot — Architecture Plan

A spot-trading bot that uses technical analysis and market data to decide when to buy
and sell. The user adds a coin (trading pair) and the bot starts trading it
autonomously within configured risk limits.

This document is the target design. Implementation status as of June 2026
(update this table in the same PR as the code it describes):

| Component (section) | Status |
|---|---|
| Core domain models, event bus, config (§4, §11) | **Done** — Decimal money, UTC timestamps, fail-safe defaults |
| Market data: live CCXT feed, closed-candle tracking, backfill, validation (§4.1) | **Done** — per-coin 1m feeds; four-year default first-boot backfill (a full halving cycle, covering the §12 research window across bull, bear, and chop; shallower stores are deepened on boot) primes the regime gate before trading starts while /health serves from the first second; in-process timeframe aggregation plus SQL calendar buckets (hour/day/week/month) for charting |
| Indicators: incremental EMA, RSI, ATR, ADX, Bollinger (§4.2) | **Done** — tested against TA-Lib reference values |
| Strategies: trend-following EMA crossover (§4.2) | **Done** — EMA cross with ATR stops plus sweepable knobs: anti-chase entry-extension filter, breakeven lock, ATR trailing stop; the pluggable registry is the pattern, more families pending |
| Backtester: runner, pessimistic fill simulator, golden test (§5) | **Done** — walk-forward splitting feeds the parameter sweeps (§12.5); account-level multi-symbol runner (one strategy per symbol, one shared book/risk manager, deterministic candle interleave) exercises exposure ceilings and balance contention no single-symbol backtest can show, with an account report (return, drawdown, turnover, exposure utilization, per-coin attribution) composable per walk-forward window |
| Risk manager: sizing, per-trade limits, circuit breakers (§4.3) | **Done** — daily-loss + drawdown trips (human reset), loss-streak cooldown, daily entry cap, account-wide exposure cap (open positions treated as one fully correlated block; per-coin caps alone understate crypto risk); breaker and pause/kill state persist to Postgres (saved within a candle of changing, restored before trading resumes), so a deploy cannot release a tripped breaker, reset the daily-loss anchor, or resume a killed bot |
| Execution: simulated adapter (backtest + paper) (§4.4) | **Done for paper** — protective stop lifecycle enforced in all modes: a resting stop-limit armed on entry fill from the order's persisted exit plan, its level managed by one ManagedStop shared with the evaluator (breakeven lock + ATR trail as sweepable knobs, resting order cancel/replaced as it ratchets), cancelled before any exit (single-exit guard), boot reconciliation re-arms after crashes (exact from the journaled plan, ATR approximation + market-exit backstop for plan-less history); venue filters (lot step, min quantity, min notional, price tick from the ccxt catalog) enforced at order construction, so paper orders are exchange-plausible; opt-in execution fidelity in the simulator (volume-capped partial fills with remainder-aware restart restoration and cumulative stop re-arming, volume-impact slippage, submit latency) — defaults off so the golden fixture and current paper behavior are unchanged until calibrated against real fills; live adapter, exchange-native stop placement, partial-fill handling are Phase 3 (see LIVE_TRADING_CHECKLIST.md) |
| Portfolio + persistence: positions, PnL, Postgres journal (§4.5) | **Done** — journal-replay restart recovery (fills rebuild positions; the order journal re-arms submitted-but-unfilled orders, stop-trigger latches and exit plans included, and boot replays the downtime candles through restored orders so an outage cannot defer a fill to the wrong price); scheduled gzipped-JSONL backups to S3-compatible storage (R2/S3/B2 via hand-rolled SigV4), streamed table-by-table so no table is ever fully resident (the compressed archive is buffered for one PUT), and restorable through SQLAlchemy |
| Trading engine + worker (§4.2, §7.1) | **Done** — per-candle strategy → risk → execution loop across every traded coin (see multi-coin below), paper mode only by hard guard; a crashed control plane trips a flatten-safe pause, and shutdown force-stops feeds so a deploy never hangs |
| Control API: status, pause/resume, kill, data endpoints (§6.4) | **Done** — bearer auth, public /health, CORS; SSE/WS push missing (dashboard polls) |
| Co-pilot mode: proposal queue, approve/reject, TTL + drift guards (§6.3) | **Done** — entries only; exits never wait for approval |
| Telegram notifications (§6.2) | **Done** — alerts only; command handling missing |
| Dashboard: status, chart, decisions, proposals, controls (§6.1) | **Done** — per-coin view with coin switcher, trade journal, decision/proposal panels, the bot-builder wizard, and the research screens (the bake-off tournament is the landing tab, then compare/tune/progress, with inspect — the former evaluate tab — as the single-run drill-in that compare and progress hand off to); SSE/WS push still missing (the dashboard polls) |
| Multi-coin support (§4.2) | **Done** — per-symbol feed+engine, shared account/breakers, per-coin dashboard, runtime add/remove via API + UI (coins persisted in Postgres; env var seeds first boot) |
| Automated improvement: sweep-validated self-tuning (§12.7) | **Done** — paper-scoped; self-feeding cycle rotating per improvement target (production's two families as one budget, then each research family's solo account, target-first then symbols; per-target evaluations, findings, staleness, and family-specific grids — fake-breakout width and volume-confirmation filters, MACD zero-line and volume-confirmation toggles, squeeze keltner-width tightness); findings target the next sweep's challengers with recorded lineage; accepted findings outrank proposed ones, and accepting arms a coalesced accept-triggered sweep (family-matched to the run it came from) so a verdict visibly becomes a test; knob map covers downtrend/ranging, wrong-hold, chasing, early-exit, missed-opportunity, and fake-breakout patterns; promotes only statistically validated sweep winners that also survive the engine-backed confirmation gate (challenger vs incumbent replayed through the production engine — sizing, fees, stop lifecycle, breakers — before any promotion; a vetoed winner is alerted, never applied); research-family promotions tune their competition accounts and say so in the journal (routing stays §13.7); versioned settings journal with UI revert; Telegram alert per promotion; custom-bot recipes are sweepable through the human-initiated paths (the run-sweep button and accept-triggered sweeps grade variants of the whole recipe), but excluded from the auto-rotation and auto-promotion until owner opt-in |
| Evaluation & training: blind walk-forward (§12) | **Done** — foundations, scenario engine (leak-tested), run orchestration + API, research screen, scenario replay viewer, learning findings (mined + human accept/reject), walk-forward parameter sweeps with explicit overfit verdicts, cross-family candidates, multiple validation windows, bootstrap confidence intervals + Bonferroni-corrected significance on every verdict; evaluation runs grade a chosen bot — any lineup entry or custom bot via the research bot selector, defaulting to the production shape (regime-routed families, self-classified per scenario); findings carry recurrence across a bot's runs and the research timeline serves the run/sweep/promotion story (§12.8) |
| News pipeline, regime gates, signal fusion (§5.2, §5.3) | **Partial** — BTC regime gate done (ADX trend/range + drawdown risk-off; blocks every family only on risk-off/warm-up/stale data, while family routing is the router's preference rather than a gate veto so a healthy regime lets either family enter; exits never gated; verdicts journaled as `gated` decisions); sentiment tighteners done (Fear & Greed extremes, BTC dominance surges, broad negative news flow — advisory, one-way, stale data contributes nothing); news pipeline done defensively (CryptoPanic polling + keyword classifier, negative-news coin flags, env-configured event windows); confirmation filters partial (volume confirmation shipped as a sweepable per-family entry knob on breakout/momentum, §5.2.3; perp-funding tightener shipped opt-in (§5.2; crowded-long funding pauses entries, live-only, off the backtest path), order-flow P2 data still missing) and automated calendar ingestion missing |
| Breakout strategy family (§5.2, review item 9) | **Research + competition** — Donchian-channel entries (close clears the prior N-candle ceiling), turtle-style channel exits, shared ATR stop convention and managed-stop knobs, optional volume-confirmation entry filter (§5.2.3, off by default); registered for sweeps/evaluation and auto-tuned by the §12.7 rotation (promotions change its solo competition account), but deliberately unrouted in production: which regime activates it (and at whose expense) is the §13.7 human decision the accumulating evidence exists to inform |
| Mean-reversion strategy family (§5.2 routing) | **Done** — RSI oversold-recovery entries, midline exits, same ATR stop convention as trend; optional trend-filter EMA (skip falling knives) as a sweepable knob; regime-routed per coin, both families' indicators always warm, exits pass from either family in any regime |
| Squeeze strategy family (§13) | **Research + competition** — volatility-squeeze breakout: enters the *upward release* of a Bollinger-band-inside-Keltner-channel compression (TTM-style coil), exits when the close falls back below the Bollinger basis, shared ATR stop convention and managed-stop knobs, optional volume-confirmation entry filter (§5.2.3, off by default); built from the new TA-Lib-verified incremental Bollinger plus the existing EMA/ATR; sweepable, evaluated, auto-tuned by the §12.7 rotation (its keltner-width knob is the squeeze tightness the grid tunes), and traded solo by its competition account; unrouted in production until the §13.7 evidence gate is met and a human routes it |
| Momentum strategy family (§13) | **Research + competition** — MACD histogram-crossover entries (12/26/9 defaults, zero-line filter on by default), histogram-flip exits, shared ATR stop convention, optional volume-confirmation entry filter (§5.2.3, off by default); built from the TA-Lib-verified incremental EMA; sweepable, evaluated, and auto-tuned by the §12.7 rotation like every family (promotions change its solo competition account), traded solo by that account, unrouted in production until the §13.7 evidence gate is met and a human routes it |
| Funding strategy family (§13) | **Research + competition** — the first non-price family: longs when the perpetual funding rate is deeply negative (over-crowded shorts, squeeze risk up), exits on recovery, shared ATR stop convention; reads the funding rate per candle from an injected funding series backed by the same store in backtest and live (§4.1), so it grades and trades on one code path; an absent series makes it inert, never an error; sweepable, evaluated, and auto-tuned by the §12.7 rotation (its grid steps the entry/exit thresholds and the stop), traded solo by its competition account, unrouted in production |
| Strategy competition: paper-bot lineup + custom bot builder, per-bot controls, leaderboard, research comparison (§13) | **Done** — production regime router plus six solo-family challengers (trend, mean-reversion, breakout, momentum, squeeze, funding) trade the same coins, candles, and gates from isolated journal-backed paper accounts (bot-scoped fills/orders/decisions/risk rows, per-bot signal-id namespacing, full restart replay per account); GET /competition serves the equity-ranked leaderboard; POST /evaluations/compare grades the whole lineup on byte-identical scenario sets (one frozen window + seed, grouped runs) for the research screen's side-by-side table; solo bots trade ungated by the regime router's family schedule (news/event vetoes still apply to all); per-bot pause/resume/kill + detail API; user-built custom bots (rule recipes, any/all entry voting via CompositeStrategy, validated + persisted + hot-editable + restart-replayed) |
| Strategy bake-off: one-click grid tournament (§13.8) | **Done** — ten energy presets (each family at calm/bold) plus the production baseline and a seeded random-entry control (the noise floor) graded across the full `{1h,4h,1d} × {10,50,100d}` grid, one cell per comparison on byte-identical scenarios, driven through the single research lane and polled to completion; cells with too little history reported `insufficient_data` and excluded; ranked by average return fraction with a live leaderboard updated per cell; persisted to `bake_off_jobs` (per-cell runs stay in `evaluation_runs`); POST /research/bakeoff + GET /research/bakeoff[s] with a research-tab UI |
| Observability: dead-man's switch, metrics, DB backups (§4.9, §7) | **Done** — structured JSON logging (one event per line, env-switchable to text for local tailing) with signal→order→fill correlation fields across the engine and risk manager, amounts kept exact as Decimal-derived strings; heartbeat ping gated on feed freshness; /metrics (feed lag, equity, breakers, bus counters) behind the bearer token; live-vs-backtest divergence measurable per coin (the §10 paper-gate metric: live paper fills matched against a same-candle replay of the production strategy shape; zero is the one-code-path expectation, non-zero is documented gating or a parity bug); scheduled gzipped-JSONL backups to S3-compatible storage with exact-Decimal restore (production restore drill pending, see checklist) |
| Live trading (§8 Phase 3) | **Missing** — blockers enumerated in LIVE_TRADING_CHECKLIST.md |

---

## 1. Guiding principles

1. **One code path for backtest, paper, and live trading.** The strategy code must not
   know whether it is running against historical data, a simulated account, or a real
   exchange. This is the single most important design decision: it makes backtest
   results meaningful and eliminates "works in backtest, breaks live" bugs.
2. **Risk management is a separate layer that can veto any trade.** Strategies propose,
   the risk manager disposes. No order reaches the exchange without passing risk checks.
3. **Event-driven core.** Market data events flow in; signals, orders, and fills flow
   through well-defined queues. This keeps components decoupled and testable.
4. **Survive first, profit second.** Capital preservation (stops, drawdown limits,
   circuit breakers, kill switch) is built in phase 1, not bolted on later.
5. **Everything is measured.** Every signal, order, fill, and PnL change is logged and
   queryable. You cannot improve a strategy you cannot measure.

## 2. Honest expectations

Pure technical-analysis bots on liquid spot markets compete against well-capitalized
quant firms. What a well-built bot realistically gives you:

- Disciplined, emotionless execution of a defined strategy, 24/7.
- Rigorous measurement so bad strategies are killed quickly and cheaply.
- A platform to iterate on edge (better signals, better regimes, better data) safely.

What it does not give you: guaranteed profit. The architecture below is optimized to
make iteration fast and losses bounded — that is what "best in the world" means in
practice for this class of bot.

## 3. High-level architecture

```
                        ┌──────────────────────────────┐
                        │        Control API / UI       │
                        │  (add/remove coin, configure, │
                        │   monitor, kill switch)       │
                        └──────────────┬───────────────┘
                                       │
┌───────────────┐   candles/ticks   ┌──▼───────────────┐   proposed    ┌──────────────┐
│  Market Data   │ ───────────────▶ │  Strategy Engine  │ ────────────▶ │ Risk Manager │
│  Service       │                  │  (per coin, plug- │   signals     │ (position    │
│  (WS + REST)   │                  │   gable strategies│               │  sizing,     │
└───────┬───────┘                   └──────────────────┘               │  limits,veto)│
        │                                                              └──────┬───────┘
        │ persists                                                            │ approved
┌───────▼───────┐                   ┌──────────────────┐   orders     ┌───────▼──────┐
│  Time-series   │                  │  Portfolio /      │ ◀─────────── │  Execution   │
│  Store         │                  │  State Manager    │    fills     │  Engine      │
│  (OHLCV, fills │ ◀──────────────  │  (positions, PnL, │ ───────────▶ │  (exchange   │
│   signals)     │                  │   balances)       │              │   adapter)   │
└───────────────┘                   └──────────────────┘              └──────────────┘
```

All components run inside one deployable service initially (a **modular monolith**),
communicating over in-process async queues. The module boundaries are strict enough
that any component can later be split into its own process if scale demands it.
Do not start with microservices — for a single-user bot they add latency and
operational pain with zero benefit.

## 4. Components

### 4.1 Market Data Service
- Subscribes to exchange WebSocket streams: trades, best bid/ask, and kline/candle
  streams for every coin the user has added.
- Builds and gap-fills OHLCV candles (1m base resolution, aggregated up to 5m/15m/1h/4h).
- REST backfill on startup and after disconnects; WS reconnect with exponential backoff.
- Publishes `CandleClosed`, `TickerUpdate` events to the strategy engine.
- Persists all candles to the time-series store (this becomes the backtest dataset).
- **Perpetual funding history** is backfilled and topped up into its own store
  (`FundingStore`), keyed by the *spot* symbol but fetched from the matching
  USDT perp (`BTC/USDT` → `BTC/USDT:USDT`) on the same unified CCXT client. This
  turns funding from a live-only tightener into a researchable series the
  funding strategy grades on — backtest and live read one store the same way
  (§3). Optional and fail-safe: a coin with no perp funding degrades to an empty
  series, so it never blocks spot trading; depth follows `history_backfill_days`.

### 4.2 Strategy Engine
- One strategy instance per (coin, strategy) pair, each consuming the event stream.
- **Trades the timeframe it is researched on.** The live feed publishes 1m
  `CandleClosed`; each engine rolls them up to the configured `trade_timeframe`
  (default 1h) in-process — the same `TimeframeAggregator` the regime detector
  and the backtest use — and the strategy decides only on those closed bars.
  Config locks `trade_timeframe` equal to the research timeframes
  (`auto_improve_timeframe`/`campaign_timeframe`), so a promotion graded on
  hourly bars is *applied* to an hourly trader, not a 1m one (a 50-period EMA
  must mean the same 50 hours live as in the sweep that validated it). Gap
  catch-up after a reconnect rolls up through the same aggregator, so a bar
  that closed during an outage meets resting orders exactly once.
- Strategies are pluggable classes implementing a small interface:
  `on_candle(candle, context) -> Signal | None` where context exposes indicator
  history, current position, and config.
- Indicator library computed incrementally (EMA, RSI, MACD, Bollinger Bands, ATR,
  ADX, VWAP, volume profile) — no recomputing full history every candle.
- Built-in strategy families (see §13 for the full competition lineup):
  - **Trend following**: EMA cross + ADX trend filter, ATR trailing stop.
  - **Mean reversion**: RSI oversold-recovery entries, midline exits, in ranging regimes.
  - **Breakout**: Donchian-channel entries with turtle-style channel exits.
  - **Momentum**: MACD histogram crossovers with a zero-line filter.
  - **Squeeze**: enters the upward release of a Bollinger-inside-Keltner volatility
    compression, exits at the Bollinger basis.
  - **Funding**: the first non-price family — longs when the perpetual funding rate is
    deeply negative (over-crowded shorts, squeeze risk up), exits when it recovers. Reads
    the rate per candle from an injected funding series (§4.1) backed by the same store in
    backtest and live; an absent series makes it inert, never an error.
  - **Regime filter**: classifies trend vs. range (ADX/volatility based) and routes to
    the appropriate strategy — this matters more than any single indicator.
- Output is a `Signal` (side, confidence, suggested stop/target), never an order.

### 4.3 Risk Manager
- Converts signals into sized orders or rejects them. Checks, in order:
  - Per-trade risk: risk at most X% (default 1%) of equity between entry and stop.
  - Position sizing via ATR-based stop distance.
  - Max concurrent positions and max exposure per coin / total.
  - Daily and weekly max-loss circuit breakers (halt trading, alert the user).
  - Cooldown after consecutive losses on a coin.
  - Liquidity check: order size must be a small fraction of recent volume.
- Owns the global **kill switch**: one command flattens all positions and halts.

### 4.4 Execution Engine
- Exchange adapter behind a common interface (`place_order`, `cancel`, `balances`,
  `open_orders`) with three implementations: **backtest fill simulator**, **paper
  trading** (real prices, simulated fills), **live exchange**.
- Live adapter built on **CCXT** for breadth, with native WebSocket order/fill streams
  for the primary exchange. Start with **one exchange** (Binance or Coinbase, decided
  by where the user's funds are) and add others later through the same interface.
- Handles the unglamorous reality: idempotent order submission (client order IDs),
  partial fills, retries with backoff, rate-limit budgeting, exchange minimums
  (min notional, lot size, price precision), and reconciliation of local state
  against exchange state on startup and on a timer.
- Default order type: limit orders at/near touch with a timeout-then-reprice loop,
  to control slippage and earn maker fees where possible. Fees and slippage are
  modeled identically in the backtest simulator.
- **Stops live on the exchange, not in the bot.** Protective stops are placed as
  exchange-native stop-limit orders wherever the exchange supports them, so positions
  stay protected even if the bot crashes, Railway restarts it, or connectivity drops.
  The bot manages (trails, replaces) these resting orders rather than watching prices
  and reacting — bot-side stop logic is the fallback only where native stops are
  unavailable.
  The lifecycle is mode-independent: entry orders carry a `ProtectiveExitPlan`
  (trigger = the signal's invalidation level the position was sized against, so
  enforced risk equals sized risk; limit floor a configured offset below the
  trigger). When the entry fills, the engine arms the planned stop-limit; a
  strategy exit or the kill switch cancels it before selling (never two SELLs
  working one position); startup reconciliation re-arms any stop the journal
  shows missing. In backtest and paper the same resting order lives in the fill
  simulator, so gap-through risk shows up in research results too.

### 4.5 Portfolio / State Manager
- Single source of truth for positions, balances, open orders, realized/unrealized PnL.
- **All accounting in one configured quote currency (default USDT):** v1 trades only
  pairs quoted in it, so equity, PnL, and risk limits are all directly comparable
  with no FX conversion ambiguity. Multi-quote support is a later, deliberate feature.
- Persisted in Postgres so the bot resumes cleanly after a restart. Two journals
  carry recovery: the append-only **fill journal** rebuilds positions and balances,
  and the **order journal** records every submitted intent with its latest state
  (open / filled / cancelled, plus the stop-trigger latch) so orders that were
  in flight when the process died are re-armed in the adapter instead of silently
  vanishing. Where the two disagree (crash between writes), the fill journal
  outranks the order row — an order with a journaled fill is never restored.
- Emits PnL and exposure metrics consumed by the risk manager and the UI.

### 4.6 Backtester & Research Loop
- Replays stored candles through the **exact same** strategy engine, risk manager, and
  execution interface, with a fill simulator that models fees, spread, and slippage.
- **Walk-forward validation** built in: optimize parameters on window N, validate on
  window N+1, roll forward. Never trust a single in-sample backtest.
- Reports: equity curve, max drawdown, Sharpe/Sortino, profit factor, win rate, trade
  distribution, exposure over time. A strategy must beat buy-and-hold of the same coin
  net of fees over the validation windows before it is eligible for live trading.
- Vectorized pre-screening (vectorbt or similar) is allowed for rapid idea filtering,
  but final validation always runs through the event-driven engine.

### 4.7 Control API & UI
- **FastAPI** service: add/remove coin, choose strategy and risk profile per coin,
  pause/resume, kill switch, and read endpoints for positions, PnL, trade history.
- "Add a coin" flow: validate the pair exists on the exchange → **screen it**
  (minimum 24h volume, minimum listing age, spread below threshold, not on any
  delisting/monitoring list — reject coins the bot cannot exit safely) → backfill
  history → warm up indicators → start in **paper mode by default** → user explicitly
  promotes to live after reviewing paper results.
- Notifications via Telegram (trade executed, stop hit, circuit breaker tripped,
  WS disconnected). The full interface design is in section 6.

### 4.8 Trade authorization & anti-accident safeguards

Defense in depth against unintended orders, plus a per-coin **autonomy mode**:

**Autonomy modes (set per coin, changeable any time):**
- **Autonomous:** the bot executes everything within its risk limits (default design).
- **Co-pilot (approval mode):** signals that pass every gate become **pending
  proposals** instead of orders. The user is notified (Telegram + dashboard) with the
  full context — side, size, entry, stop, target, and the reasons from each gate —
  and must explicitly approve or reject. Proposals **expire** (configurable TTL,
  e.g. 15 minutes) and are **auto-cancelled if price moves beyond a threshold** from
  the proposal price, so a stale approval can never execute at a bad price. All risk
  checks are **re-run at approval time**, not proposal time.
- **Safety invariant in both modes:** protective actions — stop-losses, circuit
  breakers, news-driven exits, the kill switch — always execute autonomously and
  immediately. A human approval queue in front of a stop-loss is itself a risk.
  Only entries and discretionary (take-profit/rebalance) exits go through approval.

**Anti-accident safeguards (always on, regardless of mode):**
- **Single chokepoint:** the execution engine accepts orders only from the risk
  manager's queue, each carrying an intent record (signal ID + gate decisions).
  There is deliberately no code path that can place an order without that lineage.
- **Pre-submit sanity checks at the execution engine:** price band (reject any order
  priced >X% away from current mid), per-order max notional cap, available-balance
  check, and exchange-filter validation (lot size, min notional) before submission.
- **Runaway-bot brakes:** global max orders per minute and max trades per day; the
  bot halts itself and alerts if order rejection rate or trade frequency is abnormal.
- **Duplicate suppression:** deterministic client order IDs make resubmits idempotent;
  reconciliation cancels unknown/orphaned orders found on the exchange.
- **Risk-limit changes are guarded:** increasing any limit (per-trade risk, exposure
  caps, disabling a circuit breaker) requires typed confirmation in the UI and is
  recorded in the audit log; decreases apply instantly.
- **Full audit trail:** every proposal, approval, rejection, expiry, manual action,
  and config change is journaled with timestamp and source (UI, Telegram, system).

### 4.9 Observability
- Structured JSON logging with event correlation (signal → order → fill chain).
- Metrics: data-feed lag, order round-trip time, fill rate, rejection reasons,
  live-vs-backtest signal divergence (a key health metric).
- Heartbeat alert if the data feed or trading loop stalls.
- **Dead-man's switch:** the bot cannot report its own death, so it pings an external
  monitor (e.g. healthchecks.io, free tier) every minute; missed pings alert the user
  through a channel independent of the bot and of Railway. Exchange-native stops mean
  positions stay protected while the bot is down (section 4.4).

## 5. Data sources & signals

### 5.1 Source catalog

| Priority | Source | Provider | Cost | Used for |
|---|---|---|---|---|
| P0 | Exchange WebSocket (trades, klines, book ticker, depth) | Primary exchange | Free | Live TA signals, order-flow (CVD, book imbalance), execution |
| P0 | Exchange REST | Primary exchange | Free | Backfill, order management, balances |
| P0 | Historical klines | data.binance.vision dumps | Free | Backtesting dataset |
| P1 | CCXT | OSS | Free | Multi-exchange abstraction |
| P1 | Funding rates, open interest, long/short ratio | Exchange futures API; Coinglass (aggregated) | Free tier | Leverage/positioning regime filter for spot decisions |
| P1 | Fear & Greed index | alternative.me | Free | Market-wide sentiment regime |
| P1 | BTC dominance, total market cap, stablecoin supply | CoinGecko API | Free | BTC regime gate for altcoin entries |
| P2 | News flow with bullish/bearish votes | CryptoPanic API; exchange announcement feeds | Free tier | News veto & event awareness (see 5.3) |
| P2 | Economic calendar (FOMC, CPI), token unlock schedule | Public calendars; TokenUnlocks | Free | Scheduled-event risk-off windows |
| P2 | Liquidation data | Coinglass | Free tier | Cascade detection, capitulation entries |
| P3 | On-chain (exchange inflows/outflows, whale moves) | CryptoQuant / Glassnode / Whale Alert | Paid | Research; adopt only if it wins in walk-forward |
| P3 | Social sentiment | Santiment / LunarCrush | Paid | Research; same rule |

Rule: a new data source is only wired into live trading after it demonstrably improves
walk-forward results. Paid sources are not purchased until the backtester exists to
evaluate them. Everything beyond P0/P1 is research material first.

### 5.2 Signal fusion — how TA, market data, and news combine

A trade decision is a pipeline of gates, not a vote among equals:

0. **Data-health gate (per coin):** before anything reads a candle, the coin's
   feed must have confirmed gap-free history. A feed is *degraded* until its
   first REST backfill succeeds and after any backfill fails (an unrepaired
   outage gap), and the gate blocks new entries while it is — entering on a
   feed with a hole means the strategy's indicators were computed across
   missing candles and resting orders may have skipped the candles that
   actually happened offline. Exits are never gated, so a degraded feed pauses
   new risk without trapping an open position behind its still-working stop.
   The block is journaled as a `gated` decision like every other gate.
1. **Regime gate (market-wide):** BTC regime (trend/range/risk-off via ADX + Fear &
   Greed extremes + BTC dominance shifts) decides *whether* entries are allowed at
   all: it blocks every family only when the market is genuinely hostile or
   unreadable — risk-off (a deep drawdown from the recent peak), warm-up (no regime
   formed yet), or stale data. Which family a healthy regime *favours* (trend in
   trends, mean reversion in ranges) is the router's preference, not a gate veto:
   enforcing the family schedule as a veto starved the single-coin production
   account, where the one traded market is also the regime reference, so trend
   crossovers (firing in chop) and oversold bounces (firing in selloffs) were each
   blocked by the regime the other family wanted. The router still routes by
   preference; the gate no longer vetoes the non-preferred family in a healthy
   regime. (Solo competition challengers remain fully ungated by the regime gate.)
   The sentiment tighteners are family-aware where it matters: extreme *greed*,
   dominance surges, broad negative news, and crowded-long perpetual funding
   pause every family's entries, but extreme *fear* pauses only trend entries —
   mean-reversion exists to buy fear behind its protective stop, and a genuine
   crash is still caught by the drawdown-based risk-off state, which halts
   everything. Both Fear & Greed thresholds are operator-tunable
   (`TRADEBOT_SENTIMENT_EXTREME_FEAR_AT_OR_BELOW`,
   `TRADEBOT_SENTIMENT_EXTREME_GREED_AT_OR_ABOVE`). **Perp funding** is the
   newest such tightener (`signals/funding.py` feeding the shared sentiment
   state): persistently high positive funding on the matching perpetual is a
   crowded, over-leveraged long, so new entries pause. It is one-way and
   stale-safe like the others, **opt-in** (off by default — a newer, less-proven
   positioning signal that needs the venue to expose funding for the configured
   contract), and a **live signal only** — never fed to the deterministic
   scenario engine, so the golden backtest is unaffected. The worker polls the
   contract's funding off its market-data exchange and feeds the shared
   sentiment state, opt-in via `TRADEBOT_FUNDING_SIGNAL_ENABLED` and
   `TRADEBOT_FUNDING_REFERENCE_SYMBOL` (threshold
   `TRADEBOT_FUNDING_CROWDED_LONG_AT_OR_ABOVE`).
2. **Entry signal (TA, per coin):** the active strategy produces the candidate
   buy/sell signal with stop and target. TA is the trigger; everything else filters.
3. **Confirmation filters (market microstructure & positioning):** order-flow (CVD
   direction, book imbalance) and derivatives positioning (funding not at a
   crowded extreme against the trade) must not contradict the signal. Each filter
   can only *block or shrink* a trade, never create one — this keeps the system
   testable and prevents filter soup. Implemented so far: **volume confirmation**
   on the breakout and momentum families — a `min_volume_ratio` knob requiring the
   entry candle's volume to reach a multiple of the prior candles' volume EMA
   (off by default, fail-safe while the baseline forms, exits never filtered,
   sweepable and toggled by the §12.7 rotation's losing-entry/fake-breakout
   findings). Order-flow and funding filters await their P2 data sources.
4. **News/event veto (see 5.3):** active negative-news flag or scheduled high-impact
   event window blocks new entries and may tighten stops on open positions.
5. **Risk manager:** final sizing and portfolio-level checks as defined in 4.3.
6. **Authorization (mode-dependent, see 4.8):** in autonomous mode the order goes to
   execution; in co-pilot mode it becomes a pending proposal awaiting user approval.

Every gate decision is logged with the signal, so backtests can attribute PnL impact
to each filter individually and dead filters get removed.

### 5.3 News & event pipeline

Honest framing first: a retail bot cannot trade *on* news faster than HFT firms —
by the time a headline is parsed, the price has moved. News is therefore used
**defensively and for event awareness**, not as an alpha source:

- **Ingestion:** poll CryptoPanic (filtered to held/watched coins) and the primary
  exchange's announcement feed (listings/delistings) every 1–2 minutes.
- **Classification:** map items to event types — delisting, hack/exploit, regulatory
  action, listing, partnership/noise — via keyword rules first; an LLM classifier is
  a later upgrade only if the simple version proves too noisy.
- **Actions:** delisting/hack/regulatory on a held coin → flag for exit and block new
  entries; broad negative flow → raise the regime gate to risk-off; everything
  else → logged for research, no live action.
- **Scheduled events:** FOMC/CPI timestamps and token-unlock dates for held coins
  define "no new entries" windows (configurable, e.g. ±2h) because volatility around
  them breaks normal TA assumptions.

## 6. User experience (UI / UX)

The user hands real money to an autonomous system, so **trust is the product**. The
UX goal: at any moment the user understands what the bot holds, what it is doing, and
*why* — and can stop everything in one click. Most trading bots are black boxes with
a settings page; radical explainability is the UX differentiator here.

### 6.1 Interfaces

- **Web dashboard** (primary): React + Tailwind, **TradingView Lightweight Charts**
  (free, OSS, the standard look traders expect) for candles; live updates pushed over
  WebSocket from FastAPI. Responsive layout installable as a PWA so the phone
  experience is first-class. Dark mode as default (trading convention).
- **Telegram** (push + emergency remote control): tiered notifications, a few guarded
  commands — `/status`, `/pause <coin>`, `/killswitch` — and **trade proposals with
  inline Approve / Reject buttons** for coins in co-pilot mode, so approvals work
  from a phone in seconds.
- **REST API**: everything the dashboard can do, for power users and automation.

### 6.2 Core screens

1. **Overview:** total equity curve, today's/total net PnL, open positions, one card
   per coin (paper/live badge, **autonomy mode badge**, active regime, last action +
   reason), a **pending approvals inbox** (proposals with countdown to expiry), and a
   system health strip (data feed, exchange connection, last heartbeat,
   circuit-breaker state). Designed to answer "is everything okay?" in three seconds.
2. **Coin detail:** live candlestick chart with entries, exits, and current stop
   plotted on the chart; active indicator overlays; and the **decision pipeline view**
   — each gate from section 5.2 shown green/red with its live reason (e.g. "entry
   blocked: funding at crowded-long extreme"). This is where trust is built.
3. **Add-a-coin wizard:** search pair → screening report (volume, spread, age —
   pass/fail with explanations) → pick strategy + risk preset (conservative /
   balanced / aggressive, described in plain language: "risks ~0.5% of equity per
   trade") → pick autonomy mode (autonomous / co-pilot) → backfill progress →
   starts in paper mode. Recommended first-live path: **co-pilot mode**, then switch
   to autonomous once the user trusts the bot's proposals.
4. **Trade journal:** filterable table of every trade; each row expands to the full
   decision context (signal, gate decisions, fees, slippage vs. expectation).
5. **Research:** run backtests from the UI, equity curve vs. buy-and-hold, walk-forward
   window results, paper-vs-backtest divergence. The **promote-to-live** action lives
   here, enabled only when the section 10 gates pass.
6. **Settings & risk:** risk limits edited in plain language with previews ("a losing
   day can cost at most $X"), notification preferences, API key management.

### 6.3 UX principles

- **Explainability over everything:** the bot never does (or skips) something the UI
  cannot explain. Every action and every inaction has a visible reason.
- **Mode clarity:** paper vs. live is unmistakable — persistent colored banner per
  coin, and promoting to live shows the paper results and requires typed confirmation.
- **Kill switch always visible** on every screen: one click + confirm, wired directly
  to the execution engine so it works even if the strategy engine is wedged.
- **Critical-first notifications:** info (fills) / warning (stop hit, feed reconnect) /
  critical (circuit breaker, news exit, feed stalled). Info is mutable; critical never.
- **No vanity metrics:** the UI leads with net PnL after fees, max drawdown, and
  performance vs. buy-and-hold — not win-rate dopamine.

### 6.4 Control-plane security

- Single-user by design (multi-tenant is a non-goal). Dashboard and API sit behind
  authentication (token or password + session) and HTTPS via a reverse proxy — this
  UI can move money and is never exposed unauthenticated.
- Telegram commands accepted only from an allowlisted chat ID; destructive commands
  require an inline confirmation tap.

## 7. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12+, fully async (asyncio) | Best ecosystem for TA/backtesting; spot TA trading at 1m+ timescale doesn't need lower latency |
| Primary exchange | **Deferred to deployment config.** All market data and execution code is exchange-agnostic via CCXT; the venue is an env var chosen at Phase 3 by region/fees (Binance 0.1% if available; Kraken as regulated alternative; Coinbase accepting higher fees). Binance public dumps remain the backtest dataset regardless of trading venue. | Keeps the venue decision reversible; fee level still determines how much edge a strategy needs |
| Exchange connectivity | CCXT + native WS client for primary exchange | Breadth + low-latency streams; switching exchanges later is cheap behind the adapter |
| Indicators | pandas-ta / TA-Lib + incremental implementations | Standard, well-tested |
| Storage | **Railway Postgres** from day one (candles, trades, state); Parquet export for research datasets | Railway's filesystem is ephemeral, so SQLite is out; managed Postgres is zero-ops here and simplifies "one code path" |
| API | FastAPI + Pydantic | Async, typed, quick |
| Frontend | React + Tailwind + TradingView Lightweight Charts, WebSocket live updates, PWA | Familiar trader UI, first-class on mobile |
| Control-plane auth | Railway-provided HTTPS domains + session/token auth in the app | The UI can move money; Railway terminates TLS, the app still authenticates every request |
| Backups | Railway Postgres backups + scheduled `pg_dump` to external object storage (e.g. Cloudflare R2) | Trade history and state survive losing the Railway project |
| Backtest screening | vectorbt | Fast parameter sweeps |
| Config | YAML per coin/strategy, Pydantic-validated | Reproducible runs |
| Deployment | **Railway** — one project hosting backend, frontend, and Postgres | 24/7 always-on services, deploy-on-push from GitHub, managed DB, zero server admin |
| Secrets | Railway environment variables; exchange API keys with **trade-only** permissions (withdrawals disabled) | Limits blast radius |

### 7.1 Railway deployment topology

One Railway project, three services plus the database:

- **`bot`** (always-on worker): the modular monolith — market data service, strategy
  engine, risk manager, execution engine, and the FastAPI control API in one process.
  **Exactly 1 replica, never horizontally scaled** — two replicas would mean duplicate
  orders. Idempotent client order IDs are the safety net; single-replica is the rule.
- **`frontend`**: the React dashboard served as a static build, talking to `bot` over
  its Railway-provided API URL.
- **`Postgres`**: Railway's managed database; candles, trades, state, journal.
- **Cron service** (Phase 4): scheduled walk-forward re-optimization and nightly
  `pg_dump` to object storage.

Railway-specific notes:

- **Deploys restart the bot.** Fine by design — startup reconciliation (section 4.4)
  rebuilds state from the exchange and Postgres, and spot positions are unaffected by
  the bot being down for seconds. Still, live deploys should be deliberate:
  auto-deploy `main` to a **paper environment**, promote manually to the **live
  environment** — Railway environments map cleanly onto the paper/live split.
- **Region:** pick the Railway region with the lowest latency to the chosen exchange
  (EU/Asia for Binance, US for Coinbase). At 1m+ candle timescales this is a
  nice-to-have, not critical.
- **Static outbound IPs are not guaranteed on all Railway plans.** Exchange API key
  IP-allowlisting needs a stable egress IP — if the plan in use doesn't provide one,
  run trade-only/no-withdrawal keys without the IP restriction rather than building a
  proxy workaround on day one.

## 8. Roadmap

**Phase 1 — Foundation (the part most bots skip and regret):**
project skeleton, exchange adapter interface, market data service with persistence,
portfolio state, backtester with fee/slippage model, one trend-following strategy,
walk-forward report. Exit criteria: a backtest run is reproducible end-to-end.

**Phase 2 — Paper trading:**
live data feed, paper execution adapter, risk manager, Telegram alerts, control API
with the add-a-coin flow, **autonomy modes with the approval workflow** (exercised in
paper mode to build trust before any real money), and the **dashboard MVP** (overview
screen with approvals inbox, coin detail with decision pipeline view, kill switch —
reviewing paper results requires a UI). Exit criteria: bot paper-trades a coin
unattended for 2+ weeks with no crashes, no data gaps, and live signals matching
backtest signals.

**Phase 3 — Live trading, small:**
live execution adapter with reconciliation, circuit breakers and kill switch proven by
fault-injection tests, promote to the Railway live environment. Start with small
capital on one coin.
Exit criteria: 4+ weeks live with execution quality (slippage, fill rate) matching the
backtest model's assumptions.

**Phase 4 — Edge expansion & full UX:**
mean-reversion strategy + regime router, multi-coin portfolio limits, parameter
re-optimization pipeline (scheduled walk-forward), P2 data sources, full dashboard
(add-a-coin wizard, trade journal, research screen with in-UI backtests), optional ML
(e.g., regime classification) — only where it beats the simple baseline out of sample.

## 9. Key risks and mitigations

- **Overfitting** — the #1 killer. Mitigation: walk-forward only, few parameters,
  penalize complexity, require out-of-sample beat of buy-and-hold net of fees.
- **Fees + slippage eating the edge.** Mitigation: model both pessimistically in
  backtests; prefer maker orders; trade fewer, higher-conviction signals.
- **Exchange/operational failures** (WS drops, rate limits, partial fills, restarts).
  Mitigation: reconciliation on startup, idempotent orders, fault-injection tests.
- **Tail events** (flash crashes, delistings). Mitigation: hard stops, daily loss
  circuit breaker, max exposure caps, kill switch, only trade liquid pairs.
- **Bot or exchange downtime with open positions.** Mitigation: exchange-native stop
  orders keep protection active while the bot is down; on exchange outage the bot
  alerts immediately and blocks new entries until reconciliation completes.
- **Key compromise.** Mitigation: trade-only API keys, IP allowlists, no withdrawal
  permission, secrets never in the repo.

## 10. Profitability validation gates

Planning cannot prove profitability — only data can. A strategy/coin combination is
"validated" only when it has passed every gate below, in order, and it remains live
only while it keeps passing the last one:

1. **Backtest gate:** beats buy-and-hold of the same coin net of pessimistic fees and
   slippage across all walk-forward validation windows; max drawdown within the
   configured tolerance; results stable under small parameter perturbations.
2. **Paper gate:** 2+ weeks of paper trading where live signals match backtest signals
   (divergence metric below threshold) and simulated PnL is consistent with backtest
   expectations.
3. **Small-live gate:** 4+ weeks with minimal capital where realized slippage and fill
   rates match the backtest model's assumptions.
4. **Ongoing gate:** live performance is compared monthly against the rolling
   walk-forward expectation; a strategy whose live results fall outside its backtest
   confidence range is automatically demoted back to paper.

Operational record-keeping supports this: every trade is journaled (signal context,
gate decisions, fees, PnL) and exportable as CSV — needed for both strategy review
and tax reporting.

## 11. Engineering quality & reproducibility

What separates a bot that survives from one that quietly rots:

**Testing strategy:**
- **Indicator unit tests** against TA-Lib reference outputs — a subtly wrong EMA is
  worse than a crash because it loses money silently.
- **Property-based tests for risk math:** position sizing can never exceed configured
  limits for any input, stop distance is never zero, balances never go negative.
- **Golden backtest in CI:** a fixed dataset + fixed config must produce byte-identical
  trades on every commit. Any diff means behavior changed — intentionally or not.
- **Fault-injection suite:** WebSocket drop mid-candle, restart with open orders,
  partial fill then disconnect, exchange 5xx/rate-limit responses, malformed payloads.
  Run in CI against a mock exchange; this is what makes Phase 3's exit criteria real.
- **Fill-simulator calibration:** recorded live fills are periodically replayed against
  the simulator; if simulated fills drift from reality, backtests are lying.

**Data quality:**
- Candle validation on ingest: gaps, zero-volume anomalies, and outlier ticks are
  flagged and quarantined — the bot pauses a coin on bad data instead of trading on it.
- Exchange maintenance windows are treated as data gaps: no signals, no entries.

**Reproducibility:**
- Every live trade is tagged with the strategy version (git SHA) and a config hash;
  every backtest records its dataset version and parameters. Any historical result —
  live or simulated — can be traced to exactly the code and config that produced it.
- UTC everywhere; exchange server-time drift is checked on startup and periodically
  (signed requests fail on clock skew).

**Process:**
- CI (GitHub Actions) runs the full suite on every push; Railway auto-deploys `main`
  to the paper environment only when CI is green; promotion to live is manual.
- Before building each component, review how mature OSS bots (Freqtrade, Hummingbot,
  Jesse) solved the same problem — steal good ideas, avoid documented mistakes.

## 12. Evaluation & training (blind walk-forward)

The bot decides on history it can see and is graded against a future it could
not see. Scenarios are generated by the system, decided blind through the
production strategy + risk + fill-simulation code (one code path, section 1),
revealed, graded, and persisted. Reports tell us *where* the bot is right —
by regime, volatility, timeframe, and event — not just how often. Findings
recommend improvements; a human accepts or rejects them. **The evaluation
system never changes trading rules by itself.**

### 12.1 Pipeline

1. **Generate** — stratified scenario sampling over stored candles:
   regimes × volatility × events × timeframes × scenario class (deciding
   from flat vs. managing a holding). Timeframes above 1m are aggregated by
   the same `TimeframeAggregator` the live bot uses.
2. **Decide blind** — only candles `< decision_time` are materialized for
   the decision (enforced by query boundary, proven by leak tests).
3. **Reveal & grade** — the horizon after the decision is loaded; entries
   are simulated with the pessimistic fill simulator; verdicts and timing
   labels are assigned per the frozen definitions below.
4. **Persist** — runs, scenarios, results, findings (section 12.4). Old
   runs are never rescored or overwritten; every run snapshots its full
   strategy config and code version.

Runs are shaped either by hand or from **suggested evaluations**
(`evaluation/suggestions.py`, served at `GET /evaluations/suggestions`):
for every active coin the backend proposes exactly three ready-to-run
shapes, fitted to how deep that coin's stored 1m history actually
reaches — a full ~4-year cycle on 4h candles, the same full cycle on the
1h trading timeframe (~35k candles, the ladder's biggest sample), and
the latest quarter on 15m so the fine-grained read reflects current
conditions. Coins with shallower history get the window clamped to what
exists, so a suggestion is always runnable with one click from the
research screen.

### 12.2 Frozen scoring definitions

Changing any constant below invalidates comparability across runs; changes
require an explicit amendment to this section, never a casual edit.

**R-multiple**: PnL divided by initial risk (|entry − stop| × quantity).

**Entry verdicts** (final R at horizon end, stop honored throughout):
very bad ≤ −1R < bad ≤ −0.25R < neutral < +0.25R ≤ good < +1.5R ≤ excellent.

**Hold verdicts (flat scenarios)**: a reference trade is simulated at the
decision close with the strategy's own stop convention (2×ATR(14)); if it
reaches ≥ +1R within the horizon the pass is a *missed opportunity*,
otherwise a *correct hold*. Holding-class scenarios grade holds against the
position's stop: if the horizon stops the position out, holding was a
*wrong hold*; otherwise *correct hold*.

**Timing labels** (from maximum favorable/adverse excursion, MFE/MAE):
- *early entry*: finished ≥ 0R but MAE ≤ −0.5R — right idea, paid for impatience;
- *late entry*: finished < 0R and MFE ≤ +0.25R — the move was over at entry;
- *early exit*: price advanced ≥ +0.5R beyond the exit before horizon end;
- *late exit*: ≥ 0.5R of the horizon's MFE was given back by exit time;
- *on time* otherwise.

**Oracle benchmark**: the hindsight-best exit inside the horizon, reported
per scenario for analysis only — never simulated as an achievable result.

**Market-condition labels** (constants frozen in `evaluation/classifier.py`):
trend is UP/DOWN when |net return| > 1.5 × vol × √n, else RANGING (zero
volatility with non-flat drift is therefore a trend, not a range);
volatility is HIGH/LOW at 1.5× above/below the run's dataset-wide median
reference; a pump/dump is a single-candle return beyond 6 × the window's
median absolute return (falling back to the mean absolute return when the
median is zero, so spikes in mostly-flat windows are still labeled);
breakouts compare the tail against the close-range
of the leading ⅔ of the window (real holds beyond the range at the final
close, fake returns inside); post-crash recovery is a first-half dump
followed by a second-half climb exceeding 1 × its own vol × √n.

### 12.3 Report metrics

Headline: **expectancy (mean R)** and profit factor first, win rate second —
a 40%-right bot earning +2R per win beats an 80%-right bot losing slowly,
and the report says so in plain words. Also: median R, average win/loss,
false-buy rate, missed-opportunity rate, hold accuracy, and every metric
broken down by regime, volatility, event, timeframe, and symbol. Sharpe is
reported but flagged as indicative only (overlapping scenarios violate its
independence assumption).

A scenario is also tagged with one **named archetype** (`classifier.archetype`),
a frozen priority-ordered partition of the trend/volatility/event labels into
plain words a non-specialist reads: *bull*, *bear*, *chop* (rangebound + high
vol), *compression* (rangebound + low vol), *range*, *breakout*, *fakeout*,
*pump*, *crash*, *recovery*. Unlike the event labels (which co-occur), every
window gets exactly one archetype, so the `by_archetype` breakdown sums to the
whole — the axis the research heatmap pivots a bot lineup on to answer "which
bot wins in chop, and which dies there?". The priority order is frozen for the
same comparability reason as the §12.2 constants it is built from.

**Risk-adjusted and tail metrics** sit beside expectancy so a high mean is
not mistaken for a safe one: a **downside-deviation of R** (RMS of the losing
trades, target 0), a per-trade **Sortino** (`expectancy ÷ downside
deviation` — reward per unit of loss volatility), a **tail loss** (the
expected shortfall in R: the mean of the worst decile of trades, at least one
trade), and the **worst single trade**. These are deliberately
*distributional* — symmetric functions of the R multiset — because the money
result above is order-independent, so a *path* metric (max drawdown,
time-under-water) has no meaningful trade ordering to read here. Those path
metrics live on the equity-curve reports instead (`backtest/report.py` and
`backtest/account_report.py`, which carry max drawdown and **Calmar** —
return over max drawdown — over a real ordered curve).

Alongside the R-multiple metrics the report carries an **illustrative money
result** so a non-technical reader can read outcomes in money, not only in
R: a fixed stake (10,000 quote) replayed through the graded trades at a
fixed-fractional 1%-per-trade risk (the live risk default), compounding, to
a starting/ending balance, net PnL, and return fraction. Ending equity is
the order-independent product of `(1 + 0.01 * R)` over trades — it isolates
signal quality, not position-sizing, so the same stake makes the columns
of a comparison directly rankable by ending money.

### 12.4 Persistence

Five tables (see `persistence/database.py`): `evaluation_runs` (config
snapshot, code version, status/progress, summary), `scenarios` (coordinates
+ condition labels; candles are referenced, never copied),
`scenario_results` (decision, reasons, R, MFE/MAE, oracle, verdict, timing),
`learning_findings` (pattern, evidence ids, impact, suggestion, confidence,
human accept/reject status), `sweeps` (candidate grid + split snapshot,
training/validation scores, plain-words verdict, and the accepted-finding
ids that motivated the sweep — the §12.5 lineage link).

### 12.5 Training loop

Parameter sweeps run on a training period and are validated on later,
untouched periods (walk-forward). Candidates may come from any registered
strategy family, so a sweep can pit families against each other on
identical scenarios; the winner is selected once, on the span that
strictly precedes every validation slice (re-selecting per window would
train on earlier windows' validation data), and then tested on each of
the chronological validation windows so the report shows whether the edge
persisted or lived in one lucky stretch. A config that wins only on the
data it was tuned on is overfit, and the report says so explicitly.
Because a grid of N candidates gets N chances at a lucky winner, a
challenger is only called *validated* when its edge over the baseline
clears a one-sided bootstrap test at the Bonferroni-corrected
significance level **and** that edge *persists* across the walk-forward —
the challenger must beat the baseline in a strict majority of the
validation windows, not merely on the pooled average. An edge concentrated
in one lucky stretch (which lifts the pooled average while losing the other
windows) is rejected as overfit rather than promoted: pooled significance
alone cannot see that concentration. Every candidate's expectancy also
carries a 95% bootstrap confidence interval. Accepted findings link to the config
change they motivated, so every strategy version carries its lineage:
what changed, why, and whether validation confirmed it.

A human-initiated sweep also carries a **cost-sensitivity** read
(`evaluation/sensitivity.py`): the validated winner is re-graded on the
untouched validation slices at 1.5× and 2× the configured fees and
slippage, and the report says whether its expectancy stays positive when
the costs get worse — the §10 "net of pessimistic fees and slippage" gate,
made visible at the promotion moment. It is **non-gating** (a read, not a
veto: thin samples make it noisy) and **opt-in** — the auto-improver leaves
it off so its frequent sweeps stay cheap; the `run sweep` button turns it
on. A challenger that only profits at today's fee schedule is borrowing
from it, and this is where that shows.

### 12.7 Automated improvement (paper-scoped)

The loop closes the research cycle without a human in the middle: on a
schedule (`TRADEBOT_AUTO_IMPROVE_*`), the bot derives single-knob variants
of the parameters it is trading right now, runs them through the blind
walk-forward sweep, and promotes the winner **only when the verdict is
validated** — the Bonferroni-corrected, multi-window statistical bar.
Training wins, near-misses, and findings never promote anything. It runs in
one of two shapes that share every mechanism below — the candidate
derivation, the validated gate, the guardrails: the **single-sweep
auto-improver** (the default, one sweep per scheduled cycle, described next)
and the opt-in **iterated campaign** (`TRADEBOT_CAMPAIGN_*`, the foot of this
section), which chains those sweeps into a budgeted search.

Each cycle serves one improvement target on one symbol, rotating
target-first: `production` (the regime-routed shape, which tunes both of
its families as one budget), then each research family (`breakout`,
`momentum`) — every family is revisited before any symbol repeats, each
with its own evaluations, findings, staleness clock, and family-specific
challenger grid (the fake-breakout filter, the MACD zero-line toggle).
A research family's promotion is **tuning, not routing**: it changes
what the family's solo competition account trades — sharpening the §13
leaderboard evidence — and its journal note says so explicitly;
production routing remains the §13.7 human decision. Custom bots are
outside the rotation until their owner opts in (a recipe is the user's
own; silently mutating it would violate least surprise).

The cycle is self-feeding: when no fresh evaluation run exists, the
cycle starts one (with a sample size large enough that sweeps are never
starved below their minimum-trades bar); its completion mines findings
from the graded record in Postgres; and the next cycle's sweep adds
challengers *targeted at those findings* — a losing-downtrend pattern
toggles the mean-reversion trend filter, a chasing pattern toggles the
trend family's entry-extension filter, an early-exit pattern toggles the
ATR trail and tests a later reversion exit, a missed-opportunity pattern
loosens the oversold gate (clamped below its own exit midline) — with
the finding ids recorded as the sweep's motivation. Patterns the bot has
no knob for yet (the volatility/event/timeframe/symbol buckets) remain
human-facing suggestions.

Human verdicts curate the targeting: once any of a run's findings is
**accepted**, only accepted findings steer the targeted challengers —
every extra candidate tightens the Bonferroni bar, so the curated few
never share their significance budget with patterns still awaiting
judgement. With no verdicts yet, every non-rejected finding steers (the
historical behavior); rejected findings never target anything.

Accepting is also a trigger, not just a record
(`TRADEBOT_ACCEPT_SWEEP_*`): the first acceptance on a run arms a short
coalescing timer (default 10 minutes, never reset — bounded latency),
every further acceptance rides the same sweep, and when it fires the
accepted findings become one targeted sweep on the run's own symbol —
one Bonferroni budget for the whole curated set, started as soon as the
single-flight research lane frees up (with a loud give-up after hours of
contention; the scheduled cycle remains the backstop). The findings API
reports the chain — queued, sweeping #N, then the sweep's verdict — read
off the sweep's recorded motivation, and the finding cards render it, so
a verdict visibly becomes a test.

Manual sweeps share this derivation: a sweep started without explicit
candidates (the dashboard's "run sweep" button) challenges the actively
traded parameters with the same curated findings-targeted grid — never a
static default grid — so the button tests exactly the knobs the findings
on screen point at, with the targeting finding ids recorded as the
sweep's motivation.

Custom bots take the same human-initiated paths, recipe-aware. A custom
bot is a *recipe* (one or more families combined by entry mode), so its
sweeps grade variants of the whole recipe — the composite it actually
trades — varying one knob of one family at a time while the rest of the
recipe stays fixed, reusing each family's own knob grid and finding
mappings (`build_recipe_candidates`). Both the manual button on a custom
bot's evaluation and the accept-triggered sweep build that recipe grid;
the verdict is shown like any other. Auto-tuning — putting custom bots
in the §12.7 *rotation* and auto-promoting a validated recipe winner —
still waits on the owner's opt-in, because a recipe is the user's own
and silently rewriting it would violate least surprise.

Guardrails, in order of importance: promotions apply to the **paper** bot
only (the worker refuses to construct in any other mode, and going live
stays a human decision in every configuration); every promotion appends a
version to the `strategy_settings` journal carrying its sweep as lineage
(history is never rewritten); the dashboard lists every version with a
one-click revert — the human override that always stays available; and a
promotion hot-swaps engines onto pre-warmed strategy instances, never
touching positions, orders, stops, or risk state. Evaluations, sweeps,
and live engines all read the same active-parameters source, so research
always grades the configuration actually being traded.

The loop is visible, not just journaled: `GET /improvement` reports its
schedule and the last cycle's outcome in the loop's own plain words
("sweep #N kept the active configuration (verdict: overfit)",
"auto-promoted …"), including an in-progress marker while a sweep runs,
and the research screen's Tune tab renders it as a status card — so "is
the bot learning?" has an answer on screen instead of in the logs.

**The iterated campaign (`TRADEBOT_CAMPAIGN_*`, opt-in, default off).** The
single-sweep cycle above tests one neighbourhood of the active config per
scheduled turn; most turns clear nothing, so the bot inches. The *campaign*
closes the gap between that and "keep adapting until it is good": it runs
sweeps **back to back**, promoting every challenger that clears the same
validated gate and **climbing from it**, and a round that finds no validated
gain **refines** — a finer step around the same incumbent (coarse to fine,
`build_candidates_for(..., scale)`), so the budget buys new information
rather than re-running the identical sweep — until a **fixed budget**
(`campaign_max_rounds` or `campaign_max_hours`) is spent or the step
converges below `campaign_min_scale`. Promote-each-step means the paper bot
always trades the best proven config and the next round climbs from there.

Iterating a search over backtests is how you overfit, so the guard is
structural. Every round is graded strictly *before* a **reserved holdout**
(`SweepConfig.window_end`): the most-recent `campaign_holdout_days` are never
swept, so the search cannot turn the validation windows into a second
training set across rounds. At the end the untouched slice grades the
campaign's net move once — start config versus final — a **non-gating
honesty read** (the §12.5 cost-sensitivity stance: it informs, and arms the
human's revert, but never vetoes; every step was already walk-forward
validated, and the verdict is withheld unless both sides clear the
minimum-trades bar). The fixed budget bounds how many lucky draws the search
gets, on top of each round's Bonferroni bar. Each round also samples a
**different scenario draw** (the round's sweep seed is derived as
`base_seed * 1000 + round_index`), so a winner has to clear the bar on
independent draws rather than fit the idiosyncrasies of one fixed sample
re-graded every round; the seed is deterministic from the campaign's base
seed, so the whole run still reproduces bit for bit, and the holdout read
keeps its own frozen seed to stay comparable across campaigns.

A **driver** runs campaigns continuously across the same target rotation
(production, then each research family), one at a time — the loop is
sequential, so it never contends with itself for the single research lane,
and a round whose sweep loses that lane to a human-started sweep refines and
retries. When enabled the campaign **supersedes** the single-sweep
auto-improver (they share the one lane); when off — the default — nothing
changes. Same scope as everything else here: this paper-only worker
promotes through the journaled, revertible apply path, and the campaign's
per-round sweeps and promotions surface through the existing sweeps,
timeline, and journal while it runs. Each round records the field-level diff
a promotion applied (before → after), shown on the Tune-tab card. The driver
holds only the current campaign in memory, so every finished campaign is
appended to a durable history (`campaign_history`, the JSON-able snapshot the
live status also returns) and served newest-first at `GET /campaign/history`,
so past campaigns survive a restart and stay scrollable in the dashboard.

### 12.8 Learning memory & the research timeline

Progress must be visible to be trusted. Two read-side surfaces compose it
from the persisted record — nothing new is written, so they can never
disagree with the journals:

- **Finding recurrence.** A finding's pattern text is deterministic for a
  given mistake (frozen miners, §12.2), so the same pattern across a
  bot's completed runs *is* its lifecycle — no extra schema. The findings
  API annotates each finding with how many earlier completed runs of the
  same bot mined it and since when ("recurred · 4 runs since #44" versus
  "new pattern"), computed within a bounded window of recent runs.
- **The research timeline** (`GET /research/timeline`, the research
  screen's Progress tab): terminal evaluation runs (with expectancy and
  which patterns appeared or stopped firing versus the same bot's
  previous completed run), sweep verdicts with their motivating-finding
  lineage, and settings promotions — each carrying the field-level diff
  against the family's previous version, so a promotion shows *what* it
  changed (`atr_stop_multiple 2.5 → 1.5`), not just that it happened —
  merged newest-first into one
  plain-words feed whose headlines are composed server-side, so the
  feed, the logs, and Telegram tell the same sentence. In-flight work is
  deliberately absent (the §12.7 status card carries it); the timeline
  is the record. Failed and interrupted work appears rather than hides.

A pattern "no longer firing" in the run after a promotion is the honest
before/after read this system can offer — two runs sample different
history windows, so proving an improvement statistically remains the
sweep's job (§12.5); the timeline only reports what changed.

### 12.6 Delivery status

All six steps are implemented: foundations (this section, aggregation
batch helper, condition classifier, the tables), scenario engine + scoring,
run API, research screen, the scenario replay viewer (scenarios are rebuilt
from their stored coordinates through the run's own aggregation path and
revealed candle by candle, grade last), and the learning layer. The
learning layer has two halves. Findings: every completed run is mined for
mechanical, evidence-backed patterns (losing condition buckets, chronic
late entries / early exits, missed opportunities, wrong holds — thresholds
frozen in `evaluation/learning.py`), and each finding awaits an explicit
human accept/reject through the API; the verdict is recorded once and never
flipped. Sweeps (`evaluation/sweep.py`): candidate parameter sets — from
any registered strategy family — are scored by the same blind pipeline on
a chronological training slice; only the training winner and the baseline
are scored on the untouched validation windows, with bootstrap confidence
intervals and a Bonferroni-corrected superiority test
(`evaluation/statistics.py`), and the report's verdict is plain words —
*validated* (now meaning statistically validated), *overfit* ("wins only
on the data it was tuned on"), *baseline best*, or *insufficient
evidence* (including a real but statistically unproven edge). Evaluation
runs grade a named bot — any competition lineup entry (§13) or custom bot
(§13.5, graded by its recipe, captured at run start) — chosen per run
through the research screen's bot selector (`GET /evaluations/strategies`
lists what is gradeable; the request validates the id before any run row
exists). The default is the incumbent, i.e. the strategy shape production
trades: the regime-routed family router, with the regime self-classified
from each scenario's own candles (`evaluation/strategy.py` documents the
divergence from the live reference-market detector, which scenarios must
not read). Findings always only recommend. Sweep verdicts feed the
automated improvement loop (§12.7): in paper mode a *validated*
challenger is promoted automatically; anything touching live trading
remains a human action.

### 12.9 AI research advisor (advisory, human-approved)

The findings miner (§12.8) reports *mechanical* patterns; the **AI research
advisor** (`evaluation/advisor.py`) is an optional layer on top that reads a
completed run's report and its mined findings and asks a Claude model to
synthesize them into a short diagnosis plus a few **experiment hypotheses** —
"what looks broken, and what would you try next." It exists because the gap
between a wall of metrics and a concrete next experiment is exactly the work a
language model is good at, and the rest of §12 already produces the evidence to
ground it.

It is deliberately powerless, and the boundary is the whole point:

- **Advisory only — no order or promotion path.** The advisor returns a
  `ResearchAdvice` object (a diagnosis and a list of hypotheses, each with a
  plain-words `parameter_hint`) and nothing else. A hypothesis becomes a test
  only when a human chooses to arm a sweep from it, through the same
  human-initiated path the run-sweep button and accept-triggered sweeps already
  use (§12.7). Nothing the model writes is parsed into an applied configuration,
  and it never places an order (CLAUDE.md invariants 4).
- **Off the hot path, out of the deterministic core.** It is an on-demand call
  from the control API, never on the candle→signal→order loop, and it is never
  an input to the scenario engine or the fill simulator — so the golden backtest
  is byte-identical whether or not the advisor ran. It reads R-multiples and
  money *strings* off an already-built report to compose prose; it does no money
  arithmetic and produces no size.
- **Fail-safe and opt-in.** Off by default (`TRADEBOT_AI_ADVISOR_ENABLED`,
  defaulting false). The SDK is an optional dependency (the `ai` extra) and the
  credential is an environment variable (`ANTHROPIC_API_KEY`) — never in the
  repo. A disabled flag, a missing package, a missing key, a model refusal, a
  timeout, or any SDK error all resolve to "no advice" (`None`), never to an
  error that could fail the surrounding request. The call uses structured
  outputs (a typed schema) and adaptive thinking; the model id, token ceiling,
  and timeout are configured.

In short: the deterministic pipeline earns the evidence and keeps the veto; the
model only ever *suggests* the next experiment a human runs.

---

## 13. Strategy competition (seven bots, one winner)

One question drives this section: **which strategy is actually best?**
Backtests answer it on history; the competition answers it forward, with
the same paper money, at the same time, under the same rules.

### 13.1 The lineup

Seven competitors, fixed in code (`competition/lineup.py`) because the
roster is an architecture decision — a stable lineup is what makes the
leaderboard's history meaningful:

| Bot id | Strategy |
|---|---|
| `production` | The incumbent: regime-routed trend + mean-reversion router (the bot's real shape) |
| `trend_following` | EMA crossover with ATR stops, solo, always on |
| `mean_reversion` | RSI oversold-recovery entries, midline exits, solo |
| `breakout` | Donchian-channel entries, turtle-style channel exits, solo |
| `momentum` | MACD histogram crossovers (zero-line filtered), solo |
| `squeeze` | Volatility-squeeze breakout: enters the upward release of a Bollinger-inside-Keltner compression, exits at the basis, solo |
| `funding` | Funding contrarian: longs deeply negative perpetual funding (crowded shorts), exits on recovery, solo — reads the funding series (§4.1) |

Every challenger trades its family's **active** (possibly auto-promoted)
parameters, so the comparison is always against what the family trades
today, not a stale snapshot.

### 13.2 Fairness rules

The only variable is the strategy. Everything else is identical:

- **Same candles** — one market-data feed per symbol fans out to every
  account's engine over the bus; the competition multiplies accounts,
  never exchange connections or rate-limit spend.
- **Same market facts, own strategy** — hard news and event-window
  vetoes apply to every account identically (§5.3): those are market
  facts, not strategy. The regime gate does **not** apply to solo bots:
  it routes families (trend entries in trends, mean-reversion in
  ranges), and that routing IS the production router's strategy — gating
  a solo bot by it would mute the bot for every regime its family is
  "wrong" for, and the leaderboard would compare gate schedules instead
  of strategies. The production bot keeps its full gate chain: that is
  the shape being defended.
- **Same risk rules** — each account gets its own `RiskManager` over its
  own portfolio (same `RiskConfig`), so breakers judge each account's own
  equity; one bot's drawdown never mutes another's entries — or hides
  behind them.
- **Same starting balance** — every account seeds from
  `TRADEBOT_PAPER_INITIAL_BALANCE_QUOTE`.

### 13.3 Isolation and persistence

Each account is real bookkeeping, not a toy counter: fills, orders, and
decisions are journaled in the shared tables under a `bot_id` column
(rows predating the competition default to `production` — they always
belonged to the production bot), and each account persists its breaker
state in its own `risk_state` row. Restarts replay every account from
its own journal, restore its open orders, replay its downtime candles,
and re-arm its protective stops — the same recovery path as production,
account by account.

Signal ids are namespaced per bot (`<bot_id>/<signal_id>`, applied by a
strategy wrapper for entries/exits and by the engine for its synthesized
kill/stop exits), because order ids derive from signal ids: without the
prefix, two bots trading the same family on the same candle would mint
the same order id and collide in the shared journal. Production ids stay
unprefixed — its id streams predate the competition.

Challengers are always autonomous (co-pilot approvals are the operator's
bot's concern), never notify, and **never get routed into production by
winning** — promotion of a family into the production router remains a
human architecture decision informed by this evidence. Account-wide
operator commands stay whole-bot: pause/resume/kill act on every
account, and a coin cannot be removed while any account holds a
position or order in it. Each bot is additionally controllable on its
own — `POST /bots/{id}/pause|resume|kill` mutes, un-mutes, or halts and
flattens one account (protective stops keep running while paused), and
`GET /bots/{id}` serves the detail view (leaderboard row, marked
positions, and the exact effective parameters it trades).
`TRADEBOT_COMPETITION_ENABLED=false` falls back to the incumbent alone.

### 13.4 The leaderboard

`GET /competition` returns every account ranked by equity (marks: newest
stored 1m closes, gathered once so no competitor is priced at a
different moment): equity, return on initial balance, realized and
unrealized PnL, open positions, entry/exit fill counts, and breaker
state. The dashboard renders it as the strategy battle card. Unknown
marks make an honest `null` equity that ranks last — never a guessed
number.

### 13.5 Custom bots (build your own competitor)

Users can enter their own bots into the competition. A bot is a
**recipe**: one or more strategy families with parameter overrides,
combined as ``any`` (the first family to propose a buy wins — a wider
net) or ``all`` (every family must agree on the same candle — a
confluence filter, via ``strategies/composite.py``; exits always pass
from whichever family asks, so a position is never trapped behind a
vote). Recipes are validated against the real config models — a typo'd
family or parameter fails the create call, never trades defaults
silently — and persisted in ``custom_bots`` with a permanently reserved
``risk_state`` row (built-ins own rows 1-5; custom bots start at 100;
ids are never reused).

A created bot joins live immediately with primed indicators (stored
candles fed through the fresh strategy before its engines attach) and
is a full account: journal-scoped fills/orders/decisions, own risk
manager and breakers, complete restart replay. Recipes are editable
(``PUT /bots/{id}/rules`` hot-swaps the strategy, position and history
untouched — the same mechanics as a parameter promotion) and bots are
deletable once flat; their journals stay queryable forever, and a
recreated bot with the same name would collide loudly rather than merge
histories. Built-ins are not editable or deletable: their parameters
come from research promotions (§12.7), and the lineup's stability is
what makes the leaderboard's history meaningful. The builder UI reads
its choices from ``GET /bots/options`` — families, plain-words
descriptions, and complete defaults straight from the strategy
registry, so the frontend never hardcodes a parameter that could drift.

### 13.6 Research comparison (same question, graded scenarios)

The live leaderboard needs weeks to mean anything; the research
comparison answers the same question in minutes on history.
`POST /evaluations/compare` starts one §12 evaluation run per lineup
entry, sequentially (the single-flight rule protects the live candle
loop), all sharing one frozen window end, one seed, and therefore
**byte-identical scenario sets** — the runs differ only by strategy.
Members share a `comparison_group` (the lead run's id) so
`GET /evaluations/comparisons` can hand the research screen whole
batches for a side-by-side table (expectancy, win rate, profit factor,
per-regime breakdowns — every §12.3 metric, one column per strategy).
Because every strategy starts from the identical 10,000 stake (§12.3
money result), the table leads with each column's **ending balance and a
1st/2nd/3rd rank** by that balance — the plainest read of which strategy
did best — above the R-multiple detail. Cancelling any member cancels the
batch: half a comparison cannot answer the question the batch asked.

Below the table the research screen pivots the same batch into a **scenario
heatmap** — bots down the rows, the §12.3 market archetypes across the
columns, each cell that bot's expectancy in that archetype, washed
green/red and ringing the best bot per column. It is the same comparison
data read a different way, and it answers what a single ranking cannot:
*which bot wins in chop, and which dies there?* — the per-regime evidence
the §13.7 routing decision turns on. It recommends nothing; routing stays a
human call.

### 13.7 Routing a research family into production (the evidence gate)

Tuning a research family (§12.7) sharpens it; routing it — adding it to
the production router's regime schedule, at some incumbent's expense —
is an architecture decision this section reserves for a human, made
against explicit evidence rather than enthusiasm. A research family
(`breakout`, `momentum`) becomes a routing *candidate* only when all of
the following hold:

1. **Validated out-of-sample edge in a named regime.** Its sweeps and
   evaluation runs show statistically validated (§12.5 bar) positive
   expectancy concentrated in an identifiable regime bucket (the regime
   the router would activate it in), not a diffuse average.
2. **Beats the incumbent router on identical scenarios.** Research
   comparisons (§13.6, byte-identical scenario sets) rank it above the
   production router in that regime's scenarios across at least two
   separate comparison batches run weeks apart.
3. **Live paper evidence.** Its solo competition account shows a
   positive return over at least eight weeks of live paper trading
   without tripping its own circuit breakers. This soak is judged on the
   account's *overall* return — live PnL is not attributed per regime, so
   the regime concentration is carried by condition 1 and the
   regime-scoped head-to-head of condition 2; the soak confirms the family
   survives real conditions over a span long enough to cover them.

Meeting the gate flags the candidacy; it never flips the switch. The
human decision that remains is exactly the part evidence cannot answer:
which regime label activates the family, which incumbent (if any) yields
its slot, and whether the router's added complexity is worth the edge.
The decision is recorded by amending §5.2 and §13.1 in the same PR that
adds the route, with the evidence linked — the same lineage discipline
as every promotion (§12.5).

The three conditions are **auto-evaluated and surfaced** so the gate is a
read, not a manual audit across screens (`competition/candidacy.py`,
`GET /research/candidacy`, the research screen's Compare tab). For each
research family the panel grades each condition from the persisted record —
the validated edge and its best regime bucket from the family's sweeps and
the §12.3 `by_archetype` breakdowns; the head-to-head from the comparison
batches' `by_archetype` expectancy *in that regime* (family vs. the
incumbent, counting only wins ≥ two weeks apart); the live soak from its
competition account's overall return, breaker state, and first-fill age —
and reports whether all three hold. The evaluation is pure
over the fetched record (database-free, fully tested); the surface only
flags candidacy, exactly as this section requires — it never routes
anything.

### 13.8 The bake-off (one button, the whole grid)

The competition (§13.1–13.4) runs forward in real time; the research
comparison (§13.6) grades one frozen window. The **bake-off** answers the
broader question in one automated sweep: across many timeframes and many
history depths, *which kind of bot makes the most money?* It is the
research tab's one-click experiment — start it and walk away.

- **Contestants (`evaluation/presets.py`).** A fixed roster of ten *energy
  presets* — each of the five solo families at a `calm` temper (slower
  entries, a wider 3×ATR stop) and a `bold` one (faster entries, a tighter
  1.5×ATR stop) — plus the production router as a baseline; two **ensembles**
  that put the "the best bot may be a combination, not a soloist" thesis on
  the leaderboard (a *confluence* ensemble that enters only when its families
  agree on the same candle, and a *breadth* ensemble that enters when any of
  its families fires), built from the same composite a custom bot trades and
  graded research-only — winning the bake-off never routes a recipe into
  production (§13.7); and a **random-entry control** as the noise floor: a
  seeded coin-flip bot (`strategies/controls.py`) that buys and sells at
  random with the families' own ATR stop, fees, and slippage, so a family
  that cannot out-earn it has no edge distinguishable from luck. The control
  lives in its own registry, never `STRATEGY_FAMILIES` — it is a yardstick,
  never swept, lineup'd, or promoted. The roster is code-defined and frozen
  so two bake-offs are comparable; every contestant is validated buildable at
  import.
- **The grid.** Each *cell* is one (timeframe, history-window) pair. The
  default grid pairs each timeframe with depths sized to its own bar —
  `1h × {10, 50, 100d}`, `4h × {40, 90, 180d}`, `1d × {180, 270, 365d}`,
  nine cells — because feasibility is a candle count, not a day count: a
  scenario needs lookback + horizon candles (150 by default), and a day buys
  24 candles at 1h but only one at 1d, so a single day-window list shared
  across timeframes is either too thin to trade on 1d or buries 1h in years
  of history. Each cell is one ordinary comparison (§13.6): all contestants
  on byte-identical scenarios (one frozen window end, one seed), so within a
  cell the only variable is the strategy. Every per-cell number is a normal,
  inspectable evaluation run, linked by its `comparison_group`.
- **One research lane.** The orchestrator drives the cells through the
  evaluation manager one at a time and polls them to completion, exactly as
  the §12.7 improver polls its sweeps — never a second workload competing
  with the live candle loop. A bake-off waits its turn behind a manual run
  or the improver rather than jumping the queue.
- **Honest feasibility.** A short window on a high timeframe may hold too
  few candles to host a scenario (ten daily candles cannot). Such a cell is
  recorded `insufficient_data` and excluded from the averages — no bot is
  charged for a window nobody could trade. The default grid is sized so
  every cell clears that floor; only a hand-picked grid can land on one.
- **Ranking.** Each contestant is scored by its **average return fraction**
  across the cells it could trade (raw money is not comparable across a
  10-day and a 100-day window). The leaderboard updates after every cell,
  so a mid-flight job already shows a partial ranking.
- **Persistence (`bake_off_jobs`).** The job row snapshots the grid and
  roster and accumulates the per-cell records and ranking; the per-cell
  runs stay in `evaluation_runs`. Everything is in Postgres, so a finished
  bake-off is a permanent, queryable record to optimise against — and, like
  every research output here, it only ever *recommends*: routing a winner
  into production stays the §13.7 human decision.
