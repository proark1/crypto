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
import logging
import signal

from tradebot.core.config import AppConfig, TradingMode
from tradebot.core.events import EventBus
from tradebot.engine import TradingEngine
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.marketdata.live_feed import LiveMarketDataFeed, OhlcvExchange
from tradebot.persistence import CandleStore, Database, FillStore
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskConfig, RiskManager
from tradebot.strategies import TrendFollowingConfig, TrendFollowingStrategy

logger = logging.getLogger(__name__)


class Worker:
    """One symbol, one strategy, paper fills — the Phase 2 bot."""

    def __init__(self, config: AppConfig, database: Database, exchange: OhlcvExchange) -> None:
        """Compose every component; raises unless ``config.mode`` is paper."""
        if config.mode != TradingMode.PAPER:
            raise NotImplementedError(
                f"worker only supports paper mode for now, got {config.mode}; "
                "live trading is a Phase 3 milestone with its own adapter"
            )
        self.config = config
        self.bus = EventBus()
        self.candle_store = CandleStore(database)
        self.fill_store = FillStore(database)
        self.portfolio = Portfolio(config.paper_initial_balance_quote)
        self.engine = TradingEngine(
            TrendFollowingStrategy(TrendFollowingConfig()),
            RiskManager(RiskConfig(), self.portfolio),
            self.portfolio,
            SimulatedExecutionAdapter(FillSimulatorConfig()),
            symbol=config.symbol,
            fill_store=self.fill_store,
        )
        self.feed = LiveMarketDataFeed(exchange, config.symbol, self.candle_store, self.bus)
        self._database = database

    async def replay_journal(self) -> int:
        """Rebuild portfolio state from persisted fills; returns fills replayed.

        Paper-mode reconciliation: the journal is the source of truth across
        restarts (live mode will reconcile against the exchange instead).
        """
        fills = await self.fill_store.fetch_all(self.config.symbol)
        for fill in fills:
            self.portfolio.apply_fill(fill)
        return len(fills)

    async def run(self) -> None:
        """Start the bot and block until :meth:`stop` is called."""
        await self._database.create_schema()
        replayed = await self.replay_journal()
        position = self.portfolio.position(self.config.symbol)
        logger.info(
            "worker starting: %s on %s (paper), %d fills replayed, position=%s, balance=%s",
            self.config.symbol,
            self.config.exchange_id,
            replayed,
            position.quantity_base if position else "flat",
            self.portfolio.quote_balance,
        )
        self.engine.attach_to(self.bus)
        await self.feed.run()
        logger.info("worker stopped cleanly")

    def stop(self) -> None:
        """Request shutdown (also wired to SIGTERM for Railway deploys)."""
        self.feed.stop()


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
