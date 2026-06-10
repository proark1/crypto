# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

Autonomous crypto **spot trading bot**: technical analysis + market data signals,
per-coin autonomy modes (autonomous / co-pilot approval), strict risk management.
The full design is in **ARCHITECTURE.md** — read the relevant section before
implementing a component, and update it in the same PR when a design decision changes.
Deployment target is **Railway** (backend worker + static frontend + Postgres).

## Repository structure

```
crypto/
├── ARCHITECTURE.md          # The design document — source of truth
├── CLAUDE.md
├── backend/                 # Python 3.12+, fully async
│   ├── pyproject.toml       # uv-managed; ruff + mypy + pytest configured here
│   ├── src/tradebot/
│   │   ├── core/            # event bus, domain events, config loading, clock
│   │   ├── marketdata/      # WS/REST ingestion, candle building, gap-fill, validation
│   │   ├── indicators/      # incremental indicators (EMA, RSI, ATR, ...)
│   │   ├── strategies/      # pluggable strategies; one file per strategy
│   │   ├── signals/         # regime gates, confirmation filters, signal fusion
│   │   ├── news/            # news ingestion, classification, event calendar
│   │   ├── risk/            # position sizing, limits, circuit breakers, kill switch
│   │   ├── execution/       # adapter interface + backtest/paper/live implementations
│   │   ├── portfolio/       # positions, balances, PnL, persistence (Postgres)
│   │   ├── backtest/        # runner, fill simulator, walk-forward, reports
│   │   ├── authorization/   # autonomy modes, proposal queue, approvals
│   │   ├── api/             # FastAPI routes (control plane), auth, WS pushes
│   │   └── notify/          # Telegram bot (alerts, approve/reject)
│   └── tests/               # mirrors src layout; + tests/golden/ for golden backtests
├── frontend/                # React + TypeScript + Tailwind, Vite, PWA
│   └── src/
│       ├── api/             # typed client for backend API
│       ├── components/
│       ├── screens/         # overview, coin-detail, wizard, journal, research, settings
│       └── lib/
└── .github/workflows/       # CI: lint, typecheck, tests, golden backtest
```

Module boundaries follow ARCHITECTURE.md section 3: strategies never place orders,
the execution engine accepts orders only from the risk manager, and nothing in
`strategies/` or `signals/` may know whether it runs in backtest, paper, or live.

## Non-negotiable safety invariants

These exist because this code moves real money. Never weaken them to make a test
pass or a feature simpler:

1. **Money is `Decimal`, never `float`.** All prices, quantities, balances, and PnL.
   Floats are acceptable only inside indicator math that never feeds an order size
   directly.
2. **All timestamps are timezone-aware UTC.** Naive datetimes are a bug.
3. **One code path:** strategy/risk code must work identically under backtest, paper,
   and live adapters. Never add `if live:` branches to strategy logic.
4. **Orders flow only through the risk manager** into the execution engine, carrying
   their signal lineage. Do not add any other order-placing code path, including in
   tests of live code, scripts, or "temporary" tools.
5. **Protective stops are exchange-native** resting orders wherever supported.
6. **Paper mode is the default** everywhere a mode is chosen; going live is always an
   explicit, confirmed action. New config defaults must fail safe.
7. **No secrets in the repo** — no API keys, tokens, or chat IDs, including in tests,
   fixtures, and docs. Config comes from environment variables.
8. **Single bot replica.** Never introduce horizontal scaling for the worker.

## Code style

### Python (backend)
- Python 3.12+, `asyncio` throughout the bot core; no blocking I/O in async paths.
- **Type hints on everything**; `mypy --strict` must pass. Pydantic models for config,
  API schemas, and domain objects crossing module boundaries.
- Lint/format with **ruff** (format + lint); both must be clean before commit.
- Docstrings on every public module, class, and function — explain *why* and the
  contract (units, invariants), not a restatement of the code. Document units
  explicitly wherever amounts appear (quote vs. base currency).
- No bare `except:`. Catch specific exceptions; anything swallowed must be logged
  with context. Errors in order/position handling are never silently ignored.
- Structured logging (one JSON-able event per line) with correlation IDs following
  the signal → order → fill chain. Never log secrets.
- Naming: descriptive over short (`entry_price_quote`, not `ep`). One concept per
  module; if a file needs a section header comment, split it.

### TypeScript (frontend)
- TypeScript strict mode; no `any` without a comment justifying it.
- ESLint + Prettier clean before commit.
- All backend data accessed through the typed client in `frontend/src/api/` — no ad
  hoc `fetch` calls in components.
- Amounts arrive from the API as strings (Decimal-safe); never do money arithmetic
  in the frontend beyond display formatting.

## Testing requirements

- Every new module ships with tests in the mirrored path under `backend/tests/`.
- Indicator implementations are tested against TA-Lib reference outputs.
- Risk math gets property-based tests (sizing never exceeds limits, stops never zero).
- The **golden backtest** (fixed dataset + config → byte-identical trades) runs in CI;
  if your change alters its output, the diff must be intentional and explained in the
  PR description — never regenerate the golden file just to make CI pass.
- Execution-engine changes need fault-injection coverage (disconnects, partial fills,
  restarts, exchange errors) against the mock exchange.
- Run the full check locally before pushing:
  `ruff check && ruff format --check && mypy && pytest` (backend),
  `npm run lint && npm run typecheck && npm test` (frontend).

## Git & PR conventions

- Branches: `feat/<topic>`, `fix/<topic>`, `chore/<topic>`.
- Commits: imperative mood, small and focused; the subject says *what*, the body
  says *why* when it isn't obvious.
- PRs stay reviewable (aim < ~500 lines of diff); split bigger work. CI must be green.
- A PR that changes architecture-level behavior updates ARCHITECTURE.md in the same PR.
- Never commit directly to `main`; `main` auto-deploys to the Railway paper
  environment, so it must always be deployable.

## Efficiency guidelines

- Indicators are computed **incrementally** per candle — never recompute full history
  on each tick. Keep per-candle work O(1) where possible.
- Batch DB writes (e.g., candle inserts) and use indexed queries; the hot path
  (candle → signal → order decision) must not block on unindexed scans.
- Respect exchange rate limits via a shared budget in the execution engine — never
  add raw API calls that bypass it.
- Prefer simple code over premature optimization everywhere outside the hot path.
