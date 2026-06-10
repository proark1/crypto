# crypto

Autonomous crypto **spot trading bot**: technical analysis + market data signals,
per-coin autonomy modes (autonomous / co-pilot approval), strict risk management.

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the design document and source of truth.
- **[CLAUDE.md](CLAUDE.md)** — repository structure, safety invariants, and coding standards.

## Layout

- `backend/` — Python 3.12+ bot core (uv-managed). See `backend/pyproject.toml`.
- `frontend/` — React + TypeScript dashboard (coming with Phase 2).

## Backend development

```bash
cd backend
uv sync --dev
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

All four checks must pass before pushing; CI enforces them on every PR.
