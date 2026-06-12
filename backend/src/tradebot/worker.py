"""The bot worker: composes the paper trading process (ARCHITECTURE.md 7.1).

This is the always-on Railway service — exactly one replica, never scaled
horizontally (CLAUDE.md invariant 8). It wires config -> Postgres -> live
market data feed -> event bus -> trading engine with the paper adapter, and
restores portfolio state by replaying the fill journal on startup, so a
deploy restart resumes exactly where the books left off.

Only paper mode runs here today. Live mode raises on purpose: going live is
an explicit Phase 3 milestone behind its own adapter, reconciliation, and
fault-injection coverage — not a config flip away from an unfinished path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from tradebot.authorization import ProposalQueue
from tradebot.backtest.parity import DivergenceReport
from tradebot.competition import (
    LINEUP,
    PRODUCTION_BOT_ID,
    CompetitorSpec,
    build_challenger_strategy,
    build_rules_strategy,
    build_scenario_strategy,
    describe_rules,
    slugify_bot_label,
    spec_for,
    validate_rules,
)
from tradebot.competition.lineup import ScopedSignalStrategy
from tradebot.core.config import AppConfig, TradingMode, validate_symbol_quote
from tradebot.core.events import CandleClosed, EventBus
from tradebot.core.logging import configure_logging, log_event
from tradebot.core.metrics import MetricsCollector
from tradebot.core.models import AutonomyMode, CandleInterval, Fill, SymbolFilters
from tradebot.engine import TradingEngine
from tradebot.evaluation import ScenarioEvaluator
from tradebot.evaluation.improve import AutoImprover
from tradebot.evaluation.runner import EvaluationManager, EvaluationRunConfig, EvaluationRunner
from tradebot.evaluation.strategy import build_traded_strategy
from tradebot.evaluation.sweep import (
    STRATEGY_FAMILIES,
    SweepCandidate,
    SweepConfig,
    SweepManager,
    SweepRunner,
    build_candidate_strategy,
    validate_family_params,
)
from tradebot.execution import FeeSchedule, FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.marketdata.live_feed import LiveMarketDataFeed, OhlcvExchange
from tradebot.news import CryptoPanicSource, EventCalendar, NewsFlags, NewsGate, NewsMonitor
from tradebot.persistence import (
    CandleStore,
    CoinStore,
    CustomBotStore,
    Database,
    DecisionStore,
    EvaluationStore,
    FillStore,
    OrderStore,
    RiskStateStore,
    StrategySettingsStore,
    TradingFeesStore,
)
from tradebot.portfolio import Portfolio
from tradebot.risk import BreakerState, ManagedStop, RiskConfig, RiskManager
from tradebot.signals import (
    DataHealthGate,
    EntryGate,
    FeedHealth,
    MarketRegimeDetector,
    MarketSentiment,
    RegimeGate,
    SentimentConfig,
    SentimentMonitor,
)
from tradebot.strategies import (
    MeanReversionConfig,
    MeanReversionStrategy,
    RegimeStrategyRouter,
    Strategy,
    TrendFollowingConfig,
    TrendFollowingStrategy,
)

if TYPE_CHECKING:  # runtime import stays local to its start method
    from tradebot.notify import TelegramNotifier

logger = logging.getLogger(__name__)

PRODUCTION_FAMILIES = frozenset({"trend_following", "mean_reversion"})
"""Families the worker's router actually trades. Sweeps may grade more
(e.g. ``breakout``); routing a new family is an explicit architecture
decision, never a side effect of a promotion."""

STRATEGY_PRIME_CANDLES = 1000
"""Stored 1m candles fed through a freshly promoted strategy before it
goes live: comfortably past the slowest indicator's warm-up (a 90-period
EMA needs ~450), so a promotion never trades on half-formed indicators."""


def filters_from_market(market: Mapping[str, Any]) -> SymbolFilters:
    """Translate one ccxt market entry into venue filters.

    ccxt unifies ``limits`` (min amount/cost) across exchanges but leaves
    ``precision`` mode-dependent: an int means decimal places, a fraction
    means the tick/step itself. Missing or malformed fields degrade to 0
    (unconstrained) — paper trading must not crash on a sparse catalog,
    and an unconstrained filter is exactly the pre-filters behavior.
    """

    def as_step(value: object) -> Decimal:
        if value is None or isinstance(value, bool):
            return Decimal(0)
        if isinstance(value, int):
            return Decimal(1).scaleb(-value) if value >= 0 else Decimal(0)
        try:
            step = Decimal(str(value))
        except ArithmeticError:
            return Decimal(0)
        return step if step > 0 else Decimal(0)

    def as_minimum(value: object) -> Decimal:
        if value is None or isinstance(value, bool):
            return Decimal(0)
        try:
            minimum = Decimal(str(value))
        except ArithmeticError:
            return Decimal(0)
        return minimum if minimum > 0 else Decimal(0)

    precision = market.get("precision") or {}
    limits = market.get("limits") or {}
    amount_limits = limits.get("amount") or {}
    cost_limits = limits.get("cost") or {}
    return SymbolFilters(
        price_tick_quote=as_step(precision.get("price")),
        quantity_step_base=as_step(precision.get("amount")),
        min_quantity_base=as_minimum(amount_limits.get("min")),
        min_notional_quote=as_minimum(cost_limits.get("min")),
    )


class TradingVenue(OhlcvExchange, Protocol):
    """The worker's view of the exchange: OHLCV plus the market catalog."""

    async def load_markets(self) -> Mapping[str, object]:
        """Return the exchange's market catalog keyed by unified symbol."""
        ...


@dataclass
class CompetitorRuntime:
    """One challenger's complete paper account inside the worker.

    Mirrors the production bot's wiring one-for-one — own portfolio, own
    risk manager (account-level brakes must judge each account's equity,
    not the incumbent's), own journal-scoped stores, one engine per
    symbol — so the only difference between competitors is the strategy.
    """

    spec: CompetitorSpec
    portfolio: Portfolio
    risk_manager: RiskManager
    fill_store: FillStore
    order_store: OrderStore
    decision_store: DecisionStore
    risk_state_store: RiskStateStore
    rules: dict[str, Any] | None = None
    """A custom bot's validated recipe; ``None`` for built-in lineup entries
    (which trade their family with the active promoted parameters)."""

    engines: dict[str, TradingEngine] = field(default_factory=dict)
    # Where each symbol's boot gap-replay starts for this account: the
    # earliest restored open order's decision time.
    gap_replay_from: dict[str, datetime] = field(default_factory=dict)
    saved_risk_state: tuple[BreakerState, tuple[str, ...]] | None = None


