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
import signal
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from tradebot.authorization import ProposalQueue
from tradebot.core.config import AppConfig, TradingMode, validate_symbol_quote
from tradebot.core.events import EventBus
from tradebot.engine import TradingEngine
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.marketdata.live_feed import LiveMarketDataFeed, OhlcvExchange
from tradebot.persistence import CandleStore, CoinStore, Database, DecisionStore, FillStore
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskConfig, RiskManager
from tradebot.strategies import TrendFollowingConfig, TrendFollowingStrategy

logger = logging.getLogger(__name__)


class TradingVenue(OhlcvExchange, Protocol):
    """The worker's view of the exchange: OHLCV plus the market catalog."""

    async def load_markets(self) -> Mapping[str, object]:
        """Return the exchange's market catalog keyed by unified symbol."""
        ...


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
        self.coin_store = CoinStore(database)
        self.portfolio = Portfolio(config.paper_initial_balance_quote)
        # One risk manager for all symbols: the circuit breakers and equity
        # caps are account-level and must see every position through one
        # pair of eyes (engines and feeds are per-symbol).
        self.risk_manager = RiskManager(RiskConfig(), self.portfolio)
        self.engines: dict[str, TradingEngine] = {}
        self._feeds: dict[str, LiveMarketDataFeed] = {}
        self._feed_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_group: asyncio.TaskGroup | None = None
        self._stop_requested = asyncio.Event()
        self._exchange = exchange
        self._database = database

    @property
    def symbols(self) -> tuple[str, ...]:
        """The actively traded pairs, in the order they were added."""
        return tuple(self.engines)

    def _activate(self, symbol: str) -> None:
        """Build and wire one coin's engine and feed; start the feed if running."""
        engine = TradingEngine(
            TrendFollowingStrategy(TrendFollowingConfig()),
            self.risk_manager,
            self.portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig()),
            symbol=symbol,
            fill_store=self.fill_store,
            decision_store=self.decision_store,
            autonomy_mode=self.config.autonomy_mode,
            proposal_queue=ProposalQueue(
                ttl=timedelta(seconds=self.config.proposal_ttl_seconds),
                max_drift_fraction=self.config.proposal_max_drift_fraction,
            ),
        )
        engine.attach_to(self.bus)
        feed = LiveMarketDataFeed(
            self._exchange,
            symbol,
            self.candle_store,
            self.bus,
            history_days=self.config.history_backfill_days,
        )
        self.engines[symbol] = engine
        self._feeds[symbol] = feed
        if self._task_group is not None:
            self._feed_tasks[symbol] = self._task_group.create_task(feed.run())

    async def initialize(self) -> int:
        """Create the schema, load the active coins, and replay the journal.

        Returns the number of fills replayed. Separate from ``__init__``
        because the coin set lives in the database.
        """
        await self._database.create_schema()
        if await self.coin_store.seed_if_empty(self.config.symbol_list(), datetime.now(UTC)):
            logger.info("first boot: coins seeded from TRADEBOT_SYMBOLS")
        for symbol in await self.coin_store.list_symbols():
            self._activate(symbol)
        return await self.replay_journal()

    async def add_coin(self, symbol: str) -> None:
        """Start trading ``symbol``: validate, persist, build, stream.

        Raises ``ValueError`` for an invalid, duplicate, or unlisted pair.
        """
        symbol = symbol.strip()
        validate_symbol_quote(symbol, self.config.quote_currency)
        if symbol in self.engines:
            raise ValueError(f"{symbol} is already being traded")
        markets = await self._exchange.load_markets()
        if symbol not in markets:
            raise ValueError(f"{symbol} is not listed on {self.config.exchange_id}")
        await self.coin_store.add(symbol, datetime.now(UTC))
        self._activate(symbol)
        logger.info("coin added at runtime: %s", symbol)

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
        if self.portfolio.position(symbol) is not None:
            raise RuntimeError(f"{symbol} has an open position; flatten it first (kill or exit)")
        if engine.pending_proposals():
            raise RuntimeError(f"{symbol} has a pending proposal; approve or reject it first")
        await self.coin_store.remove(symbol)
        self._feeds.pop(symbol).stop()
        task = self._feed_tasks.pop(symbol, None)
        if task is not None:
            # The TaskGroup ignores cancelled children, so removal never
            # tears down the other feeds the way a crash would.
            task.cancel()
        engine.detach_from(self.bus)
        del self.engines[symbol]
        logger.info("coin removed at runtime: %s", symbol)

    async def replay_journal(self) -> int:
        """Rebuild portfolio state from persisted fills; returns fills replayed.

        Paper-mode reconciliation: the journal is the source of truth across
        restarts (live mode will reconcile against the exchange instead).
        Replayed history is then rebased out of the loss-streak tracker —
        an old losing streak must not start a new cooldown at boot.
        """
        fills = await self.fill_store.fetch_all()
        for fill in fills:
            self.portfolio.apply_fill(fill)
        self.risk_manager.rebase_realized_pnl()
        return len(fills)

    async def run(self) -> None:
        """Start the bot (and the control API, if configured) until stopped."""
        replayed = await self.initialize()
        positions = (
            ", ".join(
                f"{symbol}={position.quantity_base}"
                for symbol, position in self.portfolio.positions.items()
            )
            or "flat"
        )
        logger.info(
            "worker starting: %s on %s (paper), %d fills replayed, positions=%s, balance=%s",
            ", ".join(self.symbols),
            self.config.exchange_id,
            replayed,
            positions,
            self.portfolio.quote_balance,
        )
        # Started inside the try so a failure in any later startup step still
        # tears down whatever already runs (no leaked tasks or clients).
        api_task: asyncio.Task[None] | None = None
        notifier_client: httpx.AsyncClient | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        heartbeat_client: httpx.AsyncClient | None = None
        try:
            api_task = self._start_api()
            notifier_client = await self._start_notifier_if_configured()
            heartbeat_task, heartbeat_client = self._start_heartbeat_if_configured()
            # TaskGroup, not gather: if one feed crashes, the others must be
            # cancelled with it — a bot trading some symbols while blind on
            # another would be worse than one that stops and restarts. The
            # keeper task holds the group open while coins are added and
            # removed at runtime (even down to zero feeds).
            async with asyncio.TaskGroup() as task_group:
                self._task_group = task_group
                task_group.create_task(self._stop_requested.wait())
                for symbol, feed in self._feeds.items():
                    self._feed_tasks[symbol] = task_group.create_task(feed.run())
        finally:
            self._task_group = None  # late add_coin calls must not target a closed group
            for task in (api_task, heartbeat_task):
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
                    logger.exception("background task failed during shutdown")
            for client in (notifier_client, heartbeat_client):
                if client is not None:
                    await client.aclose()
        logger.info("worker stopped cleanly")

    async def _start_notifier_if_configured(self) -> httpx.AsyncClient | None:
        """Attach Telegram alerts when both token and chat id are set."""
        token = self.config.telegram_bot_token
        chat_id = self.config.telegram_chat_id
        if not token or not chat_id:
            # Truthiness, not None-ness: empty-string env vars must disable
            # alerts gracefully rather than crash the worker at startup.
            logger.info("telegram alerts disabled: token or chat id not set")
            return None
        from tradebot.notify import TelegramNotifier

        client = httpx.AsyncClient(timeout=10)
        notifier = TelegramNotifier(token, chat_id, client)
        notifier.attach_to(self.bus)
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
            logger.info("dead-man's switch disabled: no TRADEBOT_HEARTBEAT_URL")
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
                logger.exception("dead-man's switch heartbeat task crashed")

        task.add_done_callback(log_heartbeat_outcome)
        return task, client

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
            logger.info("control API disabled (no TRADEBOT_API_TOKEN); serving /health only")
            app = create_health_only_app()
        else:
            logger.info("control API enabled")
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
        task = asyncio.create_task(server.serve())

        def log_api_outcome(finished: asyncio.Task[None]) -> None:
            # A crashed API (port conflict, bad config) must be loud: the bot
            # would otherwise trade on with its control plane silently dead.
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("control API server crashed or failed to start")

        task.add_done_callback(log_api_outcome)
        logger.info("HTTP server listening on port %d", self.config.api_port)
        return task

    def stop(self) -> None:
        """Request shutdown (also wired to SIGTERM for Railway deploys)."""
        self._stop_requested.set()
        for feed in self._feeds.values():
            feed.stop()


async def run_from_env() -> None:
    """Build the worker from environment variables and run it until signaled."""
    config = AppConfig()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
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
                logger.info("worker cancelled by shutdown signal")
    finally:
        await exchange.close()


def main() -> None:
    """Run the worker synchronously (``python -m tradebot``)."""
    asyncio.run(run_from_env())
