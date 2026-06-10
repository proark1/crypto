"""The per-candle trading loop shared by backtest and paper modes.

Fixed order of operations per closed candle (identical to what the backtest
proved out, because it is the same code):

1. the adapter evaluates open orders against the candle — decisions made on
   earlier candles fill no earlier than now;
2. fills update the portfolio (and the persistent fill journal, if wired);
3. the strategy sees the candle and the post-fill position;
4. entry gates (regime, and later confirmation filters) may block a BUY;
5. the risk manager sizes or vetoes the resulting signal;
6. an approved order is submitted to the adapter.
"""

from __future__ import annotations

import logging

from tradebot.authorization import ProposalQueue
from tradebot.core.events import CandleClosed, EventBus, FillRecorded, ProposalCreated
from tradebot.core.models import (
    AutonomyMode,
    Candle,
    CandleInterval,
    Decision,
    DecisionOutcome,
    Fill,
    Proposal,
    ProposalStatus,
    Side,
    Signal,
)
from tradebot.execution.simulator import SimulatedExecutionAdapter
from tradebot.persistence import DecisionStore, FillStore
from tradebot.portfolio import Portfolio
from tradebot.risk import CircuitBreakers, RiskManager
from tradebot.signals import EntryGate
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
        decision_store: DecisionStore | None = None,
        autonomy_mode: AutonomyMode = AutonomyMode.AUTONOMOUS,
        proposal_queue: ProposalQueue | None = None,
        entry_gates: tuple[EntryGate, ...] = (),
    ) -> None:
        """Wire the components; ``symbol=None`` binds to the first candle seen.

        ``entry_gates`` run on BUY signals only, in order, before
        authorization and risk sizing (the §5.2 pipeline). Exits are never
        gated: protective actions must not sit behind a filter.
        """
        if autonomy_mode == AutonomyMode.COPILOT and proposal_queue is None:
            raise ValueError("co-pilot mode requires a proposal queue")
        self._strategy = strategy
        self._risk_manager = risk_manager
        self._portfolio = portfolio
        self._adapter = adapter
        self._symbol = symbol
        self._interval = interval
        self._fill_store = fill_store
        self._decision_store = decision_store
        self._autonomy_mode = autonomy_mode
        self._proposal_queue = proposal_queue
        self._entry_gates = entry_gates
        self._fills: list[Fill] = []
        self._paused = False
        self._last_candle: Candle | None = None
        self._bus: EventBus | None = None
        adapter.set_fill_handler(self._on_fill)

    @property
    def fills(self) -> tuple[Fill, ...]:
        """Every fill seen by this engine, in execution order."""
        return tuple(self._fills)

    @property
    def strategy_name(self) -> str:
        """The active signal generator's identifier, for status surfaces."""
        return self._strategy.name

    def replace_strategy(self, strategy: Strategy) -> None:
        """Swap the signal generator in place (automated promotion, §12.7).

        Only the strategy changes: the position, pending orders, risk
        state, and gates are untouched — a promotion mid-position simply
        means the next exit comes from the new rules. The replacement must
        arrive pre-warmed (primed from stored candles by the caller), and
        the assignment is atomic on the event loop, so no candle is ever
        half-processed across two strategies.
        """
        previous = self._strategy.name
        self._strategy = strategy
        logger.info(
            "strategy replaced for %s: %s -> %s", self._symbol, previous, strategy.name
        )

    @property
    def paused(self) -> bool:
        """True while the strategy is muted (orders/stops keep working)."""
        return self._paused

    @property
    def breakers(self) -> CircuitBreakers:
        """Circuit-breaker state, for the control plane's status view."""
        return self._risk_manager.breakers

    def reset_breakers(self) -> None:
        """Operator reset of a tripped breaker; an explicit, logged action."""
        self._risk_manager.breakers.reset()

    def pause(self) -> None:
        """Mute the strategy: no new signals; resting orders stay live.

        Pausing never touches protective orders — disabling stops as a side
        effect of pausing would be the dangerous kind of surprise.
        """
        self._paused = True
        logger.info("engine paused for %s", self._symbol)

    def resume(self) -> None:
        """Unmute the strategy."""
        self._paused = False
        logger.info("engine resumed for %s", self._symbol)

    async def kill(self) -> bool:
        """Flatten and halt: cancel open orders, market-exit, stay paused.

        Returns True if an exit order was submitted (it fills on the next
        candle, which the engine keeps processing while paused). Deliberately
        does not consult the strategy — the kill switch must work even if
        strategy logic is wedged (ARCHITECTURE.md 6.3).

        Raises ``RuntimeError`` if a position is open but no candle has been
        seen to price the exit: the bot is halted but **not** flat, and that
        state must never be reportable as "nothing to flatten".
        """
        self.pause()
        for order in self._adapter.open_orders():
            # One failed cancel (e.g. an order racing its own fill on a live
            # venue) must not stop the kill switch from cancelling the rest.
            try:
                await self._adapter.cancel(order.client_order_id)
                logger.warning("kill switch cancelled open order %s", order.client_order_id)
            except Exception:
                logger.exception("kill switch failed to cancel order %s", order.client_order_id)
        if self._symbol is None:
            return False
        position = self._portfolio.position(self._symbol)
        if position is None:
            logger.warning("kill switch: already flat, halted only")
            return False
        if self._last_candle is None:
            raise RuntimeError(
                "kill switch: position open but no candle seen yet; halted but NOT flat"
            )
        last_close = self._last_candle.close_quote
        signal = Signal(
            signal_id=f"kill:{self._symbol}:{self._last_candle.close_time.isoformat()}",
            strategy_name="kill_switch",
            symbol=self._symbol,
            side=Side.SELL,
            confidence=1.0,
            stop_price_quote=last_close,  # informational: this is a full exit
            reasons=("kill switch",),
            created_at=self._last_candle.close_time,
        )
        exit_order = self._risk_manager.evaluate(signal, last_close)
        if exit_order is None:  # pragma: no cover - exits always pass; defensive
            logger.error("kill switch exit was vetoed; halted unflattened")
            return False
        await self._adapter.submit(exit_order)
        logger.warning(
            "kill switch submitted exit %s for %s", exit_order.client_order_id, self._symbol
        )
        await self._record_decision(signal, DecisionOutcome.SUBMITTED)
        return True

    def attach_to(self, bus: EventBus) -> None:
        """Subscribe to ``CandleClosed`` events (paper/live wiring).

        Once attached, the engine also publishes ``FillRecorded`` for
        observers (notifications, UI pushes). The backtest runner never
        attaches a bus, so backtests stay observer-free and byte-identical.
        """
        self._bus = bus
        bus.subscribe(CandleClosed, self._on_candle_event)

    def detach_from(self, bus: EventBus) -> None:
        """Unsubscribe from ``bus`` (runtime coin removal).

        A detached engine must never see another candle: if the coin is
        re-added, a fresh engine subscribes, and a lingering subscription
        here would process every candle twice.
        """
        bus.unsubscribe(CandleClosed, self._on_candle_event)
        self._bus = None

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
        self._last_candle = candle
        await self._adapter.process_candle(candle)
        # Post-fill equity mark: the breakers must judge the same books the
        # strategy is about to see.
        self._risk_manager.on_candle(candle)
        await self._sweep_proposals(candle)
        # The strategy consumes every candle even when paused so indicators
        # stay warm for resume; only its output is discarded.
        signal = self._strategy.on_candle(candle, self._portfolio.position(candle.symbol))
        if signal is None:
            return
        if self._paused:
            logger.info(
                "paused: discarding signal %s %s %s",
                signal.strategy_name,
                signal.side,
                signal.symbol,
            )
            await self._record_decision(signal, DecisionOutcome.PAUSED)
            return
        if signal.side == Side.BUY:
            # Entry gates run before authorization: a proposal for a gated
            # entry must never reach the user (§5.2 pipeline order). Exits
            # are deliberately not gated — capital protection comes first.
            for gate in self._entry_gates:
                verdict = gate.evaluate(signal)
                if not verdict.allowed:
                    logger.info(
                        "entry gated: %s %s — %s",
                        signal.strategy_name,
                        signal.symbol,
                        "; ".join(verdict.reasons),
                    )
                    journaled = signal.model_copy(
                        update={"reasons": signal.reasons + verdict.reasons}
                    )
                    await self._record_decision(journaled, DecisionOutcome.GATED)
                    return
        if (
            self._autonomy_mode == AutonomyMode.COPILOT
            and self._proposal_queue is not None
            and signal.side == Side.BUY
        ):
            # Entries wait for the user; exits never do (capital protection
            # must not sit behind a human queue — ARCHITECTURE.md 4.8).
            proposal = self._proposal_queue.create(signal, candle.close_quote, candle.close_time)
            if proposal is None:
                logger.info("proposal already pending for %s; signal dropped", signal.symbol)
                return
            logger.info("co-pilot proposal created: %s", signal.signal_id)
            await self._record_decision(signal, DecisionOutcome.PROPOSED)
            if self._bus is not None:
                await self._bus.publish(ProposalCreated(proposal=proposal))
            return
        order = self._risk_manager.evaluate(signal, candle.close_quote)
        if order is None:
            logger.info(
                "signal vetoed by risk manager: %s %s %s",
                signal.strategy_name,
                signal.side,
                signal.symbol,
            )
            await self._record_decision(signal, DecisionOutcome.VETOED)
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
        await self._record_decision(signal, DecisionOutcome.SUBMITTED)

    def pending_proposals(self) -> tuple[Proposal, ...]:
        """Return co-pilot proposals awaiting an answer (empty if autonomous)."""
        if self._proposal_queue is None:
            return ()
        return self._proposal_queue.pending()

    def has_proposal(self, signal_id: str) -> bool:
        """Return whether this engine's queue knows ``signal_id`` (any status).

        Lets a multi-symbol control plane route an approve/reject to the
        right engine without parsing symbols out of signal ids.
        """
        return self._proposal_queue is not None and (
            self._proposal_queue.status_of(signal_id) is not None
        )

    async def approve_proposal(self, signal_id: str) -> str:
        """Execute a pending proposal; risk checks re-run at approval time.

        Returns a human-readable outcome. Raises ``KeyError`` for unknown
        proposals and ``ValueError`` when the proposal expired or drifted —
        approval of a stale market is refused, not silently honored.
        """
        if self._proposal_queue is None:
            raise KeyError("no proposal queue: engine is autonomous")
        queue_status = self._proposal_queue.status_of(signal_id)
        if queue_status is None:
            # Unknown-id beats no-data: a ghost id is the caller's mistake and
            # must be reported as such regardless of market state.
            raise KeyError(f"no pending proposal {signal_id!r}")
        if queue_status != ProposalStatus.PENDING:
            # Truthful staleness: it existed, but was resolved before the
            # user's answer arrived — never report it as "not found".
            raise ValueError(f"proposal {signal_id!r} is already {queue_status.value}")
        if self._last_candle is None:
            raise ValueError("no market data yet; cannot price an approval")
        current_price = self._last_candle.close_quote
        try:
            proposal = self._proposal_queue.approve(
                signal_id, self._last_candle.close_time, current_price
            )
        except ValueError:
            # The approval itself discovered staleness (sweep had not run
            # yet); journal the transition so the trail stays complete.
            stale = self._proposal_queue.get(signal_id)
            if stale is not None and stale.status in (
                ProposalStatus.EXPIRED,
                ProposalStatus.DRIFTED,
            ):
                outcome = (
                    DecisionOutcome.EXPIRED
                    if stale.status == ProposalStatus.EXPIRED
                    else DecisionOutcome.DRIFTED
                )
                await self._record_decision(stale.signal, outcome)
            raise
        order = self._risk_manager.evaluate(proposal.signal, current_price)
        if order is None:
            await self._record_decision(proposal.signal, DecisionOutcome.VETOED)
            return "approved, but risk checks vetoed at approval time"
        await self._adapter.submit(order)
        await self._record_decision(proposal.signal, DecisionOutcome.APPROVED)
        logger.info("proposal %s approved; order %s submitted", signal_id, order.client_order_id)
        return "approved; order submitted, fills on next candle"

    async def reject_proposal(self, signal_id: str) -> None:
        """Reject a pending proposal; raises ``KeyError`` if unknown."""
        if self._proposal_queue is None:
            raise KeyError("no proposal queue: engine is autonomous")
        proposal = self._proposal_queue.reject(signal_id)
        await self._record_decision(proposal.signal, DecisionOutcome.REJECTED)
        logger.info("proposal %s rejected by user", signal_id)

    async def _sweep_proposals(self, candle: Candle) -> None:
        if self._proposal_queue is None:
            return
        for stale in self._proposal_queue.sweep(candle.close_time, candle.close_quote):
            outcome = (
                DecisionOutcome.EXPIRED
                if stale.status == ProposalStatus.EXPIRED
                else DecisionOutcome.DRIFTED
            )
            logger.info("proposal %s removed: %s", stale.signal.signal_id, outcome.value)
            await self._record_decision(stale.signal, outcome)

    async def _record_decision(self, signal: Signal, outcome: DecisionOutcome) -> None:
        """Journal a signal's fate for the decision-pipeline view.

        Best-effort by design: the explainability trail must never break
        trading, so persistence failures are logged and dropped.
        """
        if self._decision_store is None:
            return
        decision = Decision(
            signal_id=signal.signal_id,
            strategy_name=signal.strategy_name,
            symbol=signal.symbol,
            side=signal.side,
            stop_price_quote=signal.stop_price_quote,
            reasons=signal.reasons,
            outcome=outcome,
            created_at=signal.created_at,
        )
        try:
            await self._decision_store.append(decision)
        except Exception:
            logger.exception("failed to journal decision %s", signal.signal_id)

    async def _on_fill(self, fill: Fill) -> None:
        # Journal before touching in-memory state: if the write fails, memory
        # must not be ahead of the persistent record that restart recovery
        # replays — losing the in-memory update is recoverable, the reverse
        # is silent divergence.
        if self._fill_store is not None:
            await self._fill_store.append(fill)
        self._portfolio.apply_fill(fill)
        self._risk_manager.on_fill(fill)
        self._fills.append(fill)
        if self._bus is not None:
            await self._bus.publish(FillRecorded(fill=fill))
        logger.info(
            "fill %s: %s %s %s @ %s (fee %s)",
            fill.client_order_id,
            fill.side,
            fill.quantity_base,
            fill.symbol,
            fill.price_quote,
            fill.fee_quote,
        )
