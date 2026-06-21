"""Engine lifecycle and schema definition.

One ``Database`` per process wraps the async engine. The schema is created
idempotently at startup — migrations (Alembic) become worthwhile once the
schema stops churning; revisit after Phase 2 settles.
"""

from __future__ import annotations

import logging
from types import TracebackType

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Connection,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.schema import CreateColumn

from tradebot.core.logging import log_event

logger = logging.getLogger(__name__)

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

funding_rates_table = Table(
    "funding_rates",
    metadata,
    Column("symbol", Text, primary_key=True),
    Column("funding_time", DateTime(timezone=True), primary_key=True),
    Column("rate", Numeric, nullable=False),
)
"""Perpetual funding history — the researchable series behind the funding
strategy and the restart-durable source for the funding tightener. PK
(symbol, funding_time) doubles as the range-scan index for the by-time lookup."""

fills_table = Table(
    "fills",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("bot_id", Text, nullable=False, server_default="production", index=True),
    Column("client_order_id", Text, nullable=False, index=True),
    Column("symbol", Text, nullable=False, index=True),
    Column("side", Text, nullable=False),
    Column("price_quote", Numeric, nullable=False),
    Column("quantity_base", Numeric, nullable=False),
    Column("fee_quote", Numeric, nullable=False),
    Column("filled_at", DateTime(timezone=True), nullable=False),
)
"""Append-only: fills are facts. A surrogate id because one order can fill
in several parts with identical timestamps. ``bot_id`` namespaces the
strategy competition's paper accounts; rows that predate the competition
default to ``production`` — they always belonged to the production bot."""

