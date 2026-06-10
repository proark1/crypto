"""The per-candle trading loop shared by backtest and paper modes.

Fixed order of operations per closed candle (identical to what the backtest
proved out, because it is the same code):

1. the adapter evaluates open orders against the candle — decisions made on
   earlier candles fill no earlier than now;
2. fills update the portfolio (and the persistent fill journal, if wired);
3. the strategy sees the candle and the post-fill position;
4. the risk manager sizes or vetoes the resulting signal;
5. an approved order is submitted to the adapter.
"""

from __future__ import annotations

import logging

from tradebot.core.events import CandleClosed, EventBus
from tradebot.core.models import Candle, CandleInterval, Fill
from tradebot.execution.simulator import SimulatedExecutionAdapter
from tradebot.persistence import FillStore
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskManager
from tradebot.strategies import Strategy

logger = logging.getLogger(__name__)


class TradingEngine:
    """Drives one strategy on one symbol through the production order flow.

    The adapter is the candle-driven simulator in both backtest and paper
    mode — paper trading is, by definition, real prices with simulated
    fills. A live adapter (exchange-driven fills) gets its own engine
    variant in Phase 3; strategies and risk code will not change.
    """

    def __init__(
        self,
        strategy: Strategy,
        risk_manager: RiskManager,
        portfolio: Portfolio,
        adapter: SimulatedExecutionAdapter,
        symbol: str | None = None,
        interval: CandleInterval = CandleInterval.M1,
        fill_store: FillStore | None = None,
    ) -> None:
        """Wire the components; ``symbol=None`` binds to the first candle seen."""
        self._strategy = strategy
        self._risk_manager = risk_manager
        self._portfolio = portfolio
        self._adapter = adapter
        self._symbol = symbol
        self._interval = interval
        self._fill_store = fill_store
        self._fills: list[Fill] = []
        adapter.set_fill_handler(self._on_fill)

    @property
    def fills(self) -> tuple[Fill, ...]:
        """Every fill seen by this engine, in execution order."""
        return tuple(self._fills)

    def attach_to(self, bus: EventBus) -> None:
        """Subscribe to ``CandleClosed`` events (paper/live wiring)."""
        bus.subscribe(CandleClosed, self._on_candle_event)

    async def _on_candle_event(self, event: CandleClosed) -> None:
        candle = event.candle
        if candle.interval != self._interval:
            return
        if self._symbol is not None and candle.symbol != self._symbol:
            return
        await self.process_candle(candle)

    async def process_candle(self, candle: Candle) -> None:
        """Run one full iteration of the trading loop for ``candle``."""
        if self._symbol is None:
            self._symbol = candle.symbol
        elif candle.symbol != self._symbol:
            raise ValueError(f"engine is bound to {self._symbol}, got {candle.symbol}")
        await self._adapter.process_candle(candle)
        signal = self._strategy.on_candle(candle, self._portfolio.position(candle.symbol))
        if signal is None:
            return
        order = self._risk_manager.evaluate(signal, candle.close_quote)
        if order is None:
            logger.info(
                "signal vetoed by risk manager: %s %s %s",
                signal.strategy_name,
                signal.side,
                signal.symbol,
            )
            return
        logger.info(
            "submitting order %s: %s %s %s (signal %s)",
            order.client_order_id,
            order.side,
            order.quantity_base,
            order.symbol,
            order.signal_id,
        )
        await self._adapter.submit(order)

    async def _on_fill(self, fill: Fill) -> None:
        # Journal before touching in-memory state: if the write fails, memory
        # must not be ahead of the persistent record that restart recovery
        # replays — losing the in-memory update is recoverable, the reverse
        # is silent divergence.
        if self._fill_store is not None:
            await self._fill_store.append(fill)
        self._portfolio.apply_fill(fill)
        self._fills.append(fill)
        logger.info(
            "fill %s: %s %s %s @ %s (fee %s)",
            fill.client_order_id,
            fill.side,
            fill.quantity_base,
            fill.symbol,
            fill.price_quote,
            fill.fee_quote,
        )
