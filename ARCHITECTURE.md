# Crypto Spot Trading Bot — Architecture Plan

A spot-trading bot that uses technical analysis and market data to decide when to buy
and sell. The user adds a coin (trading pair) and the bot starts trading it
autonomously within configured risk limits.

This document is the plan. Nothing here is implemented yet.

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

### 4.5 Portfolio / State Manager
- Single source of truth for positions, balances, open orders, realized/unrealized PnL.
- Persisted in SQLite (later Postgres) so the bot resumes cleanly after a restart.
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
  WS disconnected). A simple web dashboard comes later; Telegram + API first.

### 4.8 Observability
- Structured JSON logging with event correlation (signal → order → fill chain).
- Metrics: data-feed lag, order round-trip time, fill rate, rejection reasons,
  live-vs-backtest signal divergence (a key health metric).
- Heartbeat alert if the data feed or trading loop stalls.

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

## 6. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12+, fully async (asyncio) | Best ecosystem for TA/backtesting; spot TA trading at 1m+ timescale doesn't need lower latency |
| Primary exchange | **Binance** if available in user's region; **Kraken** as regulated alternative; **Coinbase Advanced Trade** if US-only (accepting higher fees) | Liquidity, 0.1% fees, best API & free historical data; fee level directly determines how much edge a strategy needs |
| Exchange connectivity | CCXT + native WS client for primary exchange | Breadth + low-latency streams; switching exchanges later is cheap behind the adapter |
| Indicators | pandas-ta / TA-Lib + incremental implementations | Standard, well-tested |
| Storage | SQLite + Parquet files first; TimescaleDB when needed | Zero ops to start; clean upgrade path |
| API | FastAPI + Pydantic | Async, typed, quick |
| Backtest screening | vectorbt | Fast parameter sweeps |
| Config | YAML per coin/strategy, Pydantic-validated | Reproducible runs |
| Deployment | Docker Compose on a small VPS near the exchange region | 24/7 uptime, simple |
| Secrets | Env vars; exchange API keys with **trade-only** permissions (withdrawals disabled), IP-allowlisted | Limits blast radius |

## 7. Roadmap

**Phase 1 — Foundation (the part most bots skip and regret):**
project skeleton, exchange adapter interface, market data service with persistence,
portfolio state, backtester with fee/slippage model, one trend-following strategy,
walk-forward report. Exit criteria: a backtest run is reproducible end-to-end.

**Phase 2 — Paper trading:**
live data feed, paper execution adapter, risk manager, Telegram alerts, control API
with the add-a-coin flow. Exit criteria: bot paper-trades a coin unattended for 2+
weeks with no crashes, no data gaps, and live signals matching backtest signals.

**Phase 3 — Live trading, small:**
live execution adapter with reconciliation, circuit breakers and kill switch proven by
fault-injection tests, deploy to VPS. Start with small capital on one coin.
Exit criteria: 4+ weeks live with execution quality (slippage, fill rate) matching the
backtest model's assumptions.

**Phase 4 — Edge expansion:**
mean-reversion strategy + regime router, multi-coin portfolio limits, parameter
re-optimization pipeline (scheduled walk-forward), P2 data sources, optional ML
(e.g., regime classification) — only where it beats the simple baseline out of sample.

## 8. Key risks and mitigations

- **Overfitting** — the #1 killer. Mitigation: walk-forward only, few parameters,
  penalize complexity, require out-of-sample beat of buy-and-hold net of fees.
- **Fees + slippage eating the edge.** Mitigation: model both pessimistically in
  backtests; prefer maker orders; trade fewer, higher-conviction signals.
- **Exchange/operational failures** (WS drops, rate limits, partial fills, restarts).
  Mitigation: reconciliation on startup, idempotent orders, fault-injection tests.
- **Tail events** (flash crashes, delistings). Mitigation: hard stops, daily loss
  circuit breaker, max exposure caps, kill switch, only trade liquid pairs.
- **Key compromise.** Mitigation: trade-only API keys, IP allowlists, no withdrawal
  permission, secrets never in the repo.

## 9. Profitability validation gates

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
