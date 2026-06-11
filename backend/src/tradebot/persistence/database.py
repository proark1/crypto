"""Engine lifecycle and schema definition.

One ``Database`` per process wraps the async engine. The schema is created
idempotently at startup — migrations (Alembic) become worthwhile once the
schema stops churning; revisit after Phase 2 settles.
"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

metadata = MetaData()

# NUMERIC with no precision cap: Postgres stores exact arbitrary-precision
# decimals, matching the Decimal-only money invariant end to end.
candles_table = Table(
    "candles",
    metadata,
    Column("symbol", Text, primary_key=True),
    Column("interval", Text, primary_key=True),
    Column("open_time", DateTime(timezone=True), primary_key=True),
    Column("close_time", DateTime(timezone=True), nullable=False),
    Column("open_quote", Numeric, nullable=False),
    Column("high_quote", Numeric, nullable=False),
    Column("low_quote", Numeric, nullable=False),
    Column("close_quote", Numeric, nullable=False),
    Column("volume_base", Numeric, nullable=False),
)
"""PK (symbol, interval, open_time) doubles as the range-scan index for the
hot fetch path (one symbol+interval ordered by time)."""

fills_table = Table(
    "fills",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("client_order_id", Text, nullable=False, index=True),
    Column("symbol", Text, nullable=False, index=True),
    Column("side", Text, nullable=False),
    Column("price_quote", Numeric, nullable=False),
    Column("quantity_base", Numeric, nullable=False),
    Column("fee_quote", Numeric, nullable=False),
    Column("filled_at", DateTime(timezone=True), nullable=False),
)
"""Append-only: fills are facts. A surrogate id because one order can fill
in several parts with identical timestamps."""

orders_table = Table(
    "orders",
    metadata,
    Column("client_order_id", Text, primary_key=True),
    Column("signal_id", Text, nullable=False),
    Column("symbol", Text, nullable=False, index=True),
    Column("side", Text, nullable=False),
    Column("order_type", Text, nullable=False),
    Column("quantity_base", Numeric, nullable=False),
    Column("limit_price_quote", Numeric, nullable=True),
    Column("stop_price_quote", Numeric, nullable=True),
    Column("protective_stop_price_quote", Numeric, nullable=True),
    Column("protective_limit_price_quote", Numeric, nullable=True),
    Column("protective_breakeven_at_r", Float, nullable=True),
    Column("protective_trail_distance_quote", Numeric, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("triggered", Boolean, nullable=False),
    Column("status_at", DateTime(timezone=True), nullable=False),
)
"""Every order intent the engine ever submitted, with its latest known state
(open / filled / cancelled) — the recovery source for in-flight orders, so a
restart can re-arm the paper adapter instead of silently dropping them.
Keyed by ``client_order_id``: ids are deterministic per intent, so the same
intent resubmitted after a cancel reopens its row rather than duplicating it.
``triggered`` latches a stop-limit whose stop has crossed (the order behaves
as a plain limit from then on, exactly like a real exchange), so a restart
cannot un-trigger a stop. The ``protective_*`` columns flatten an entry's
``ProtectiveExitPlan`` (initial level, limit floor, ratchet policy) so a
position can be re-protected from the journal — exactly, policy included —
even if the crash landed between the entry fill and the stop placement."""

decisions_table = Table(
    "decisions",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("signal_id", Text, nullable=False),
    Column("strategy_name", Text, nullable=False),
    Column("symbol", Text, nullable=False, index=True),
    Column("side", Text, nullable=False),
    Column("stop_price_quote", Numeric, nullable=False),
    Column("reasons", ARRAY(Text), nullable=False),
    Column("outcome", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
"""Every signal and its fate (submitted/vetoed/paused) — the audit trail the
decision-pipeline UI reads. Append-only, like fills."""


coins_table = Table(
    "coins",
    metadata,
    Column("symbol", Text, primary_key=True),
    Column("added_at", DateTime(timezone=True), nullable=False),
)
"""The actively traded pairs — the runtime source of truth. The env var
``TRADEBOT_SYMBOLS`` only seeds this table on first boot; afterwards coins
are added and removed through the control API."""

strategy_settings_table = Table(
    "strategy_settings",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("family", Text, nullable=False, index=True),
    Column("params", JSONB, nullable=False),
    Column("source_sweep_id", BigInteger, nullable=True),
    Column("note", Text, nullable=True),
    Column("activated_at", DateTime(timezone=True), nullable=False),
)
"""Versioned strategy parameters, append-only: the newest row per family is
what the bot trades; every promotion (automated, or a manual revert) appends
a new row carrying its lineage (the sweep that validated it). History is
never rewritten — §12.5 requires every config to know where it came from."""


evaluation_runs_table = Table(
    "evaluation_runs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("symbols", ARRAY(Text), nullable=False),
    Column("timeframes", ARRAY(Text), nullable=False),
    Column("config", JSONB, nullable=False),
    Column("code_version", Text, nullable=False),
    Column("progress_done", Integer, nullable=False),
    Column("progress_total", Integer, nullable=False),
    Column("summary", JSONB, nullable=True),
)
"""One blind walk-forward evaluation run (ARCHITECTURE.md section 12).
``config`` snapshots the full run + strategy configuration so results are
never orphaned from the rules that produced them; old runs are never
overwritten or rescored."""

scenarios_table = Table(
    "scenarios",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", BigInteger, ForeignKey("evaluation_runs.id"), nullable=False, index=True),
    Column("symbol", Text, nullable=False),
    Column("timeframe", Text, nullable=False),
    Column("decision_time", DateTime(timezone=True), nullable=False),
    Column("lookback_candles", Integer, nullable=False),
    Column("scenario_class", Text, nullable=False),
    Column("trend", Text, nullable=False),
    Column("volatility", Text, nullable=False),
    Column("events", ARRAY(Text), nullable=False),
    Column("seed", BigInteger, nullable=False),
)
"""One blind decision point. Candles are referenced by coordinates
(symbol, timeframe, decision_time, lookback) — never copied out of the
candles table, which stays the single source of price truth."""

scenario_results_table = Table(
    "scenario_results",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "scenario_id",
        BigInteger,
        ForeignKey("scenarios.id"),
        nullable=False,
        index=True,
        unique=True,
    ),
    Column("decision", Text, nullable=False),
    Column("confidence", Numeric, nullable=True),
    Column("reasons", ARRAY(Text), nullable=False),
    Column("entry_price_quote", Numeric, nullable=True),
    Column("exit_price_quote", Numeric, nullable=True),
    Column("r_multiple", Numeric, nullable=True),
    Column("pnl_quote", Numeric, nullable=True),
    Column("mfe_r", Numeric, nullable=True),
    Column("mae_r", Numeric, nullable=True),
    Column("duration_candles", Integer, nullable=True),
    Column("stop_hit", Boolean, nullable=True),
    Column("oracle_r", Numeric, nullable=True),
    Column("verdict", Text, nullable=False),
    Column("timing", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
"""The decision the bot made blind, and its grade once the future was
revealed. Trade fields are null for hold decisions; ``oracle_r`` is the
hindsight-perfect benchmark (analysis only, never a simulated exit)."""

learning_findings_table = Table(
    "learning_findings",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", BigInteger, ForeignKey("evaluation_runs.id"), nullable=False, index=True),
    Column("pattern", Text, nullable=False),
    Column("evidence_scenario_ids", ARRAY(BigInteger), nullable=False),
    Column("affected_count", Integer, nullable=False),
    Column("average_r_impact", Numeric, nullable=False),
    Column("suggestion", Text, nullable=False),
    Column("confidence", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
"""Mined mistake patterns with their evidence. Findings recommend; a human
accepts or rejects — the system never changes trading rules by itself."""

sweeps_table = Table(
    "sweeps",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("timeframe", Text, nullable=False),
    Column("config", JSONB, nullable=False),
    Column("motivating_finding_ids", ARRAY(BigInteger), nullable=False),
    Column("report", JSONB, nullable=True),
)
"""One walk-forward parameter sweep (ARCHITECTURE.md section 12.5).
``config`` snapshots the full candidate grid and split; ``report`` carries
training scores, validation scores, and the plain-words verdict (validated /
overfit / baseline best). ``motivating_finding_ids`` is the lineage link from
accepted findings to the config change they motivated."""


_SYNC_SCHEME_PREFIXES = (
    "postgres://",
    "postgresql://",
    "postgresql+psycopg2://",
    "postgresql+psycopg://",
)


def coerce_async_dsn(url: str) -> str:
    """Rewrite common Postgres URL schemes to the asyncpg driver.

    Platforms hand out plain ``postgresql://`` (Railway) or legacy
    ``postgres://`` (Heroku-style) DSNs; SQLAlchemy would resolve those to
    the synchronous psycopg2 driver, which is not installed — by design,
    this codebase is asyncpg-only. Coercing the scheme here means any
    standard Postgres URL can be pasted into ``TRADEBOT_DATABASE_URL``
    without the deploy crash-looping on a driver import.
    """
    # Schemes are case-insensitive (RFC 3986 §3.1), and copy-pasted env
    # vars routinely carry stray whitespace.
    cleaned = url.strip()
    lowered = cleaned.lower()
    if lowered.startswith("postgresql+asyncpg://"):
        return cleaned
    for prefix in _SYNC_SCHEME_PREFIXES:
        if lowered.startswith(prefix):
            return "postgresql+asyncpg://" + cleaned[len(prefix) :]
    return cleaned


class Database:
    """Owns the async engine; use as an async context manager."""

    def __init__(self, url: str) -> None:
        """Create an engine for ``url`` (any standard Postgres DSN form).

        ``pool_pre_ping`` because the bot runs for weeks against a managed
        Postgres: pooled connections silently die across DB restarts and
        idle timeouts, and the first query after that must not be the one
        that fails.
        """
        self._engine: AsyncEngine = create_async_engine(coerce_async_dsn(url), pool_pre_ping=True)

    @property
    def engine(self) -> AsyncEngine:
        """The underlying engine, for stores and tests."""
        return self._engine

    async def create_schema(self) -> None:
        """Create all tables if they do not exist (idempotent)."""
        async with self._engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

    async def __aenter__(self) -> Database:
        """Enter context: the engine connects lazily, nothing to do yet."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Dispose the engine and its connection pool."""
        await self._engine.dispose()
