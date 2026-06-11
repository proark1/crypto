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
| Indicators: incremental EMA, RSI, ATR (§4.2) | **Done** — tested against reference values |
| Strategies: trend-following EMA crossover (§4.2) | **Done** — EMA cross with ATR stops plus sweepable knobs: anti-chase entry-extension filter, breakeven lock, ATR trailing stop; the pluggable registry is the pattern, more families pending |
| Backtester: runner, pessimistic fill simulator, golden test (§5) | **Done** — walk-forward splitting feeds the parameter sweeps (§12.5); account-level multi-symbol runner (one strategy per symbol, one shared book/risk manager, deterministic candle interleave) exercises exposure ceilings and balance contention no single-symbol backtest can show, with an account report (return, drawdown, turnover, exposure utilization, per-coin attribution) composable per walk-forward window |
| Risk manager: sizing, per-trade limits, circuit breakers (§4.3) | **Done** — daily-loss + drawdown trips (human reset), loss-streak cooldown, daily entry cap, account-wide exposure cap (open positions treated as one fully correlated block; per-coin caps alone understate crypto risk); breaker and pause/kill state persist to Postgres (saved within a candle of changing, restored before trading resumes), so a deploy cannot release a tripped breaker, reset the daily-loss anchor, or resume a killed bot |
| Execution: simulated adapter (backtest + paper) (§4.4) | **Done for paper** — protective stop lifecycle enforced in all modes: a resting stop-limit armed on entry fill from the order's persisted exit plan, its level managed by one ManagedStop shared with the evaluator (breakeven lock + ATR trail as sweepable knobs, resting order cancel/replaced as it ratchets), cancelled before any exit (single-exit guard), boot reconciliation re-arms after crashes (exact from the journaled plan, ATR approximation + market-exit backstop for plan-less history); venue filters (lot step, min quantity, min notional, price tick from the ccxt catalog) enforced at order construction, so paper orders are exchange-plausible; opt-in execution fidelity in the simulator (volume-capped partial fills with remainder-aware restart restoration and cumulative stop re-arming, volume-impact slippage, submit latency) — defaults off so the golden fixture and current paper behavior are unchanged until calibrated against real fills; live adapter, exchange-native stop placement, partial-fill handling are Phase 3 (see LIVE_TRADING_CHECKLIST.md) |
| Portfolio + persistence: positions, PnL, Postgres journal (§4.5) | **Done** — journal-replay restart recovery (fills rebuild positions; the order journal re-arms submitted-but-unfilled orders, stop-trigger latches and exit plans included, and boot replays the downtime candles through restored orders so an outage cannot defer a fill to the wrong price); nightly backups missing |
| Trading engine + worker (§4.2, §7.1) | **Done** — single symbol, paper mode only by hard guard |
| Control API: status, pause/resume, kill, data endpoints (§6.4) | **Done** — bearer auth, public /health, CORS; SSE/WS push missing (dashboard polls) |
| Co-pilot mode: proposal queue, approve/reject, TTL + drift guards (§6.3) | **Done** — entries only; exits never wait for approval |
| Telegram notifications (§6.2) | **Done** — alerts only; command handling missing |
| Dashboard: status, chart, decisions, proposals, controls (§6.1) | **Done** — per-coin view with coin switcher; wizard, journal, research screens missing |
| Multi-coin support (§4.2) | **Done** — per-symbol feed+engine, shared account/breakers, per-coin dashboard, runtime add/remove via API + UI (coins persisted in Postgres; env var seeds first boot) |
| Automated improvement: sweep-validated self-tuning (§12.7) | **Done** — paper-scoped; self-feeding cycle (auto-evaluates when stale, findings target the next sweep's challengers with recorded lineage); promotes only statistically validated sweep winners that also survive the engine-backed confirmation gate (challenger vs incumbent replayed through the production engine — sizing, fees, stop lifecycle, breakers — before any promotion; a vetoed winner is alerted, never applied); versioned settings journal with UI revert; Telegram alert per promotion |
| Evaluation & training: blind walk-forward (§12) | **Done** — foundations, scenario engine (leak-tested), run orchestration + API, research screen, scenario replay viewer, learning findings (mined + human accept/reject), walk-forward parameter sweeps with explicit overfit verdicts, cross-family candidates, multiple validation windows, bootstrap confidence intervals + Bonferroni-corrected significance on every verdict; evaluation runs grade the production strategy shape (regime-routed families, self-classified per scenario) |
| News pipeline, regime gates, signal fusion (§5.2, §5.3) | **Partial** — BTC regime gate done (ADX trend/range + drawdown risk-off; family routing: trend entries in trends, mean-reversion entries in ranges; exits never gated; verdicts journaled as `gated` decisions); sentiment tighteners done (Fear & Greed extremes, BTC dominance surges, broad negative news flow — advisory, one-way, stale data contributes nothing); news pipeline done defensively (CryptoPanic polling + keyword classifier, negative-news coin flags, env-configured event windows); confirmation filters (order-flow/funding, P2 data) and automated calendar ingestion missing |
| Breakout strategy family (§5.2, review item 9) | **Research-only** — Donchian-channel entries (close clears the prior N-candle ceiling), turtle-style channel exits, shared ATR stop convention and managed-stop knobs; registered for sweeps/evaluation so research can pit it against the incumbents on identical scenarios, but deliberately unrouted in production: which regime activates it (and at whose expense) is a human decision the sweep evidence should inform, and the worker refuses to promote it until that route exists |
| Mean-reversion strategy family (§5.2 routing) | **Done** — RSI oversold-recovery entries, midline exits, same ATR stop convention as trend; optional trend-filter EMA (skip falling knives) as a sweepable knob; regime-routed per coin, both families' indicators always warm, exits pass from either family in any regime |
| Momentum strategy family (§13) | **Research + competition** — MACD histogram-crossover entries (12/26/9 defaults, zero-line filter on by default), histogram-flip exits, shared ATR stop convention; built from the TA-Lib-verified incremental EMA; sweepable and evaluated like every family, traded solo by its competition account, unrouted in production until a human routes it |
| Strategy competition: five paper bots, leaderboard, research comparison (§13) | **Done** — production regime router plus four solo-family challengers (trend, mean-reversion, breakout, momentum) trade the same coins, candles, and gates from isolated journal-backed paper accounts (bot-scoped fills/orders/decisions/risk rows, per-bot signal-id namespacing, full restart replay per account); GET /competition serves the equity-ranked leaderboard; POST /evaluations/compare grades the whole lineup on byte-identical scenario sets (one frozen window + seed, grouped runs) for the research screen's side-by-side table |
| Observability: dead-man's switch, metrics, DB backups (§4.9, §7) | **Done** — heartbeat ping gated on feed freshness; /metrics (feed lag, equity, breakers, bus counters) behind the bearer token; live-vs-backtest divergence measurable per coin (the §10 paper-gate metric: live paper fills matched against a same-candle replay of the production strategy shape; zero is the one-code-path expectation, non-zero is documented gating or a parity bug); scheduled gzipped-JSONL backups to S3-compatible storage with exact-Decimal restore (production restore drill pending, see checklist) |
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

### 4.2 Strategy Engine
- One strategy instance per (coin, strategy) pair, each consuming the event stream.
- Strategies are pluggable classes implementing a small interface:
  `on_candle(candle, context) -> Signal | None` where context exposes indicator
  history, current position, and config.
- Indicator library computed incrementally (EMA, RSI, MACD, Bollinger Bands, ATR,
  ADX, VWAP, volume profile) — no recomputing full history every candle.
- Built-in starter strategies:
  - **Trend following**: EMA cross + ADX trend filter, ATR trailing stop.
  - **Mean reversion**: RSI/Bollinger band reversion in ranging regimes.
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

1. **Regime gate (market-wide):** BTC regime (trend/range/risk-off via ADX + Fear &
   Greed extremes + BTC dominance shifts) decides *whether* altcoin entries are
   allowed at all and which strategy family (trend vs. mean-reversion) is active.
   The sentiment tighteners are family-aware where it matters: extreme *greed*,
   dominance surges, and broad negative news pause every family's entries, but
   extreme *fear* pauses only trend entries — mean-reversion exists to buy fear
   behind its protective stop, and a genuine crash is still caught by the
   drawdown-based risk-off state, which halts everything. Both Fear & Greed
   thresholds are operator-tunable (`TRADEBOT_SENTIMENT_EXTREME_FEAR_AT_OR_BELOW`,
   `TRADEBOT_SENTIMENT_EXTREME_GREED_AT_OR_ABOVE`).
2. **Entry signal (TA, per coin):** the active strategy produces the candidate
   buy/sell signal with stop and target. TA is the trigger; everything else filters.
3. **Confirmation filters (market microstructure & positioning):** order-flow (CVD
   direction, book imbalance) and derivatives positioning (funding not at a
   crowded extreme against the trade) must not contradict the signal. Each filter
   can only *block or shrink* a trade, never create one — this keeps the system
   testable and prevents filter soup.
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
significance level; every candidate's expectancy also carries a 95%
bootstrap confidence interval. Accepted findings link to the config
change they motivated, so every strategy version carries its lineage:
what changed, why, and whether validation confirmed it.

### 12.7 Automated improvement (paper-scoped)

The loop closes the research cycle without a human in the middle: on a
schedule (`TRADEBOT_AUTO_IMPROVE_*`), the bot derives single-knob variants
of the parameters it is trading right now, runs them through the blind
walk-forward sweep, and promotes the winner **only when the verdict is
validated** — the Bonferroni-corrected, multi-window statistical bar.
Training wins, near-misses, and findings never promote anything.

The cycle is self-feeding: when no fresh evaluation run exists, the
cycle starts one (with a sample size large enough that sweeps are never
starved below their minimum-trades bar); its completion mines findings
from the graded record in Postgres; and the next cycle's sweep adds
challengers *targeted at those findings* — a losing-downtrend pattern
toggles the mean-reversion trend filter, a chasing pattern toggles the
trend family's entry-extension filter — with the finding ids recorded as
the sweep's motivation. Patterns the bot has no knob for yet remain
human-facing suggestions.

Manual sweeps share this derivation: a sweep started without explicit
candidates (the dashboard's "run sweep" button) challenges the actively
traded parameters with the same findings-targeted grid — never a static
default grid — so the button tests exactly the knobs the findings on
screen point at, with the non-rejected finding ids recorded as the
sweep's motivation.

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
runs grade a named competition lineup entry (§13) — by default the
incumbent, i.e. the strategy shape production trades: the regime-routed
family router, with the regime self-classified from each scenario's own
candles (`evaluation/strategy.py` documents the divergence from the live
reference-market detector, which scenarios must not read). Findings
always only recommend. Sweep verdicts feed the automated improvement loop
(§12.7): in paper mode a *validated* challenger is promoted automatically;
anything touching live trading remains a human action.

---

## 13. Strategy competition (five bots, one winner)

One question drives this section: **which strategy is actually best?**
Backtests answer it on history; the competition answers it forward, with
the same paper money, at the same time, under the same rules.

### 13.1 The lineup

Five competitors, fixed in code (`competition/lineup.py`) because the
roster is an architecture decision — a stable lineup is what makes the
leaderboard's history meaningful:

| Bot id | Strategy |
|---|---|
| `production` | The incumbent: regime-routed trend + mean-reversion router (the bot's real shape) |
| `trend_following` | EMA crossover with ATR stops, solo, always on |
| `mean_reversion` | RSI oversold-recovery entries, midline exits, solo |
| `breakout` | Donchian-channel entries, turtle-style channel exits, solo |
| `momentum` | MACD histogram crossovers (zero-line filtered), solo |

Every challenger trades its family's **active** (possibly auto-promoted)
parameters, so the comparison is always against what the family trades
today, not a stale snapshot.

### 13.2 Fairness rules

The only variable is the strategy. Everything else is identical:

- **Same candles** — one market-data feed per symbol fans out to every
  account's engine over the bus; the competition multiplies accounts,
  never exchange connections or rate-limit spend.
- **Same gates** — regime gate, sentiment tighteners, and news vetoes
  apply to every account identically (§5.2).
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
human architecture decision informed by this evidence. Operator commands
stay whole-bot: pause/resume/kill act on every account, and a coin
cannot be removed while any account holds a position or order in it.
`TRADEBOT_COMPETITION_ENABLED=false` falls back to the incumbent alone.

### 13.4 The leaderboard

`GET /competition` returns every account ranked by equity (marks: newest
stored 1m closes, gathered once so no competitor is priced at a
different moment): equity, return on initial balance, realized and
unrealized PnL, open positions, entry/exit fill counts, and breaker
state. The dashboard renders it as the strategy battle card. Unknown
marks make an honest `null` equity that ranks last — never a guessed
number.

### 13.5 Research comparison (same question, graded scenarios)

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
Cancelling any member cancels the batch: half a comparison cannot answer
the question the batch asked.