class Worker:
    """N symbols, one strategy each, one account, paper fills — the Phase 2 bot.

    Coins live in Postgres (``coins`` table) and can be added or removed at
    runtime through the control API. ``TRADEBOT_SYMBOLS`` only seeds the
    table on first boot — afterwards the table is the source of truth, so a
    coin removed via the API stays removed across deploys.
    """

    def __init__(self, config: AppConfig, database: Database, exchange: TradingVenue) -> None:
        """Compose the static components; raises unless ``config.mode`` is paper.

        Coins (engines and feeds) are built in :meth:`initialize`, which
        needs the database.
        """
        if config.mode != TradingMode.PAPER:
            raise NotImplementedError(
                f"worker only supports paper mode for now, got {config.mode}; "
                "live trading is a Phase 3 milestone with its own adapter"
            )
        self.config = config
        self.bus = EventBus()
        self.candle_store = CandleStore(database)
        self.fill_store = FillStore(database)
        self.decision_store = DecisionStore(database)
        self.order_store = OrderStore(database)
        self.risk_state_store = RiskStateStore(database)
        # Where each symbol's boot gap-replay starts: the earliest restored
        # open order's decision time, set during replay_journal.
        self._gap_replay_from: dict[str, datetime] = {}
        self._saved_risk_state: tuple[BreakerState, tuple[str, ...]] | None = None
        self.coin_store = CoinStore(database)
        self.evaluation_store = EvaluationStore(database)
        self.strategy_settings_store = StrategySettingsStore(database)
        # One mutable fee schedule shared by every paper engine's simulator,
        # so an operator fee change takes effect on the next fill without
        # rebuilding engines. Seeded from config; the persisted value (if an
        # operator has saved one) overrides it in initialize().
        self.trading_fees_store = TradingFeesStore(database)
        self.fee_schedule = FeeSchedule(
            buy_fee_bps=config.buy_fee_bps, sell_fee_bps=config.sell_fee_bps
        )
        # The active (possibly auto-promoted) parameters per strategy
        # family; loaded from Postgres in initialize(), empty means
        # defaults. Engines, scenarios, and sweeps all read this one dict
        # so research always grades what the bot actually trades.
        self.strategy_params: dict[str, dict[str, Any]] = {}
        self.portfolio = Portfolio(config.paper_initial_balance_quote)
        # Per-symbol venue rules, filled from the exchange's market catalog
        # in initialize()/add_coin and read live by the risk manager.
        self.symbol_filters: dict[str, SymbolFilters] = {}
        # One risk manager for all symbols: the circuit breakers and equity
        # caps are account-level and must see every position through one
        # pair of eyes (engines and feeds are per-symbol).
        self.risk_manager = RiskManager(RiskConfig(), self.portfolio, self.symbol_filters)
        self.engines: dict[str, TradingEngine] = {}
        self._database = database
        self.custom_bot_store = CustomBotStore(database)
        # The strategy competition's challenger accounts (ARCHITECTURE.md
        # §13): built-ins trade one family solo, custom bots trade their
        # user-built recipe, each from its own paper balance and journal
        # scope. The production bot competes as itself. Custom bots load
        # from Postgres in initialize().
        self.challengers: dict[str, CompetitorRuntime] = {}
        if config.competition_enabled:
            for spec in LINEUP:
                if spec.bot_id == PRODUCTION_BOT_ID:
                    continue
                self.challengers[spec.bot_id] = self._new_runtime(spec)
        self._feeds: dict[str, LiveMarketDataFeed] = {}
        self._feed_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_group: asyncio.TaskGroup | None = None
        self._stop_requested = asyncio.Event()
        # Tripped when a safety-critical background service (the control plane)
        # dies: a supervisor inside the run TaskGroup reacts by muting the
        # production bot's entries — see :meth:`_pause_for_failed_service`.
        self._safety_pause_requested = asyncio.Event()
        self._safety_pause_reason: str | None = None
        self._exchange = exchange
        self.evaluations = EvaluationManager(
            EvaluationRunner(
                self.candle_store,
                self.evaluation_store,
                # Bound method, resolved per run: a run grades whichever
                # lineup entry its config names, with the wiring and active
                # parameters as they are right then — production's shape
                # (router with the regime gate on, bare trend without) is
                # just the default entry, never a snapshot from boot.
                self._scenario_evaluator_for,
            ),
            self.evaluation_store,
            code_version=os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown"),
            spawn=self._spawn_background,
        )
        self.sweeps = SweepManager(
            SweepRunner(self.candle_store, self.evaluation_store, build_candidate_strategy),
            self.evaluation_store,
            spawn=self._spawn_background,
        )
        # Built here, validated against the coin set in initialize(): a gate
        # whose reference market the bot does not stream would block every
        # entry forever on stale data.
        self.regime_detector: MarketRegimeDetector | None = (
            MarketRegimeDetector(config.regime_reference_symbol)
            if config.regime_gate_enabled
            else None
        )
        # Set only when a configured gate had to be switched off at startup
        # because its reference market is not traded — surfaced in /status so
        # "entries run ungated" is visible, not silent.
        self.regime_disabled_reason: str | None = None
        # News state is always built: the gate is a pass-through with no
        # flags and an empty calendar, and event awareness must not depend
        # on remembering to enable it.
        self.news_flags = NewsFlags(ttl=timedelta(hours=config.news_flag_ttl_hours))
        self.news_calendar = EventCalendar.from_json(config.event_calendar_json)
        # Sentiment can only tighten the regime gate, so it exists only
        # alongside it; without the gate there is nothing for it to tighten.
        self.sentiment: MarketSentiment | None = (
            MarketSentiment(
                SentimentConfig(
                    extreme_fear_at_or_below=config.sentiment_extreme_fear_at_or_below,
                    extreme_greed_at_or_above=config.sentiment_extreme_greed_at_or_above,
                )
            )
            if config.sentiment_enabled and self.regime_detector is not None
            else None
        )
        self.metrics = MetricsCollector()
        self.metrics.attach_to(self.bus)
        self._notifier: TelegramNotifier | None = None
        # Validated at boot, not first cycle: a bad timeframe must fail the
        # deploy, not the first improvement attempt half a day later.
        CandleInterval(config.auto_improve_timeframe)

    def _new_runtime(
        self, spec: CompetitorSpec, rules: dict[str, Any] | None = None
    ) -> CompetitorRuntime:
        """One challenger account: own portfolio, risk manager, scoped stores."""
        portfolio = Portfolio(self.config.paper_initial_balance_quote)
        return CompetitorRuntime(
            spec=spec,
            portfolio=portfolio,
            risk_manager=RiskManager(RiskConfig(), portfolio, self.symbol_filters),
            fill_store=FillStore(self._database, bot_id=spec.bot_id),
            order_store=OrderStore(self._database, bot_id=spec.bot_id),
            decision_store=DecisionStore(self._database, bot_id=spec.bot_id),
            risk_state_store=RiskStateStore(self._database, row_id=spec.risk_state_row_id),
            rules=rules,
        )

    def _spawn_background(self, coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run a coroutine under the worker's TaskGroup (shutdown cancels it)."""
        if self._task_group is None:
            raise RuntimeError("worker is not running; background tasks need its TaskGroup")
        return self._task_group.create_task(coroutine)

    async def start_evaluation(self, config: EvaluationRunConfig) -> int:
        """Start a blind walk-forward evaluation run (one at a time)."""
        return await self.evaluations.start(config)

    async def start_comparison(self, config: EvaluationRunConfig) -> list[int]:
        """Grade the whole competition lineup on identical scenarios.

        One run per lineup entry, sequential, sharing a frozen window and
        seed — the research counterpart of the live leaderboard. Returns
        the run ids in lineup order.
        """
        return await self.evaluations.start_comparison(config, [spec.bot_id for spec in LINEUP])

    def cancel_evaluation(self, run_id: int) -> bool:
        """Cancel the in-flight evaluation run, if it is this one."""
        return self.evaluations.cancel(run_id)

    async def start_sweep(self, config: SweepConfig) -> int:
        """Start a walk-forward parameter sweep (one at a time)."""
        return await self.sweeps.start(config)

    def cancel_sweep(self, sweep_id: int) -> bool:
        """Cancel the in-flight sweep, if it is this one."""
        return self.sweeps.cancel(sweep_id)

    @property
    def symbols(self) -> tuple[str, ...]:
        """The actively traded pairs, in the order they were added."""
        return tuple(self.engines)

    def feed_health(self, symbol: str) -> LiveMarketDataFeed | None:
        """Return the symbol's market-data feed (its health latch), or ``None``.

        The feed satisfies the :class:`FeedHealth` contract the control plane
        reads for /status; ``None`` before the coin is activated.
        """
        return self._feeds.get(symbol)

    def all_engines(self) -> Iterator[TradingEngine]:
        """Every engine across every competition account.

        Operator commands (pause/resume/kill) act through this: "I halted
        the bot" must mean every account, not "except the challengers".
        """
        yield from self.engines.values()
        for runtime in self.challengers.values():
            yield from runtime.engines.values()

    def trading_fees(self) -> dict[str, Decimal]:
        """Return the active per-side trading fees in basis points, for the API/UI."""
        return {
            "buy_fee_bps": self.fee_schedule.buy_fee_bps,
            "sell_fee_bps": self.fee_schedule.sell_fee_bps,
        }

    async def update_trading_fees(self, *, buy_fee_bps: Decimal, sell_fee_bps: Decimal) -> None:
        """Set both trading fees and persist them; takes effect on the next fill.

        The shared schedule is mutated in place, so every running engine's
        simulator charges the new fee from its next fill — no restart, no
        engine rebuild. Validation (finite, non-negative, sane upper bound)
        lives in :meth:`FeeSchedule.update` and raises ``ValueError`` on bad
        input. If persistence fails the in-memory schedule is rolled back, so
        the running fee never diverges from what a restart would reload.
        """
        previous_buy = self.fee_schedule.buy_fee_bps
        previous_sell = self.fee_schedule.sell_fee_bps
        self.fee_schedule.update(buy_fee_bps=buy_fee_bps, sell_fee_bps=sell_fee_bps)
        try:
            await self.trading_fees_store.save(buy_fee_bps, sell_fee_bps, datetime.now(UTC))
        except Exception:
            self.fee_schedule.update(buy_fee_bps=previous_buy, sell_fee_bps=previous_sell)
            raise
        log_event(
            logger,
            logging.INFO,
            "trading_fees_updated",
            buy_fee_bps=str(buy_fee_bps),
            sell_fee_bps=str(sell_fee_bps),
        )

    async def competition_snapshot(self) -> list[dict[str, Any]]:
        """One leaderboard row per competitor, best equity first.

        Marks come from the newest stored 1m closes, gathered once and
        applied to every account so no competitor is priced at a different
        moment. A bot holding a coin with no stored candle yet reports
        equity ``None`` (unknown beats wrong); rows with unknown equity
        rank last. Amounts are ``Decimal`` end to end — the API boundary
        stringifies them.
        """
        marks: dict[str, Decimal] = {}
        for symbol in self.symbols:
            candle = await self.candle_store.latest_candle(symbol, CandleInterval.M1)
            if candle is not None:
                marks[symbol] = candle.close_quote
        initial = self.config.paper_initial_balance_quote
        rows: list[dict[str, Any]] = []
        production_spec = spec_for(PRODUCTION_BOT_ID)
        accounts: list[
            tuple[
                CompetitorSpec, Portfolio, FillStore, RiskManager, Mapping[str, TradingEngine], str
            ]
        ] = [
            (
                production_spec,
                self.portfolio,
                self.fill_store,
                self.risk_manager,
                self.engines,
                "production",
            )
        ]
        for runtime in self.challengers.values():
            accounts.append(
                (
                    runtime.spec,
                    runtime.portfolio,
                    runtime.fill_store,
                    runtime.risk_manager,
                    runtime.engines,
                    "custom" if runtime.rules is not None else "builtin",
                )
            )
        for spec, portfolio, fill_store, risk_manager, engines, kind in accounts:
            positions = portfolio.positions
            unrealized = Decimal(0)
            all_marked = True
            for symbol, position in positions.items():
                mark = marks.get(symbol)
                if mark is None:
                    all_marked = False
                    break
                unrealized += position.unrealized_pnl_quote(mark)
            equity = portfolio.equity_quote(marks) if all_marked else None
            fill_counts = await fill_store.count_by_side()
            rows.append(
                {
                    "bot_id": spec.bot_id,
                    "label": spec.label,
                    "description": spec.description,
                    "is_production": kind == "production",
                    "kind": kind,
                    # Paused means muted: a bot whose every engine is paused
                    # proposes nothing (protective stops still run).
                    "paused": bool(engines) and all(engine.paused for engine in engines.values()),
                    "equity_quote": equity,
                    "initial_balance_quote": initial,
                    "return_fraction": (
                        (equity - initial) / initial if equity is not None and initial > 0 else None
                    ),
                    "quote_balance": portfolio.quote_balance,
                    "realized_pnl_quote": portfolio.realized_pnl_quote(),
                    "unrealized_pnl_quote": unrealized if all_marked else None,
                    "open_positions": len(positions),
                    "entry_fills": fill_counts.get("buy", 0),
                    "exit_fills": fill_counts.get("sell", 0),
                    "breaker_tripped_reason": risk_manager.breakers.tripped_reason,
                }
            )
        rows.sort(key=lambda row: (row["equity_quote"] is None, -(row["equity_quote"] or 0)))
        return rows

    def _runtime_for(self, bot_id: str) -> CompetitorRuntime:
        """Return ``bot_id``'s challenger account; ``KeyError`` if unknown."""
        runtime = self.challengers.get(bot_id)
        if runtime is None:
            raise KeyError(f"no competition bot {bot_id!r}")
        return runtime

    def _bot_engines(self, bot_id: str) -> Mapping[str, TradingEngine]:
        """One bot's engines, production included; ``KeyError`` if unknown."""
        if bot_id == PRODUCTION_BOT_ID:
            return self.engines
        return self._runtime_for(bot_id).engines

    def fill_store_for(self, bot_id: str) -> FillStore:
        """One bot's fill journal view; ``KeyError`` if unknown."""
        if bot_id == PRODUCTION_BOT_ID:
            return self.fill_store
        return self._runtime_for(bot_id).fill_store

    def decision_store_for(self, bot_id: str) -> DecisionStore:
        """One bot's decision trail view; ``KeyError`` if unknown."""
        if bot_id == PRODUCTION_BOT_ID:
            return self.decision_store
        return self._runtime_for(bot_id).decision_store

    def _effective_family_params(self, family: str) -> dict[str, Any]:
        """Return the complete parameter set ``family`` trades right now.

        Active promoted overrides merged over the config model's defaults,
        so the bot detail page shows exactly what will trade — never a
        partial diff the reader has to mentally merge.
        """
        config_model, _ = STRATEGY_FAMILIES[family]
        params: dict[str, Any] = config_model(**self.strategy_params.get(family, {})).model_dump(
            mode="json"
        )
        return params

    async def pause_bot(self, bot_id: str) -> None:
        """Mute one bot's entries (its protective stops keep running)."""
        for engine in self._bot_engines(bot_id).values():
            engine.pause()
        await self.persist_risk_state()
        log_event(logger, logging.INFO, "bot_paused", bot_id=bot_id)

    async def resume_bot(self, bot_id: str) -> None:
        """Un-mute one bot's entries."""
        for engine in self._bot_engines(bot_id).values():
            engine.resume()
        await self.persist_risk_state()
        log_event(logger, logging.INFO, "bot_resumed", bot_id=bot_id)

    def _trip_safety_pause(self, service: str) -> None:
        """Signal the safety supervisor that ``service`` died (idempotent, sync).

        Called from a task done-callback, which cannot await — it only sets
        the event the supervisor in :meth:`run`'s TaskGroup is waiting on, so
        the actual pause runs on the event loop, never inside the callback.
        The first trip wins: a later crash must not overwrite the reason a
        human will read in the alert.
        """
        if self._safety_pause_requested.is_set():
            return
        self._safety_pause_reason = service
        self._safety_pause_requested.set()

    async def _run_safety_supervisor(self) -> None:
        """Pause trading once a safety-critical service trips the latch.

        Runs inside the run TaskGroup, which only closes when every task is
        done — so this must also return on a normal stop, not just on a trip,
        or shutdown would hang forever waiting for a crash that never comes.
        One-shot: once entries are muted there is nothing more to do until a
        human restarts.
        """
        waiters = [
            asyncio.ensure_future(self._safety_pause_requested.wait()),
            asyncio.ensure_future(self._stop_requested.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        if self._safety_pause_requested.is_set():
            await self._pause_for_failed_service(self._safety_pause_reason or "unknown")

    async def _pause_for_failed_service(self, service: str) -> None:
        """Flatten-safe halt when a safety-critical background service dies.

        Mutes the production bot's entries — its protective stops and exits
        keep running, so capital is never trapped — and raises a loud alert.
        Deliberately does not stop the worker: a dead control plane means the
        operator is blind to new risk, so the safe response is to take none
        until they restart and resume, not to abandon open positions to an
        unattended shutdown. The pause is persisted, so it survives the
        restart and waits for an explicit human resume.
        """
        log_event(logger, logging.ERROR, "safety_pause_tripped", service=service)
        try:
            await self.pause_bot(PRODUCTION_BOT_ID)
        except Exception:
            # The in-memory mute already took effect (pause_bot mutes before it
            # persists); a failed persist must still raise the alert, not swallow
            # the one event the operator needs to see.
            log_event(
                logger, logging.ERROR, "safety_pause_persist_failed", service=service, exc_info=True
            )
        try:
            await self._notify(
                f"⚠️ tradebot paused: the {service} service crashed; new entries are "
                "halted (protective stops and exits still run). Restart the worker and "
                "resume the bot once the control plane is back."
            )
        except Exception:
            # This runs in the run TaskGroup's supervisor: a failed alert (a
            # slow or down Telegram) must not crash the group and take the
            # worker — already-tripped-and-muted — down with it.
            log_event(
                logger, logging.ERROR, "safety_pause_alert_failed", service=service, exc_info=True
            )

    async def kill_bot(self, bot_id: str) -> tuple[int, list[str]]:
        """Halt one bot and flatten its positions at market.

        Returns (exit orders submitted, failure reasons). Same semantics
        as the account-wide kill switch, scoped to one account: every
        engine is halted before any failure is reported.
        """
        exits_submitted = 0
        failures: list[str] = []
        for engine in self._bot_engines(bot_id).values():
            try:
                if await engine.kill():
                    exits_submitted += 1
            except RuntimeError as error:
                failures.append(str(error))
        await self.persist_risk_state()
        log_event(
            logger,
            logging.WARNING,
            "bot_killed",
            bot_id=bot_id,
            exits_submitted=exits_submitted,
            failure_count=len(failures),
        )
        return exits_submitted, failures

    async def create_custom_bot(
        self, label: str, description: str, rules: Mapping[str, Any]
    ) -> str:
        """Build, persist, and start a user-defined bot; returns its id.

        Raises ``ValueError`` for a bad recipe or a name collision and
        ``RuntimeError`` when the competition is disabled. The strategy is
        primed from stored candles before its engines attach, so a bot
        created mid-stream starts with warm indicators instead of trading
        blind for its first hour.
        """
        if not self.config.competition_enabled:
            raise RuntimeError(
                "the competition is disabled (TRADEBOT_COMPETITION_ENABLED=false); "
                "custom bots need it on"
            )
        normalized = validate_rules(rules)
        bot_id = slugify_bot_label(label)
        if bot_id in self.challengers or any(spec.bot_id == bot_id for spec in LINEUP):
            raise ValueError(f"a bot named {label.strip()!r} already exists")
        final_description = description.strip() or describe_rules(normalized)
        risk_row = await self.custom_bot_store.create(
            bot_id, label.strip(), final_description, normalized, datetime.now(UTC)
        )
        spec = CompetitorSpec(
            bot_id=bot_id,
            label=label.strip(),
            family=None,
            risk_state_row_id=risk_row,
            description=final_description,
        )
        runtime = self._new_runtime(spec, rules=normalized)
        self.challengers[bot_id] = runtime
        for symbol in self.symbols:
            strategy = self._challenger_strategy(runtime)
            stored = await self.candle_store.fetch_recent(
                symbol, CandleInterval.M1, STRATEGY_PRIME_CANDLES
            )
            for candle in stored:
                strategy.on_candle(candle, None)
            self._activate_challenger_engine(runtime, symbol, strategy=strategy)
        log_event(
            logger,
            logging.INFO,
            "custom_bot_created",
            bot_id=bot_id,
            description=final_description,
        )
        return bot_id

    async def update_custom_bot(self, bot_id: str, rules: Mapping[str, Any]) -> None:
        """Replace a custom bot's recipe and hot-swap its strategies.

        Position, orders, and risk state are untouched — exactly like a
        parameter promotion. ``ValueError`` for built-ins (their parameters
        come from research promotions, not hand edits), ``KeyError`` for an
        unknown bot.
        """
        runtime = self._runtime_for(bot_id)
        if runtime.rules is None:
            raise ValueError(
                f"{bot_id} is a built-in bot; its parameters come from research "
                "promotions and cannot be edited here"
            )
        normalized = validate_rules(rules)
        await self.custom_bot_store.update_rules(bot_id, normalized)
        runtime.rules = normalized
        for symbol, engine in runtime.engines.items():
            strategy = self._challenger_strategy(runtime)
            stored = await self.candle_store.fetch_recent(
                symbol, CandleInterval.M1, STRATEGY_PRIME_CANDLES
            )
            for candle in stored:
                strategy.on_candle(candle, None)
            engine.replace_strategy(strategy)
        log_event(logger, logging.INFO, "custom_bot_rules_updated", bot_id=bot_id)

    async def delete_custom_bot(self, bot_id: str) -> None:
        """Retire a custom bot; its journals stay queryable forever.

        Refuses while the bot holds a position or a resting order — stop
        the bot first (kill flattens at market on the next candle), then
        delete. ``ValueError`` for built-ins, ``KeyError`` for unknown.
        """
        runtime = self._runtime_for(bot_id)
        if runtime.rules is None:
            raise ValueError(f"{bot_id} is a built-in lineup bot and cannot be deleted")
        for symbol, engine in runtime.engines.items():
            if runtime.portfolio.position(symbol) is not None:
                raise RuntimeError(
                    f"{bot_id} holds a position in {symbol}; stop the bot first "
                    "(its exit fills on the next candle), then delete"
                )
            if engine.open_orders():
                raise RuntimeError(
                    f"{bot_id} has open orders in {symbol}; stop the bot first, then delete"
                )
        for engine in runtime.engines.values():
            engine.detach_from(self.bus)
        del self.challengers[bot_id]
        await self.custom_bot_store.delete(bot_id)
        log_event(logger, logging.INFO, "custom_bot_deleted", bot_id=bot_id)

    async def bot_detail(self, bot_id: str) -> dict[str, Any]:
        """Everything the bot detail page needs, in one shape.

        The leaderboard row plus open positions (marked at the newest
        stored closes) and a strategy descriptor saying exactly what the
        bot trades. ``KeyError`` for an unknown bot.
        """
        rows = await self.competition_snapshot()
        row = next((entry for entry in rows if entry["bot_id"] == bot_id), None)
        if row is None:
            raise KeyError(f"no competition bot {bot_id!r}")
        if bot_id == PRODUCTION_BOT_ID:
            portfolio = self.portfolio
            strategy_info: dict[str, Any] = {
                "kind": "production",
                "regime_routed": self.regime_detector is not None,
                "families": {
                    family: self._effective_family_params(family)
                    for family in sorted(PRODUCTION_FAMILIES)
                },
            }
        else:
            runtime = self._runtime_for(bot_id)
            portfolio = runtime.portfolio
            if runtime.rules is not None:
                strategy_info = {"kind": "custom", "rules": runtime.rules}
            else:
                family = runtime.spec.family
                assert family is not None  # built-ins always carry one
                strategy_info = {
                    "kind": "builtin",
                    "family": family,
                    "params": self._effective_family_params(family),
                }
        positions: list[dict[str, Any]] = []
        for symbol, position in portfolio.positions.items():
            candle = await self.candle_store.latest_candle(symbol, CandleInterval.M1)
            mark = candle.close_quote if candle is not None else None
            positions.append(
                {
                    "symbol": symbol,
                    "quantity_base": position.quantity_base,
                    "average_entry_price_quote": position.average_entry_price_quote,
                    "mark_price_quote": mark,
                    "unrealized_pnl_quote": (
                        position.unrealized_pnl_quote(mark) if mark is not None else None
                    ),
                }
            )
        return {**row, "positions": positions, "strategy": strategy_info}

    async def apply_strategy_params(
        self,
        family: str,
        params: Mapping[str, Any],
        source_sweep_id: int | None = None,
        note: str | None = None,
    ) -> int:
        """Promote ``params`` as ``family``'s active configuration (§12.7).

        Persists a new settings version (lineage first — a promotion that
        crashed mid-swap must still be visible in the journal), then hot-
        swaps every engine's strategy with a fresh instance primed from
        stored candles, so indicators are warm from the first live candle.
        Returns the new version id. Raises ``ValueError`` for an unknown
        family or parameter.
        """
        validate_family_params(family, dict(params))
        if family not in PRODUCTION_FAMILIES:
            # Sweepable but not yet routed: silently "promoting" parameters
            # the router never trades would be a lie in the journal.
            raise ValueError(
                f"{family} is research-only: it has no production route yet, so its "
                "parameters cannot be promoted (sweep it via the research API instead)"
            )
        version = await self.strategy_settings_store.record(
            family, params, datetime.now(UTC), source_sweep_id, note
        )
        self.strategy_params[family] = dict(params)
        await self._rebuild_strategies()
        log_event(
            logger,
            logging.INFO,
            "strategy_settings_active",
            version=version,
            strategy_name=family,
            params=dict(params),
            source_sweep_id=source_sweep_id,
        )
        return version

    async def revert_strategy_version(self, version_id: int) -> int:
        """Re-apply a historical settings version; returns the new version.

        Reverting appends rather than rewrites: the journal keeps the full
        story, including the human override. Raises ``KeyError`` for an
        unknown version id.
        """
        row = await self.strategy_settings_store.fetch(version_id)
        if row is None:
            raise KeyError(f"no strategy settings version {version_id}")
        return await self.apply_strategy_params(
            row["family"],
            row["params"],
            source_sweep_id=row["source_sweep_id"],
            note=f"manual revert to version #{version_id}",
        )

    async def _rebuild_strategies(self) -> None:
        """Swap every engine onto a freshly built, pre-warmed strategy.

        Priming feeds recent stored candles through the new instance (its
        outputs discarded) so the EMAs/RSI/ATR are formed before the swap;
        the swap itself is a single assignment on the event loop, and the
        engine's position, orders, and risk state are untouched.

        Challengers are rebuilt too: each tracks its family's *active*
        parameters, so a promotion improves every account that trades the
        family — the competition compares strategies, not parameter ages.
        """
        for symbol, engine in self.engines.items():
            strategy = self._build_strategy()
            stored = await self.candle_store.fetch_recent(
                symbol, CandleInterval.M1, STRATEGY_PRIME_CANDLES
            )
            for candle in stored:
                strategy.on_candle(candle, None)
            engine.replace_strategy(strategy)
        for runtime in self.challengers.values():
            for symbol, engine in runtime.engines.items():
                strategy = self._challenger_strategy(runtime)
                stored = await self.candle_store.fetch_recent(
                    symbol, CandleInterval.M1, STRATEGY_PRIME_CANDLES
                )
                for candle in stored:
                    strategy.on_candle(candle, None)
                engine.replace_strategy(strategy)

    async def _notify(self, text: str) -> None:
        """Send a Telegram alert when configured; silently skip otherwise."""
        if self._notifier is not None:
            await self._notifier.send(text)

    def _entry_gates(self, feed: FeedHealth) -> tuple[EntryGate, ...]:
        """Build the §5.2 gate chain in pipeline order: data, regime, news.

        The data-health gate leads: a degraded feed (an unrepaired gap)
        means every later gate and the strategy itself are reading suspect
        candles, so entries pause until backfill confirms the data.
        """
        gates: tuple[EntryGate, ...] = (DataHealthGate(feed),)
        if self.regime_detector is not None:
            gates += (RegimeGate(self.regime_detector, self.sentiment),)
        return (*gates, NewsGate(self.news_flags, self.news_calendar))

    def _challenger_entry_gates(self, feed: FeedHealth) -> tuple[EntryGate, ...]:
        """Challenger gates: data-health and news/event veto — no regime gate.

        The regime gate routes families (trend entries in trends,
        mean-reversion in ranges); that routing IS the production router's
        strategy, and applying it to a solo bot would mute the bot for
        every regime its family is "wrong" for — the competition would
        compare gate schedules, not strategies. Degraded data and hard
        news/event windows still veto everyone: those are market facts, not
        strategy.
        """
        return (DataHealthGate(feed), NewsGate(self.news_flags, self.news_calendar))

    def _challenger_strategy(self, runtime: CompetitorRuntime) -> Strategy:
        """One fresh, bot-scoped strategy for a challenger account."""
        if runtime.rules is not None:
            return ScopedSignalStrategy(build_rules_strategy(runtime.rules), runtime.spec.bot_id)
        return build_challenger_strategy(runtime.spec, self.strategy_params)

    def _scenario_evaluator_for(self, strategy_id: str) -> ScenarioEvaluator:
        """One evaluator grading the named lineup entry on fresh instances.

        Scenario strategies self-classify the regime from their own candles
        instead of reading the live detector (whose wall-clock state would
        leak the present into a historical decision); see
        ``tradebot.evaluation.strategy`` for the documented divergence.
        ``ValueError`` for an unknown id — a typo'd run must fail before a
        row exists, never silently grade the wrong strategy.
        """
        spec = spec_for(strategy_id)
        return ScenarioEvaluator(
            lambda: build_scenario_strategy(
                spec,
                self.strategy_params,
                regime_routed=self.regime_detector is not None,
            )
        )

    def _build_strategy(self) -> Strategy:
        """One strategy per coin: regime-routed families when the gate runs.

        With the regime detector on, the router activates trend following
        in trending markets and mean reversion in ranging ones (§5.2);
        without it there is no regime to route by, so the trend family
        trades alone — exactly the pre-router behavior.
        """
        trend = TrendFollowingStrategy(
            TrendFollowingConfig(**self.strategy_params.get("trend_following", {}))
        )
        if self.regime_detector is None:
            return trend
        detector = self.regime_detector
        return RegimeStrategyRouter(
            trend,
            MeanReversionStrategy(
                MeanReversionConfig(**self.strategy_params.get("mean_reversion", {}))
            ),
            regime_label=lambda: detector.regime.label,
        )

    def _activate(self, symbol: str) -> None:
        """Build and wire one coin's engines and feed (does not start the feed).

        One feed per symbol fans out to every competitor's engine through
        the bus — the competition multiplies accounts, never market-data
        connections (the exchange rate budget is shared). The feed is built
        first: every engine's data-health gate reads its health latch, so
        the feed must exist before any engine on this symbol is wired.
        Starting the feed task is the caller's job: at boot ``run`` starts
        every feed inside the TaskGroup, and ``add_coin`` starts the feed
        only after the new coin's strategies have been primed.
        """
        feed = LiveMarketDataFeed(
            self._exchange,
            symbol,
            self.candle_store,
            self.bus,
            history_days=self.config.history_backfill_days,
        )
        self._feeds[symbol] = feed
        engine = TradingEngine(
            self._build_strategy(),
            self.risk_manager,
            self.portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig(), fees=self.fee_schedule),
            symbol=symbol,
            fill_store=self.fill_store,
            decision_store=self.decision_store,
            order_store=self.order_store,
            autonomy_mode=self.config.autonomy_mode,
            proposal_queue=ProposalQueue(
                ttl=timedelta(seconds=self.config.proposal_ttl_seconds),
                max_drift_fraction=self.config.proposal_max_drift_fraction,
            ),
            entry_gates=self._entry_gates(feed),
        )
        engine.attach_to(self.bus)
        for runtime in self.challengers.values():
            self._activate_challenger_engine(runtime, symbol)
        self.engines[symbol] = engine

    async def _prime_symbol(self, symbol: str) -> None:
        """Warm one coin's production and challenger strategies from stored candles.

        Replays up to ``STRATEGY_PRIME_CANDLES`` recent 1m candles through a
        fresh strategy per engine (outputs discarded) and swaps it in, so
        the EMAs/RSI/ATR are formed before the first live candle — the same
        warm-start a parameter promotion gets (see ``_rebuild_strategies``).
        The engines hold no position yet, so the swap only seeds indicator
        state. A no-op when nothing is stored: the strategy then warms from
        the live stream, exactly as on a first-ever boot.
        """
        stored = await self.candle_store.fetch_recent(
            symbol, CandleInterval.M1, STRATEGY_PRIME_CANDLES
        )
        if not stored:
            return
        production = self._build_strategy()
        for candle in stored:
            production.on_candle(candle, None)
        self.engines[symbol].replace_strategy(production)
        for runtime in self.challengers.values():
            engine = runtime.engines.get(symbol)
            if engine is None:
                continue
            strategy = self._challenger_strategy(runtime)
            for candle in stored:
                strategy.on_candle(candle, None)
            engine.replace_strategy(strategy)

    def _activate_challenger_engine(
        self, runtime: CompetitorRuntime, symbol: str, strategy: Strategy | None = None
    ) -> None:
        """Build and wire one challenger's engine for ``symbol``.

        Always autonomous (a challenger exists to show what its strategy
        does unattended; co-pilot approval queues are the operator's bot's
        concern), and gated by data health and news only — see
        :meth:`_challenger_entry_gates` for why the regime gate does not
        apply to solo bots. The symbol's feed already exists: ``_activate``
        builds it before any engine, and runtime bot additions only target
        already-active symbols.
        """
        engine = TradingEngine(
            strategy if strategy is not None else self._challenger_strategy(runtime),
            runtime.risk_manager,
            runtime.portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig(), fees=self.fee_schedule),
            symbol=symbol,
            fill_store=runtime.fill_store,
            decision_store=runtime.decision_store,
            order_store=runtime.order_store,
            autonomy_mode=AutonomyMode.AUTONOMOUS,
            entry_gates=self._challenger_entry_gates(self._feeds[symbol]),
            signal_id_scope=f"{runtime.spec.bot_id}/",
        )
        engine.attach_to(self.bus)
        runtime.engines[symbol] = engine

    async def initialize(self) -> int:
        """Create the schema, load the active coins, and replay the journal.

        Returns the number of fills replayed. Separate from ``__init__``
        because the coin set lives in the database.
        """
        await self._database.create_schema()
        # Operator-set trading fees override the config boot defaults before
        # any engine's simulator captures the shared schedule.
        stored_fees = await self.trading_fees_store.load()
        if stored_fees is not None:
            buy_fee_bps, sell_fee_bps = stored_fees
            self.fee_schedule.update(buy_fee_bps=buy_fee_bps, sell_fee_bps=sell_fee_bps)
            log_event(
                logger,
                logging.INFO,
                "trading_fees_loaded",
                buy_fee_bps=str(buy_fee_bps),
                sell_fee_bps=str(sell_fee_bps),
            )
        # Active strategy parameters come first: every engine built below
        # captures its strategy from this dict.
        self.strategy_params = await self.strategy_settings_store.active()
        if self.strategy_params:
            log_event(
                logger,
                logging.INFO,
                "promoted_strategy_settings_loaded",
                families=", ".join(sorted(self.strategy_params)),
            )
        if await self.coin_store.seed_if_empty(self.config.symbol_list(), datetime.now(UTC)):
            log_event(logger, logging.INFO, "coins_seeded")
        # Custom bots join the lineup before any engine is built, so the
        # activation loop below wires them exactly like built-in challengers.
        if self.config.competition_enabled:
            for row in await self.custom_bot_store.list_all():
                spec = CompetitorSpec(
                    bot_id=row["bot_id"],
                    label=row["label"],
                    family=None,
                    risk_state_row_id=row["risk_state_row_id"],
                    description=row["description"],
                )
                self.challengers[spec.bot_id] = self._new_runtime(spec, rules=dict(row["rules"]))
            custom_count = sum(1 for r in self.challengers.values() if r.rules is not None)
            if custom_count:
                log_event(logger, logging.INFO, "custom_bots_loaded", custom_count=custom_count)
        symbols = await self.coin_store.list_symbols()
        try:
            markets = await self._exchange.load_markets()
        except Exception:
            # Venue rules tighten realism; their absence must not stop the
            # bot. Unfiltered sizing is exactly the pre-catalog behavior.
            log_event(logger, logging.WARNING, "market_catalog_unavailable")
            markets = {}
        for symbol in symbols:
            market = markets.get(symbol)
            if isinstance(market, Mapping):
                self.symbol_filters[symbol] = filters_from_market(market)
        # Resolved before any engine is built: engines capture the gate at
        # activation, and a gate without a data feed would block every entry
        # forever on stale data.
        if self.regime_detector is not None and self.regime_detector.symbol not in symbols:
            reference = self.regime_detector.symbol
            log_event(
                logger,
                logging.WARNING,
                "regime_gate_disabled",
                reference=reference,
                symbols=", ".join(symbols),
            )
            self.regime_disabled_reason = (
                f"reference market {reference} is not among the traded coins; entries run ungated"
            )
            self.regime_detector = None
        for symbol in symbols:
            self._activate(symbol)
        if self.regime_detector is not None:
            # Pull the reference market's history *before* priming: on a
            # truly first boot nothing is stored yet, and priming from an
            # empty table would leave the gate warming up — blocking every
            # entry for days — until a restart re-primed it. Resumable and
            # cheap on later boots (it starts at the newest stored candle),
            # and a failure only defers warm-up to the live stream.
            try:
                repaired = await self._feeds[self.regime_detector.symbol].backfill()
                if repaired:
                    log_event(
                        logger,
                        logging.INFO,
                        "priming_backfill_fetched",
                        count=repaired,
                        symbol=self.regime_detector.symbol,
                    )
            except Exception:
                log_event(
                    logger,
                    logging.WARNING,
                    "reference_market_backfill_failed",
                    exc_info=True,
                )
            # Prime from stored history so the gate does not spend its first
            # day warming up after every deploy.
            stored = await self.candle_store.fetch_recent(
                self.regime_detector.symbol,
                CandleInterval.M1,
                self.regime_detector.config.required_m1_candles(),
            )
            self.regime_detector.prime(stored)
            self.regime_detector.attach_to(self.bus)
            log_event(
                logger,
                logging.INFO,
                "regime_gate_enabled",
                symbol=self.regime_detector.symbol,
                regime_label=self.regime_detector.regime.label,
            )
        await self._restore_risk_state()
        replayed = await self.replay_journal()
        await self._replay_missed_candles()
        await self._rearm_protective_stops()
        # Subscribed after restore on purpose: the persister must never
        # write boot-time defaults over the state it was about to load.
        self.bus.subscribe(CandleClosed, self._persist_risk_state)
        return replayed

    async def add_coin(self, symbol: str) -> None:
        """Start trading ``symbol``: validate, persist, build, warm, stream.

        Raises ``ValueError`` for an invalid, duplicate, or unlisted pair.
        The coin's strategies are primed from a bounded recent-history fetch
        before the live stream starts, so a coin added into a running bot
        trades on warm indicators instead of staying blind for hours while
        the deep history crawl catches up in the background.
        """
        symbol = symbol.strip()
        validate_symbol_quote(symbol, self.config.quote_currency)
        if symbol in self.engines:
            raise ValueError(f"{symbol} is already being traded")
        markets = await self._exchange.load_markets()
        if symbol not in markets:
            raise ValueError(f"{symbol} is not listed on {self.config.exchange_id}")
        market = markets[symbol]
        if isinstance(market, Mapping):
            self.symbol_filters[symbol] = filters_from_market(market)
        await self.coin_store.add(symbol, datetime.now(UTC))
        self._activate(symbol)
        # Warm the strategies before any live candle reaches them. The recent
        # fetch is bounded (the multi-year crawl in the feed's own backfill
        # would block this call for minutes); a failure degrades to a cold
        # start — the strategies warm from the live stream — loudly.
        feed = self._feeds[symbol]
        try:
            fetched = await feed.prime_history(STRATEGY_PRIME_CANDLES)
            if fetched:
                log_event(
                    logger, logging.INFO, "coin_prime_fetched", symbol=symbol, candles=fetched
                )
        except Exception:
            log_event(
                logger, logging.WARNING, "coin_prime_fetch_failed", symbol=symbol, exc_info=True
            )
        await self._prime_symbol(symbol)
        if self._task_group is not None:
            self._feed_tasks[symbol] = self._task_group.create_task(feed.run())
        log_event(logger, logging.INFO, "coin_added", symbol=symbol)

    async def remove_coin(self, symbol: str) -> None:
        """Stop trading ``symbol``; its candles, fills, and decisions stay.

        Raises ``KeyError`` for an unknown coin and ``RuntimeError`` when
        removal would be unsafe: an open position or pending proposal must
        be dealt with by a human first, and the last coin cannot be removed
        (pause the bot instead — an empty bot looks healthy while doing
        nothing, which is a trap).
        """
        engine = self.engines.get(symbol)
        if engine is None:
            raise KeyError(f"{symbol} is not being traded")
        if len(self.engines) == 1:
            raise RuntimeError("cannot remove the last coin; pause the bot instead")
        if self.regime_detector is not None and symbol == self.regime_detector.symbol:
            # Without its data feed the gate would go stale and block every
            # entry on every coin — refuse, instead of silently strangling
            # the bot.
            raise RuntimeError(
                f"{symbol} is the regime gate's reference market; "
                "disable the gate (TRADEBOT_REGIME_GATE_ENABLED=false) before removing it"
            )
        if self.portfolio.position(symbol) is not None:
            raise RuntimeError(f"{symbol} has an open position; flatten it first (kill or exit)")
        if engine.open_orders():
            # Discarding the engine would orphan its journaled-open orders:
            # nothing could fill or cancel them until a restart restored
            # them into a re-added coin's adapter.
            raise RuntimeError(f"{symbol} has open orders; cancel them first (kill)")
        if engine.pending_proposals():
            raise RuntimeError(f"{symbol} has a pending proposal; approve or reject it first")
        for runtime in self.challengers.values():
            # Challenger accounts are real journals too: detaching an engine
            # that holds a position or a resting order would orphan them just
            # as surely. The kill switch flattens every account.
            if runtime.portfolio.position(symbol) is not None:
                raise RuntimeError(
                    f"{symbol} has an open position in the {runtime.spec.bot_id} "
                    "competition account; flatten it first (kill)"
                )
            challenger_engine = runtime.engines.get(symbol)
            if challenger_engine is not None and challenger_engine.open_orders():
                raise RuntimeError(
                    f"{symbol} has open orders in the {runtime.spec.bot_id} "
                    "competition account; cancel them first (kill)"
                )
        await self.coin_store.remove(symbol)
        self._feeds.pop(symbol).stop()
        task = self._feed_tasks.pop(symbol, None)
        if task is not None:
            # The TaskGroup ignores cancelled children, so removal never
            # tears down the other feeds the way a crash would.
            task.cancel()
        engine.detach_from(self.bus)
        del self.engines[symbol]
        for runtime in self.challengers.values():
            challenger_engine = runtime.engines.pop(symbol, None)
            if challenger_engine is not None:
                challenger_engine.detach_from(self.bus)
        log_event(logger, logging.INFO, "coin_removed", symbol=symbol)

    async def _confirm_promotion(
        self, family: str, params: Mapping[str, Any], symbol: str
    ) -> str | None:
        """Engine-backed confirmation: the last gate before any promotion.

        Sweeps grade candidates with the scenario evaluator's unit trades,
        which deliberately ignore sizing, account limits, and the stop
        lifecycle. Before a validated winner is promoted, it and the
        incumbent are replayed through the production engine over the same
        stored history; a challenger that cannot beat the incumbent where
        it actually has to trade is vetoed — the evaluator validates, the
        engine confirms. Returns the veto reason, or ``None`` to allow.
        Fails safe: nothing to confirm on means no promotion.
        """
        from tradebot.backtest import BacktestRunner

        candles = await self.candle_store.fetch_recent(
            symbol, CandleInterval.M1, self.config.auto_improve_history_days * 1440
        )
        if not candles:
            return f"no stored {symbol} candles to replay"

        async def final_equity(candidate_params: Mapping[str, Any]) -> Decimal:
            portfolio = Portfolio(self.config.paper_initial_balance_quote)
            runner = BacktestRunner(
                build_candidate_strategy(
                    SweepCandidate(name="confirm", family=family, params=dict(candidate_params))
                ),
                RiskManager(RiskConfig(), portfolio),
                portfolio,
                SimulatedExecutionAdapter(FillSimulatorConfig(), fees=self.fee_schedule),
            )
            return (await runner.run(candles)).final_equity_quote

        challenger_equity = await final_equity(params)
        incumbent_equity = await final_equity(self.strategy_params.get(family, {}))
        if challenger_equity < incumbent_equity:
            return (
                f"engine replay over {len(candles)} {symbol} candles: challenger "
                f"final equity {challenger_equity} < incumbent {incumbent_equity}"
            )
        return None

    async def _restore_risk_state(self) -> None:
        """Adopt the persisted brakes and pause flags before trading resumes.

        A deploy must never silently release a tripped breaker, reset the
        daily-loss anchor, forget a cooldown, or resume a killed bot — the
        operator actions those states wait for did not happen just because
        the process restarted.
        """
        loaded = await self.risk_state_store.load()
        if loaded is not None:
            state, paused_symbols = loaded
            self.risk_manager.breakers.restore(state)
            for symbol in paused_symbols:
                engine = self.engines.get(symbol)
                if engine is None:
                    log_event(logger, logging.WARNING, "paused_symbol_dropped", symbol=symbol)
                    continue
                engine.pause()
                log_event(logger, logging.WARNING, "paused_state_restored", symbol=symbol)
            self._saved_risk_state = (state, paused_symbols)
        for runtime in self.challengers.values():
            loaded = await runtime.risk_state_store.load()
            if loaded is None:
                continue
            state, paused_symbols = loaded
            runtime.risk_manager.breakers.restore(state)
            for symbol in paused_symbols:
                engine = runtime.engines.get(symbol)
                if engine is None:
                    log_event(
                        logger,
                        logging.WARNING,
                        "paused_symbol_dropped",
                        symbol=symbol,
                        bot_id=runtime.spec.bot_id,
                    )
                    continue
                engine.pause()
                log_event(
                    logger,
                    logging.WARNING,
                    "paused_state_restored",
                    symbol=symbol,
                    bot_id=runtime.spec.bot_id,
                )
            runtime.saved_risk_state = (state, paused_symbols)

    async def _persist_risk_state(self, event: CandleClosed) -> None:
        """Bus hook: persist the brake/pause snapshot once per closed candle."""
        await self.persist_risk_state()

    async def persist_risk_state(self) -> None:
        """Save the brake/pause snapshot if it changed since the last save.

        Called per closed candle by the bus hook, and synchronously by the
        pause/resume/kill API endpoints — an operator halt must reach
        Postgres before the command returns, not a candle later (a crash
        inside that window would otherwise resume a killed bot). Write
        failures are logged and retried implicitly on the next candle —
        risk persistence must not break trading, only restarts can be
        stale.
        """
        snapshot = self.risk_manager.breakers.snapshot()
        paused = tuple(symbol for symbol, engine in self.engines.items() if engine.paused)
        pending = (snapshot, paused)
        if self._saved_risk_state != pending:
            # Claim before the await: per-symbol feeds publish candles
            # concurrently, and two interleaved handlers must not both write
            # the same snapshot. Reverted on failure so the retry guarantee
            # holds.
            previous = self._saved_risk_state
            self._saved_risk_state = pending
            try:
                await self.risk_state_store.save(snapshot, paused, datetime.now(UTC))
            except Exception:
                self._saved_risk_state = previous
                log_event(logger, logging.ERROR, "risk_state_persist_failed", exc_info=True)
        for runtime in self.challengers.values():
            snapshot = runtime.risk_manager.breakers.snapshot()
            paused = tuple(symbol for symbol, engine in runtime.engines.items() if engine.paused)
            pending = (snapshot, paused)
            if runtime.saved_risk_state == pending:
                continue
            previous = runtime.saved_risk_state
            runtime.saved_risk_state = pending
            try:
                await runtime.risk_state_store.save(snapshot, paused, datetime.now(UTC))
            except Exception:
                runtime.saved_risk_state = previous
                log_event(
                    logger,
                    logging.ERROR,
                    "risk_state_persist_failed",
                    exc_info=True,
                    bot_id=runtime.spec.bot_id,
                )

    async def divergence_report(
        self, symbol: str, window_hours: int = 24, window_end: datetime | None = None
    ) -> DivergenceReport:
        """Compare ``symbol``'s live paper fills against a same-candle replay.

        The §10 paper-gate metric: stored candles for the window (plus the
        standard priming history before it) are replayed through a fresh
        instance of the production strategy shape with the active
        parameters, and the replay's fills are matched against the fills
        the live engine journaled in the same window. The replay portfolio
        is seeded by replaying the symbol's own pre-window fills, so a
        position carried into the window (and the equity its history
        earned) is visible to the replayed strategy and risk checks;
        cross-symbol equity effects remain a documented approximation
        (other coins' candles never reach this replay, so their positions
        cannot be marked). Zero divergence is the expectation — paper and
        backtest are one code path; non-zero is either documented gating
        (regime/news/pause/co-pilot, which the replay deliberately omits)
        or a parity bug. Raises ``KeyError`` for an untraded coin.
        ``window_end`` defaults to now; pass it explicitly for
        reproducible reports.
        """
        from tradebot.backtest import BacktestRunner, compare_fills

        if symbol not in self.engines:
            raise KeyError(f"{symbol} is not being traded")
        if window_end is None:
            window_end = datetime.now(UTC)
        window_start = window_end - timedelta(hours=window_hours)
        prime_start = window_start - timedelta(minutes=STRATEGY_PRIME_CANDLES)
        candles = await self.candle_store.fetch_range(
            symbol, CandleInterval.M1, prime_start, window_end
        )
        symbol_fills = await self.fill_store.fetch_all(symbol)
        replay_fills: tuple[Fill, ...] = ()
        if candles:
            portfolio = Portfolio(self.config.paper_initial_balance_quote)
            for fill in symbol_fills:
                if fill.filled_at < prime_start:
                    portfolio.apply_fill(fill)
            runner = BacktestRunner(
                build_traded_strategy(
                    regime_routed=self.regime_detector is not None,
                    params_by_family=self.strategy_params,
                ),
                RiskManager(RiskConfig(), portfolio, self.symbol_filters),
                portfolio,
                SimulatedExecutionAdapter(FillSimulatorConfig(), fees=self.fee_schedule),
            )
            result = await runner.run(candles)
            # Fills inside the priming prefix exist only to warm state, and
            # the window bound is applied symmetrically on both streams.
            replay_fills = tuple(
                f for f in result.fills if window_start <= f.filled_at < window_end
            )
        live_fills = tuple(
            fill for fill in symbol_fills if window_start <= fill.filled_at < window_end
        )
        return compare_fills(live_fills, replay_fills, window_start, window_end)

    async def _replay_missed_candles(self) -> None:
        """Run downtime candles through restored orders before streaming.

        A restored order's fate was decided by the candles that happened
        while the process was down; evaluating it only against future live
        candles would fill it at the wrong time and price. For each symbol
        with restored orders, the gap is backfilled into Postgres first,
        then every candle from the earliest restored order's decision time
        onward is replayed through the adapter (strategy untouched).
        Re-replaying candles an order already survived is idempotent: the
        simulator is deterministic, and an order that did not fill on a
        candle live will not fill on it now. A failed backfill degrades to
        the old behavior — the order meets the live stream — loudly.
        """
        # One backfill per symbol, however many accounts need its candles.
        pending: dict[str, list[tuple[str, TradingEngine, datetime]]] = {}
        for symbol, replay_from in self._gap_replay_from.items():
            engine = self.engines.get(symbol)
            if engine is not None:  # replay_journal already warned about orphans
                pending.setdefault(symbol, []).append((PRODUCTION_BOT_ID, engine, replay_from))
        for runtime in self.challengers.values():
            for symbol, replay_from in runtime.gap_replay_from.items():
                engine = runtime.engines.get(symbol)
                if engine is not None:
                    pending.setdefault(symbol, []).append(
                        (runtime.spec.bot_id, engine, replay_from)
                    )
        for symbol in sorted(pending):
            feed = self._feeds.get(symbol)
            if feed is None:
                continue
            try:
                await feed.backfill()
            except Exception:
                log_event(
                    logger,
                    logging.WARNING,
                    "gap_backfill_failed",
                    exc_info=True,
                    symbol=symbol,
                )
                continue
            for bot_id, engine, replay_from in pending[symbol]:
                candles = await self.candle_store.fetch_range(
                    symbol, CandleInterval.M1, replay_from, datetime.now(UTC)
                )
                fills_before = len(engine.fills)
                for candle in candles:
                    await engine.replay_gap_candle(candle)
                log_event(
                    logger,
                    logging.INFO,
                    "gap_replay",
                    symbol=symbol,
                    bot_id=bot_id,
                    count=len(candles),
                    replay_from=replay_from.isoformat(),
                    fills=len(engine.fills) - fills_before,
                )
        self._gap_replay_from.clear()
        for runtime in self.challengers.values():
            runtime.gap_replay_from.clear()

    async def _rearm_protective_stops(self) -> None:
        """Rebuild every account's replayed-position protection after a restart."""
        await self._rearm_protective_stops_for(
            PRODUCTION_BOT_ID, self.portfolio, self.order_store, self.engines
        )
        for runtime in self.challengers.values():
            await self._rearm_protective_stops_for(
                runtime.spec.bot_id, runtime.portfolio, runtime.order_store, runtime.engines
            )

    async def _rearm_protective_stops_for(
        self,
        bot_id: str,
        portfolio: Portfolio,
        order_store: OrderStore,
        engines: Mapping[str, TradingEngine],
    ) -> None:
        """Rebuild one account's replayed-position protection after a restart.

        The resting stop order itself is restored from the order journal;
        what a restart loses is the in-memory ratchet (ManagedStop). With
        the entry's persisted exit plan the ratchet is rebuilt exactly —
        seeded with the restored order's trigger so ratchet progress is
        kept — and a missing resting order (crash between the entry fill
        and the stop placement) is resubmitted from the same plan.

        Positions with no journaled plan (history predating the order
        journal) fall back to an approximate ATR-derived level enforced by
        the engine's market-exit backstop; better an approximate stop than
        none. A position that cannot be protected at all is loud, never
        silent.
        """
        from tradebot.indicators import Atr

        trend = TrendFollowingConfig(**self.strategy_params.get("trend_following", {}))
        for symbol, position in portfolio.positions.items():
            engine = engines.get(symbol)
            if engine is None:
                log_event(
                    logger,
                    logging.WARNING,
                    "position_engine_missing",
                    symbol=symbol,
                    bot_id=bot_id,
                )
                continue
            entry = await order_store.latest_filled_entry_with_plan(symbol)
            plan = None if entry is None else entry.protective_exit
            if entry is not None and plan is not None:
                resting = engine.resting_protective_stop()
                level = (
                    plan.stop_price_quote
                    if resting is None or resting.stop_price_quote is None
                    else resting.stop_price_quote
                )
                engine.arm_managed_stop(
                    ManagedStop(
                        entry_price_quote=position.average_entry_price_quote,
                        initial_stop_quote=level,
                        breakeven_at_r=plan.breakeven_at_r,
                        trail_distance_quote=plan.trail_distance_quote,
                    ),
                    entry,
                )
                if resting is None and not engine.has_resting_exit():
                    # The crash window: the entry fill was journaled but its
                    # stop never reached the books.
                    await engine.submit_protective_stop(entry, position.quantity_base)
                log_event(
                    logger,
                    logging.INFO,
                    "protective_stop_rearmed",
                    symbol=symbol,
                    level=level,
                )
                continue
            # Plan-less position: approximate the level from current ATR and
            # let the engine's market-exit backstop enforce it.
            stored = await self.candle_store.fetch_recent(
                symbol, CandleInterval.M1, 5 * trend.atr_period
            )
            atr = Atr(trend.atr_period)
            atr_value: float | None = None
            for candle in stored:
                atr_value = atr.update(
                    float(candle.high_quote), float(candle.low_quote), float(candle.close_quote)
                )
            if atr_value is None:
                log_event(
                    logger,
                    logging.WARNING,
                    "protective_stop_rearm_failed",
                    symbol=symbol,
                    reason="no stored candles to size it",
                )
                continue
            entry_price = position.average_entry_price_quote
            stop_price = entry_price - Decimal(str(trend.atr_stop_multiple * atr_value))
            if stop_price <= 0:
                log_event(
                    logger,
                    logging.WARNING,
                    "protective_stop_rearm_failed",
                    symbol=symbol,
                    reason="degenerate level",
                )
                continue
            engine.arm_managed_stop(
                ManagedStop(
                    entry_price_quote=entry_price,
                    initial_stop_quote=stop_price,
                    breakeven_at_r=trend.breakeven_at_r,
                    trail_distance_quote=(
                        Decimal(str(trend.trail_atr_multiple * atr_value))
                        if trend.trail_atr_multiple > 0
                        else None
                    ),
                )
            )
            log_event(
                logger,
                logging.WARNING,
                "protective_stop_approximated",
                symbol=symbol,
                stop_price=stop_price,
            )

    async def replay_journal(self) -> int:
        """Rebuild portfolio state from persisted fills; returns fills replayed.

        Paper-mode reconciliation: the journal is the source of truth across
        restarts (live mode will reconcile against the exchange instead).
        Replayed history is then rebased out of the loss-streak tracker —
        an old losing streak must not start a new cooldown at boot. Finally,
        orders that were open when the process died are re-armed in their
        engines' adapters, so a restart never silently drops an in-flight
        order.
        """
        fills = await self.fill_store.fetch_all()
        for fill in fills:
            self.portfolio.apply_fill(fill)
        self.risk_manager.rebase_realized_pnl()
        restored = 0
        for open_order in await self.order_store.fetch_open():
            engine = self.engines.get(open_order.order.symbol)
            if engine is None:
                # A coin removed while an order rested (or a journal from an
                # older coin set): nothing can fill it, so leave the row
                # alone and say so instead of guessing.
                log_event(
                    logger,
                    logging.WARNING,
                    "open_order_unrestored",
                    client_order_id=open_order.order.client_order_id,
                    symbol=open_order.order.symbol,
                )
                continue
            engine.restore_order(open_order)
            restored += 1
            symbol = open_order.order.symbol
            created_at = open_order.order.created_at
            if symbol not in self._gap_replay_from or created_at < self._gap_replay_from[symbol]:
                self._gap_replay_from[symbol] = created_at
        for runtime in self.challengers.values():
            challenger_fills = await runtime.fill_store.fetch_all()
            for fill in challenger_fills:
                runtime.portfolio.apply_fill(fill)
            runtime.risk_manager.rebase_realized_pnl()
            for open_order in await runtime.order_store.fetch_open():
                engine = runtime.engines.get(open_order.order.symbol)
                if engine is None:
                    log_event(
                        logger,
                        logging.WARNING,
                        "open_order_unrestored",
                        client_order_id=open_order.order.client_order_id,
                        symbol=open_order.order.symbol,
                        bot_id=runtime.spec.bot_id,
                    )
                    continue
                engine.restore_order(open_order)
                restored += 1
                symbol = open_order.order.symbol
                created_at = open_order.order.created_at
                replay_from = runtime.gap_replay_from.get(symbol)
                if replay_from is None or created_at < replay_from:
                    runtime.gap_replay_from[symbol] = created_at
        if restored:
            log_event(logger, logging.INFO, "open_orders_restored", count=restored)
        return len(fills)

    async def run(self) -> None:
        """Start the bot (and the control API, if configured) until stopped."""
        # Started inside the try so a failure in any later startup step still
        # tears down whatever already runs (no leaked tasks or clients).
        api_task: asyncio.Task[None] | None = None
        improver_task: asyncio.Task[None] | None = None
        notifier_client: httpx.AsyncClient | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        heartbeat_client: httpx.AsyncClient | None = None
        news_task: asyncio.Task[None] | None = None
        news_client: httpx.AsyncClient | None = None
        backup_task: asyncio.Task[None] | None = None
        backup_client: httpx.AsyncClient | None = None
        sentiment_task: asyncio.Task[None] | None = None
        sentiment_client: httpx.AsyncClient | None = None
        try:
            # The API comes up before initialize(): the first boot's deep
            # history backfill can run for minutes, and the platform
            # healthcheck must see /health long before that finishes. Data
            # endpoints answer an honest 409 ("no coins are active") until
            # the engines exist.
            api_task = self._start_api()
            replayed = await self.initialize()
            positions = (
                ", ".join(
                    f"{symbol}={position.quantity_base}"
                    for symbol, position in self.portfolio.positions.items()
                )
                or "flat"
            )
            log_event(
                logger,
                logging.INFO,
                "worker_starting",
                symbols=", ".join(self.symbols),
                exchange_id=self.config.exchange_id,
                fills_replayed=replayed,
                positions=positions,
                balance=self.portfolio.quote_balance,
            )
            notifier_client = await self._start_notifier_if_configured()
            heartbeat_task, heartbeat_client = self._start_heartbeat_if_configured()
            news_task, news_client = self._start_news_monitor_if_configured()
            backup_task, backup_client = self._start_backups_if_configured()
            sentiment_task, sentiment_client = self._start_sentiment_if_configured()
            improver_task = self._start_improver_if_enabled()
            # TaskGroup, not gather: if one feed crashes, the others must be
            # cancelled with it — a bot trading some symbols while blind on
            # another would be worse than one that stops and restarts. The
            # keeper task holds the group open while coins are added and
            # removed at runtime (even down to zero feeds).
            async with asyncio.TaskGroup() as task_group:
                self._task_group = task_group
                task_group.create_task(self._stop_requested.wait())
                # Mutes entries if a safety-critical service (the control
                # plane) crashes; idle for the worker's whole life otherwise.
                task_group.create_task(self._run_safety_supervisor())
                for symbol, feed in self._feeds.items():
                    self._feed_tasks[symbol] = task_group.create_task(feed.run())
        finally:
            self._task_group = None  # late add_coin calls must not target a closed group
            for task in (
                api_task,
                improver_task,
                heartbeat_task,
                news_task,
                backup_task,
                sentiment_task,
            ):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # A task that already crashed re-raises here; the rest
                    # of shutdown (other tasks, client close) must still run.
                    log_event(logger, logging.ERROR, "shutdown_task_failed", exc_info=True)
            for client in (
                notifier_client,
                heartbeat_client,
                news_client,
                backup_client,
                sentiment_client,
            ):
                if client is not None:
                    await client.aclose()
        log_event(logger, logging.INFO, "worker_stopped")

    async def _start_notifier_if_configured(self) -> httpx.AsyncClient | None:
        """Attach Telegram alerts when both token and chat id are set."""
        token = self.config.telegram_bot_token
        chat_id = self.config.telegram_chat_id
        if not token or not chat_id:
            # Truthiness, not None-ness: empty-string env vars must disable
            # alerts gracefully rather than crash the worker at startup.
            log_event(logger, logging.INFO, "telegram_alerts_disabled")
            return None
        from tradebot.notify import TelegramNotifier

        client = httpx.AsyncClient(timeout=10)
        notifier = TelegramNotifier(token, chat_id, client)
        notifier.attach_to(self.bus)
        self._notifier = notifier
        await notifier.send(
            f"tradebot started: {', '.join(self.symbols)} on {self.config.exchange_id} (paper)"
        )
        return client

    def _start_heartbeat_if_configured(
        self,
    ) -> tuple[asyncio.Task[None] | None, httpx.AsyncClient | None]:
        """Start the dead-man's switch when a monitor URL is configured.

        Its own httpx client on purpose: the heartbeat must keep working
        when Telegram is unconfigured, and a slow Telegram send must never
        share a connection pool with the liveness signal.
        """
        url = self.config.heartbeat_url
        if not url:
            # Truthiness: an empty-string env var disables the heartbeat
            # gracefully, same convention as the other optional services.
            log_event(logger, logging.INFO, "heartbeat_disabled")
            return None, None
        from tradebot.notify import HeartbeatPinger

        client = httpx.AsyncClient(timeout=10)
        pinger = HeartbeatPinger(
            url,
            client,
            interval=timedelta(seconds=self.config.heartbeat_interval_seconds),
        )
        pinger.attach_to(self.bus)
        task = asyncio.create_task(pinger.run())

        def log_heartbeat_outcome(finished: asyncio.Task[None]) -> None:
            # A silently dead heartbeat is the one failure mode this feature
            # exists to prevent — its own crash must be loud in the logs.
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                log_event(logger, logging.ERROR, "heartbeat_task_crashed", exc_info=True)

        task.add_done_callback(log_heartbeat_outcome)
        return task, client

    def _start_news_monitor_if_configured(
        self,
    ) -> tuple[asyncio.Task[None] | None, httpx.AsyncClient | None]:
        """Start news polling when a CryptoPanic token is configured.

        Its own httpx client: a slow news API must never share a pool with
        alerts or the heartbeat. Without a token the news *gate* still runs
        (calendar windows need no news source) — only polling is skipped.
        """
        token = self.config.cryptopanic_token
        if not token:
            # Truthiness: an empty-string env var disables polling
            # gracefully, same convention as the other optional services.
            log_event(logger, logging.INFO, "news_polling_disabled")
            return None, None
        client = httpx.AsyncClient(timeout=15)
        monitor = NewsMonitor(
            CryptoPanicSource(token, client),
            self.news_flags,
            # Read fresh per poll: coins are added and removed at runtime.
            tracked_coins=lambda: self.symbols,
            bus=self.bus,
            poll_interval=timedelta(seconds=self.config.news_poll_seconds),
            sentiment=self.sentiment,
        )
        task = asyncio.create_task(monitor.run())

        def log_news_outcome(finished: asyncio.Task[None]) -> None:
            # The monitor catches its own poll errors; reaching here other
            # than by cancellation means event awareness died — say so.
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                log_event(logger, logging.ERROR, "news_monitor_crashed", exc_info=True)

        task.add_done_callback(log_news_outcome)
        log_event(
            logger,
            logging.INFO,
            "news_polling_enabled",
            interval_seconds=self.config.news_poll_seconds,
        )
        return task, client

    def _start_sentiment_if_configured(
        self,
    ) -> tuple[asyncio.Task[None] | None, httpx.AsyncClient | None]:
        """Start Fear & Greed / dominance polling when sentiment is in play.

        Skipped when the regime gate ended up disabled (no reference feed):
        the readings would tighten a gate that does not exist.
        """
        if self.sentiment is None or self.regime_detector is None:
            log_event(logger, logging.INFO, "sentiment_polling_disabled")
            return None, None
        client = httpx.AsyncClient(timeout=15)
        monitor = SentimentMonitor(
            self.sentiment,
            client,
            poll_interval=timedelta(minutes=self.config.sentiment_poll_minutes),
        )
        task = asyncio.create_task(monitor.run())

        def log_sentiment_outcome(finished: asyncio.Task[None]) -> None:
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - poll_once catches its own
                log_event(logger, logging.ERROR, "sentiment_monitor_crashed", exc_info=True)

        task.add_done_callback(log_sentiment_outcome)
        log_event(
            logger,
            logging.INFO,
            "sentiment_polling_enabled",
            interval_minutes=self.config.sentiment_poll_minutes,
        )
        return task, client

    def _start_backups_if_configured(
        self,
    ) -> tuple[asyncio.Task[None] | None, httpx.AsyncClient | None]:
        """Start scheduled database backups when object storage is configured.

        The first backup runs immediately so bad credentials surface in the
        first minutes of a deploy, not at 3am. Config validation already
        guarantees the four settings come as a complete set.
        """
        config = self.config
        if not (config.backup_s3_endpoint and config.backup_s3_bucket):
            log_event(logger, logging.INFO, "scheduled_backups_disabled")
            return None, None
        from tradebot.persistence.backup import S3Config, S3Uploader, run_backup

        assert config.backup_s3_access_key and config.backup_s3_secret_key  # validator
        client = httpx.AsyncClient(timeout=120)
        uploader = S3Uploader(
            S3Config(
                endpoint=config.backup_s3_endpoint,
                bucket=config.backup_s3_bucket,
                access_key=config.backup_s3_access_key,
                secret_key=config.backup_s3_secret_key,
                region=config.backup_s3_region,
            ),
            client,
        )

        async def backup_loop() -> None:
            while True:
                try:
                    await run_backup(self._database, uploader, config.backup_prefix)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A failed backup is loud and retried — never fatal:
                    # losing tonight's backup must not stop trading.
                    log_event(logger, logging.ERROR, "scheduled_backup_failed", exc_info=True)
                await asyncio.sleep(config.backup_interval_hours * 3600)

        task = asyncio.create_task(backup_loop())

        def log_backup_outcome(finished: asyncio.Task[None]) -> None:
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - loop catches its own errors
                log_event(logger, logging.ERROR, "backup_loop_crashed", exc_info=True)

        task.add_done_callback(log_backup_outcome)
        log_event(
            logger,
            logging.INFO,
            "scheduled_backups_enabled",
            interval_hours=config.backup_interval_hours,
            endpoint=config.backup_s3_endpoint,
            bucket=config.backup_s3_bucket,
        )
        return task, client

    def _start_improver_if_enabled(self) -> asyncio.Task[None] | None:
        """Start the automated improvement loop (§12.7) unless disabled.

        Inherently paper-scoped: this worker refuses to construct in any
        other mode, and promotions only rewrite the paper strategy's
        parameters — never the mode, never the risk limits.
        """
        if not self.config.auto_improve_enabled:
            log_event(logger, logging.INFO, "automated_improvement_disabled")
            return None
        improver = AutoImprover(
            sweeps=self.sweeps,
            evaluations=self.evaluations,
            store=self.evaluation_store,
            active_params=lambda: self.strategy_params,
            symbols=lambda: self.symbols,
            promote=self.apply_strategy_params,
            confirm=self._confirm_promotion,
            interval=timedelta(hours=self.config.auto_improve_interval_hours),
            history_days=self.config.auto_improve_history_days,
            timeframe=self.config.auto_improve_timeframe,
            notify=self._notify,
        )
        task = asyncio.create_task(improver.run())

        def log_improver_outcome(finished: asyncio.Task[None]) -> None:
            # The loop catches its own cycle errors; reaching here other
            # than by cancellation means self-improvement died — say so.
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                log_event(
                    logger, logging.ERROR, "automated_improvement_loop_crashed", exc_info=True
                )

        task.add_done_callback(log_improver_outcome)
        log_event(
            logger,
            logging.INFO,
            "automated_improvement_enabled",
            interval_hours=self.config.auto_improve_interval_hours,
            history_days=self.config.auto_improve_history_days,
            timeframe=self.config.auto_improve_timeframe,
        )
        return task

    def _start_api(self) -> asyncio.Task[None]:
        """Serve HTTP as a background task.

        With ``TRADEBOT_API_TOKEN`` set this is the full control plane;
        without it, a health-only app — the platform healthcheck must work
        in every configuration, control plane or not.
        """
        from collections.abc import Generator

        import uvicorn  # local import: only the running worker serves HTTP

        from tradebot.api import create_app, create_health_only_app

        if not self.config.api_token:
            # Truthiness: an empty-string env var must disable the control
            # plane gracefully, not crash create_app at startup.
            log_event(logger, logging.INFO, "control_api_disabled")
            app = create_health_only_app()
        else:
            log_event(logger, logging.INFO, "control_api_enabled")
            app = create_app(self, self.config.api_token)

        class NoSignalCaptureServer(uvicorn.Server):
            """Leave signal handling to the worker, which owns SIGTERM.

            uvicorn's default ``capture_signals`` installs its own handlers
            via ``signal.signal``, silently replacing the worker's shutdown
            wiring — the API would stop on SIGTERM while the bot kept trading.
            """

            @contextlib.contextmanager
            def capture_signals(self) -> Generator[None, None, None]:
                yield

        server = NoSignalCaptureServer(
            uvicorn.Config(app, host="0.0.0.0", port=self.config.api_port, log_level="warning")
        )
        task = self._supervise_control_api(asyncio.create_task(server.serve()))
        log_event(logger, logging.INFO, "http_server_listening", port=self.config.api_port)
        return task

    def _supervise_control_api(self, task: asyncio.Task[None]) -> asyncio.Task[None]:
        """Trip the safety pause if the control-plane task ends unexpectedly.

        A dead control plane (port conflict, bad config, an unhandled server
        error, or the server simply returning) leaves the operator unable to
        pause, kill, or approve — so the bot must stop taking new risk on its
        own. The task is meant to run for the worker's whole life; any end
        other than shutdown cancellation is a failure, a clean return
        included. Returns ``task`` for chaining.
        """

        def on_api_outcome(finished: asyncio.Task[None]) -> None:
            try:
                finished.result()
            except asyncio.CancelledError:
                return  # cancelled at shutdown — the one expected ending
            except Exception:
                log_event(logger, logging.ERROR, "control_api_server_crashed", exc_info=True)
            else:
                if self._stop_requested.is_set():
                    return  # the server returned as part of a clean shutdown
                log_event(logger, logging.ERROR, "control_api_server_exited")
            self._trip_safety_pause("control_api")

        task.add_done_callback(on_api_outcome)
        return task

    def stop(self) -> None:
        """Request shutdown (also wired to SIGTERM for Railway deploys)."""
        self._stop_requested.set()
        for feed in self._feeds.values():
            feed.stop()


async def run_from_env() -> None:
    """Build the worker from environment variables and run it until signaled."""
    config = AppConfig()
    configure_logging(config.log_level, config.log_format)
    if config.database_url is None:
        raise ValueError("TRADEBOT_DATABASE_URL is required to run the worker")

    import ccxt.pro  # imported here: heavy, and only the entrypoint needs it

    exchange_class = getattr(ccxt.pro, config.exchange_id, None)
    if exchange_class is None:
        raise ValueError(f"unknown CCXT exchange id {config.exchange_id!r}")
    # The built-in rate limiter shares a request budget across backfill and
    # stream reconnects — without it, a long backfill can earn an IP ban.
    exchange = exchange_class({"enableRateLimit": True})
    try:
        async with Database(config.database_url) as database:
            worker = Worker(config, database, exchange)
            loop = asyncio.get_running_loop()
            main_task = asyncio.current_task()

            def handle_shutdown() -> None:
                # Setting the stop flag is not enough: watch_ohlcv may be
                # mid-await for the next candle, so cancel to unblock it and
                # reach the finally that closes the exchange session.
                worker.stop()
                if main_task is not None:
                    main_task.cancel()

            try:
                for signal_number in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(signal_number, handle_shutdown)
            except NotImplementedError:  # pragma: no cover - Windows dev boxes
                pass

            try:
                await worker.run()
            except asyncio.CancelledError:
                log_event(logger, logging.INFO, "worker_cancelled")
    finally:
        await exchange.close()


def main() -> None:
    """Run the worker synchronously (``python -m tradebot``)."""
    asyncio.run(run_from_env())
