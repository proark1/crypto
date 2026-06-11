# crypto

Autonomous crypto **spot trading bot**: technical analysis + market data signals,
per-coin autonomy modes (autonomous / co-pilot approval), strict risk management.

The bot runs a **strategy competition** (ARCHITECTURE.md §13): five paper
accounts — the production regime router plus four solo challengers (trend
following, mean reversion, breakout, MACD momentum) — trade the same coins,
candles, and gates from their own journal-backed balances. The dashboard's
leaderboard ranks them by equity (each bot is clickable for a full detail
page and individually pausable/stoppable), and the research screen can grade
the whole lineup on identical historical scenarios for a direct side-by-side
comparison. You can also **build your own bot** from the dashboard: pick one
or more rules, mix them ("any rule fires" vs. "all rules agree"), tune the
parameters, and it joins the competition live. The dashboard supports light
and dark mode.

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the design document and source of truth
  (includes the implementation-status table).
- **[CLAUDE.md](CLAUDE.md)** — repository structure, safety invariants, and coding standards.
- **[LIVE_TRADING_CHECKLIST.md](LIVE_TRADING_CHECKLIST.md)** — the hard blockers and
  soak runbook gating live trading.

## Layout

- `backend/` — Python 3.12+ bot core (uv-managed). See `backend/pyproject.toml`.
- `frontend/` — React + TypeScript dashboard (Vite, Tailwind).

## Development

```bash
# backend
cd backend
uv sync --dev
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest

# frontend
cd frontend
npm install
npm run lint && npm run typecheck && npm test && npm run build
```

All checks must pass before pushing; CI enforces them on every PR.
Backend tests need a Postgres (CI provides one; locally set `TEST_DATABASE_URL`
or run a default-config Postgres with a `tradebot_test` database).

## Deploying to Railway (paper trading)

One Railway project, three services. The bot runs **paper mode only** for now —
live trading is a Phase 3 milestone and the worker refuses to start in any
other mode.

> **The one setting that matters:** this is a monorepo. Each service MUST set
> **Settings → Source → Root Directory** to `backend` or `frontend`. Without
> it, Railpack analyzes the repo root, finds only folders, and fails with
> "could not determine how to build the app". Build and start commands are
> then picked up automatically from the `railway.json` in each directory.

### 1. Postgres

Add Railway's Postgres. Its default `DATABASE_URL` can be used as-is — the
bot accepts any standard Postgres DSN and switches it to its async driver
internally.

### 2. `bot` (backend worker + control API)

- **Root directory: `backend`** (see above)
- Start command, restart policy, and the `/health` healthcheck come from
  `backend/railway.json` — nothing to configure.
- **Exactly 1 replica — never scale this service horizontally.**
- Environment variables:

| Variable | Required | Example / default |
|---|---|---|
| `TRADEBOT_DATABASE_URL` | yes | `${{Postgres.DATABASE_URL}}` — any standard Postgres DSN works; the bot rewrites the scheme to its asyncpg driver itself |
| `TRADEBOT_API_TOKEN` | for the API/dashboard | long random string; API stays off without it |
| `TRADEBOT_API_PORT` | no | falls back to Railway's injected `PORT` automatically |
| `TRADEBOT_API_CORS_ORIGINS` | no | `*` (safe with bearer-header auth); set to the dashboard URL, e.g. `https://frontend-xxxx.up.railway.app`, for defence in depth |
| `TRADEBOT_EXCHANGE_ID` | no | `binance` (any CCXT id: `kraken`, `coinbase`, ...) |
| `TRADEBOT_SYMBOLS` | no | `BTC/USDT` — comma-separated, e.g. `BTC/USDT,ETH/USDT` (all pairs share the quote currency; singular `TRADEBOT_SYMBOL` still works). **Seeds the coin list on first boot only** — afterwards add/remove coins from the dashboard and the database is the source of truth |
| `TRADEBOT_PAPER_INITIAL_BALANCE_QUOTE` | no | `10000` — seeds every competition account equally |
| `TRADEBOT_COMPETITION_ENABLED` | no | `true` — run the five-bot strategy competition; `false` trades the production router alone |
| `TRADEBOT_HISTORY_BACKFILL_DAYS` | no | `1460` — a full ~4-year crypto cycle of 1m history (free, public REST) fetched the first time a coin has no stored candles; existing databases are deepened to the horizon on the next boot. Expect the initial crawl to take ~30–60 min per coin-year and ~0.5 GB of Postgres per coin-year |
| `TRADEBOT_HEARTBEAT_URL` | recommended | a healthchecks.io ping URL; the monitor alerts when the bot (or its data feed) goes silent |
| `TRADEBOT_HEARTBEAT_INTERVAL_SECONDS` | no | `60` |
| `TRADEBOT_TELEGRAM_BOT_TOKEN` | for alerts | from @BotFather |
| `TRADEBOT_TELEGRAM_CHAT_ID` | for alerts | your chat id |
| `TRADEBOT_LOG_LEVEL` | no | `INFO` |

No exchange API keys are needed for paper trading — market data is public.

### 3. `frontend` (dashboard)

- **Root directory: `frontend`** (see above)
- Build and serve come from `frontend/railway.json` (Vite build, static
  `dist/` served by `serve`) — nothing to configure.
- Build-time variable: `VITE_API_URL` = the public URL of the `bot` service.
- On first load the dashboard asks for the bearer token (`TRADEBOT_API_TOKEN`).

### The paper soak

The Phase 2 exit criterion (ARCHITECTURE.md section 8): the bot paper-trades
unattended for **2+ weeks** with no crashes, no data gaps, and live signals
matching backtest signals. Watch `/status` for last-candle freshness, Telegram
for fills, and the Railway logs for warnings before trusting it further.
