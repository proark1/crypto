# crypto

Autonomous crypto **spot trading bot**: technical analysis + market data signals,
per-coin autonomy modes (autonomous / co-pilot approval), strict risk management.

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the design document and source of truth.
- **[CLAUDE.md](CLAUDE.md)** — repository structure, safety invariants, and coding standards.

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

Add Railway's Postgres. Note its connection string; the bot needs it as an
asyncpg DSN (`postgresql+asyncpg://user:pass@host:port/db`).

### 2. `bot` (backend worker + control API)

- **Root directory: `backend`** (see above)
- Start command, restart policy, and the `/health` healthcheck come from
  `backend/railway.json` — nothing to configure.
- **Exactly 1 replica — never scale this service horizontally.**
- Environment variables:

| Variable | Required | Example / default |
|---|---|---|
| `TRADEBOT_DATABASE_URL` | yes | `postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}` (Railway reference variables — the default `DATABASE_URL` uses the plain `postgresql://` scheme, which asyncpg's SQLAlchemy driver does not accept) |
| `TRADEBOT_API_TOKEN` | for the API/dashboard | long random string; API stays off without it |
| `TRADEBOT_API_PORT` | no | falls back to Railway's injected `PORT` automatically |
| `TRADEBOT_EXCHANGE_ID` | no | `binance` (any CCXT id: `kraken`, `coinbase`, ...) |
| `TRADEBOT_SYMBOL` | no | `BTC/USDT` |
| `TRADEBOT_PAPER_INITIAL_BALANCE_QUOTE` | no | `10000` |
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