orders_table = Table(
    "orders",
    metadata,
    Column("client_order_id", Text, primary_key=True),
    Column(
        "bot_id", Text, primary_key=True, nullable=False, server_default="production", index=True
    ),
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
Keyed by the composite ``(client_order_id, bot_id)``: the table is shared
across every competition account, and ids are only deterministic *within* a
bot's signal-id namespace, so keying on the id alone would let one bot's
order silently overwrite another's (and its protective exit plan). The same
intent resubmitted after a cancel reopens its own row rather than duplicating
it; an existing single-column-PK database is widened in place at startup.
``triggered`` latches a stop-limit whose stop has crossed (the order behaves
as a plain limit from then on, exactly like a real exchange), so a restart
cannot un-trigger a stop. The ``protective_*`` columns flatten an entry's
``ProtectiveExitPlan`` (initial level, limit floor, ratchet policy) so a
position can be re-protected from the journal — exactly, policy included —
even if the crash landed between the entry fill and the stop placement."""

risk_state_table = Table(
    "risk_state",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tripped_reason", Text, nullable=True),
    Column("day", Date, nullable=True),
    Column("day_start_equity_quote", Numeric, nullable=True),
    Column("entries_today", Integer, nullable=False),
    Column("peak_equity_quote", Numeric, nullable=True),
    Column("consecutive_losses", Integer, nullable=False),
    Column("cooldown_until", DateTime(timezone=True), nullable=True),
    Column("last_observed_time", DateTime(timezone=True), nullable=True),
    Column("paused_symbols", ARRAY(Text), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
"""One row (id=1): the account-level brake state and which engines were
paused/halted. Persisted so a deploy cannot silently release a tripped
breaker, reset a daily loss anchor, or resume a killed bot."""

decisions_table = Table(
    "decisions",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("bot_id", Text, nullable=False, server_default="production", index=True),
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

custom_bots_table = Table(
    "custom_bots",
    metadata,
    Column("bot_id", Text, primary_key=True),
    Column("label", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("rules", JSONB, nullable=False),
    Column("risk_state_row_id", Integer, nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
"""User-built competition bots (ARCHITECTURE.md §13.6). ``rules`` is the
validated recipe (families + per-family parameters + entry mode);
``risk_state_row_id`` is this bot's permanently reserved ``risk_state``
row, allocated at creation and never reused — the built-in lineup owns
rows 1-5, custom bots start at 100. Deleting a bot keeps its journals
(fills/orders/decisions stay queryable under its bot_id) but frees the
id for nothing: ids are forever."""

bot_capital_table = Table(
    "bot_capital",
    metadata,
    Column("bot_id", Text, primary_key=True),
    Column("initial_balance_quote", Numeric, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
"""Operator-set starting capital per bot (production, built-in, or custom).
Absent until the operator resets a bot's capital; until then the bot uses the
config default (``AppConfig.paper_initial_balance_quote``). NUMERIC keeps the
balance exact, matching the Decimal money invariant."""

trading_fees_table = Table(
    "trading_fees",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("buy_fee_bps", Numeric, nullable=False),
    Column("sell_fee_bps", Numeric, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
"""Operator-set trading fees, one row (id=1). Buy and sell fees in basis
points, applied to every live paper fill. Absent until the operator saves
fees in the UI; until then the boot defaults (``AppConfig.buy/sell_fee_bps``)
apply. NUMERIC keeps the rate exact, matching the Decimal money invariant."""

campaign_settings_table = Table(
    "campaign_settings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
"""Operator's runtime on/off for the §12.7 research-campaign loop, one row
(id=1). Absent until first toggled in the UI; until then the boot default
(``AppConfig.campaign_enabled``) applies. Flipping it takes effect live — the
driver reads it each turn — so no redeploy, like the trading-fees setting."""


candidacy_alerts_table = Table(
    "candidacy_alerts",
    metadata,
    Column("family", Text, primary_key=True),
    Column("alerted_at", DateTime(timezone=True), nullable=False),
)
"""One row per research family the operator has already been told earned §13.7
routing candidacy — the dedup so the alert fires once per family, not on every
watch tick (and not again after a redeploy). The row is appended only after the
alert is delivered, so delivery is at-least-once: a crash between a successful
send and this insert re-alerts that family once on the next boot (the safe
direction). The family stays recorded even if it later loses candidacy, so a
brief flap does not re-spam."""


campaign_history_table = Table(
    "campaign_history",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("finished_at", DateTime(timezone=True), nullable=False, index=True),
    Column("snapshot", JSONB, nullable=False),
)
"""Append-only record of finished §12.7 campaigns, one row each. ``snapshot``
is the JSON-able campaign summary the live ``GET /campaign`` also returns
(target, symbol, status, round trail with per-promotion diffs, holdout read)
so history reads back without recomputation. The in-memory driver only ever
holds the current campaign; this is how past campaigns survive a restart."""


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
    Column("strategy", Text, nullable=False, server_default="production"),
    Column("comparison_group", BigInteger, nullable=True, index=True),
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
overwritten or rescored. ``strategy`` names the competition lineup entry
the run graded (runs that predate the competition graded the production
shape); runs sharing a ``comparison_group`` were generated over identical
scenario sets, so their summaries compare strategies directly."""

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


bake_off_jobs_table = Table(
    "bake_off_jobs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("config", JSONB, nullable=False),
    Column("contestants", ARRAY(Text), nullable=False),
    Column("cells_done", Integer, nullable=False),
    Column("cells_total", Integer, nullable=False),
    Column("results", JSONB, nullable=True),
)
"""One bake-off (ARCHITECTURE.md section 13.8): the fixed contestant roster
graded across a grid of (timeframe, history-window) cells and ranked by
average return. ``config`` snapshots the grid and scenario shape;
``contestants`` is the roster in comparison order; ``results`` carries the
per-cell records and the running/final ranking (updated after every cell,
so a mid-flight job already shows a partial leaderboard). Each cell's runs
live in the ordinary ``evaluation_runs`` table, linked by their
``comparison_group``; this table is the bake-off layer above them."""


def _add_missing_columns(connection: Connection, schema_metadata: MetaData = metadata) -> None:
    """Issue ``ADD COLUMN IF NOT EXISTS`` for columns the live DB lacks.

    Runs inside ``create_schema``'s transaction. New columns must be
    nullable or carry a ``server_default``: a NOT NULL column without one
    cannot be added to a populated table, and discovering that at deploy
    time (here, with a clear message) beats discovering it as a crashed
    INSERT mid-trade.
    """
    inspector = inspect(connection)
    for table in schema_metadata.tables.values():
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable and column.server_default is None:
                raise RuntimeError(
                    f"cannot add NOT NULL column {table.name}.{column.name} without a "
                    "server_default: existing rows would violate it. Make it nullable, "
                    "give it a server_default, or adopt real migrations."
                )
            ddl = CreateColumn(column).compile(dialect=connection.dialect)
            connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS {ddl}'))
            log_event(
                logger,
                logging.WARNING,
                "schema_column_added",
                table=table.name,
                column=column.name,
            )


def _widen_orders_primary_key(connection: Connection) -> None:
    """Widen the orders primary key to (client_order_id, bot_id) in place.

    The orders table once keyed on ``client_order_id`` alone; the competition
    made it a *shared* table where that id is only unique within a bot's
    signal-id namespace, so the key must include ``bot_id`` or one bot's order
    could silently overwrite another's (and its protective stop plan).
    ``create_all`` builds new databases with the composite key directly; this
    widens an existing single-column-PK database. It is the one sanctioned
    in-place key change because it is safe *by construction* — a wider key can
    never be violated by rows a narrower one already kept unique — so unlike a
    genuine destructive migration it needs no Alembic ceremony.
    """
    inspector = inspect(connection)
    if "orders" not in inspector.get_table_names():
        return  # create_all just built it with the composite key
    primary_key = inspector.get_pk_constraint("orders")
    if primary_key.get("constrained_columns") != ["client_order_id"]:
        return  # already the composite key (fresh build or prior widen)
    constraint_name = primary_key["name"]
    connection.execute(text(f'ALTER TABLE orders DROP CONSTRAINT "{constraint_name}"'))
    connection.execute(text("ALTER TABLE orders ADD PRIMARY KEY (client_order_id, bot_id)"))
    log_event(
        logger,
        logging.WARNING,
        "orders_primary_key_widened",
        old="client_order_id",
        new="client_order_id,bot_id",
    )


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
        """Create missing tables AND missing columns (additive sync).

        ``create_all`` only creates absent tables — a column added to a
        table that already shipped would silently never reach the deployed
        database, and the first INSERT naming it would crash the worker.
        This sync closes that gap for the only schema evolution this
        project allows: **additive** changes (new tables, new nullable or
        server-defaulted columns), plus the one provably-safe key *widening*
        below (the orders composite key). Anything destructive — drops,
        renames, type changes, NOT NULL without a server default — is refused
        loudly and means it is time to adopt real migrations (Alembic).
        """
        async with self._engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.run_sync(_add_missing_columns)
            await connection.run_sync(_widen_orders_primary_key)

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
