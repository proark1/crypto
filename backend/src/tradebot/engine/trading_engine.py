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
from decimal import Decimal

from tradebot.authorization import ProposalQueue
from tradebot.core.events import CandleClosed, EventBus, FillRecorded, ProposalCreated
from tradebot.core.models import (
    AutonomyMode,
    Candle,
    CandleInterval,
    Decision,
    DecisionOutcome,
    Fill,
    Order,
    OrderType,
    Proposal,
    ProposalStatus,
    Side,
    Signal,
    utc_now,
)
from tradebot.execution.simulator import SimulatedExecutionAdapter
from tradebot.persistence import DecisionStore, FillStore, OpenOrder, OrderStore
from tradebot.portfolio import Portfolio
from tradebot.risk import CircuitBreakers, ManagedStop, RiskManager
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
        order_store: OrderStore | None = None,
        autonomy_mode: AutonomyMode = AutonomyMode.AUTONOMOUS,
        proposal_queue: ProposalQueue | None = None,
        entry_gates: tuple[EntryGate, ...] = (),
        signal_id_scope: str = "",
    ) -> None:
        """Wire the components; ``symbol=None`` binds to the first candle seen.

        ``entry_gates`` run on BUY signals only, in order, before
        authorization and risk sizing (the §5.2 pipeline). Exits are never
        gated: protective actions must not sit behind a filter.

        ``signal_id_scope`` prefixes the signal ids this engine synthesizes
        itself (kill switch, stop backstop). Strategy signals are scoped by
        the strategy; without this, two competition accounts stopping out
        on the same candle would mint the same order id and collide in the
        shared order journal. Empty for the production bot, whose id
        streams predate the competition.
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
        self._order_store = order_store
        # Stop-trigger latches already journaled, so each transition is
        # written once; restored triggered orders seed this set.
        self._journaled_triggered: set[str] = set()
        # Entry orders awaiting their fill, keyed by client_order_id: the
        # fill handler arms each one's protective exit plan. Restored
        # pending entries are re-registered by restore_order.
        self._submitted_entries: dict[str, Order] = {}
        self._autonomy_mode = autonomy_mode
        self._proposal_queue = proposal_queue
        self._entry_gates = entry_gates
        self._signal_id_scope = signal_id_scope
        self._fills: list[Fill] = []
        self._paused = False
        self._last_candle: Candle | None = None
        self._bus: EventBus | None = None
        # Protective stop state: armed when an entry fills. The ManagedStop
        # decides the level (breakeven lock, trail); a resting stop-limit in
        # the adapter enforces it, cancel/replaced as the level ratchets.
        # Management runs even while paused — capital protection is never
        # muted. ``_armed_entry`` is the plan-bearing entry order the
        # replacement orders are rebuilt from.
        self._managed_stop: ManagedStop | None = None
        self._armed_entry: Order | None = None
        # At most one exit order in flight: a stop breach and a strategy
        # exit on the same candle must not both sell the full position.
        self._pending_exit_order: str | None = None
        adapter.set_fill_handler(self._on_fill)

    @property
    def fills(self) -> tuple[Fill, ...]:
        """Every fill seen by this engine, in execution order."""
        return tuple(self._fills)

    @property
    def strategy_name(self) -> str:
        """The active signal generator's identifier, for status surfaces."""
        return self._strategy.name

    def open_orders(self) -> tuple[Order, ...]:
        """Orders currently held by the adapter (pending and resting)."""
        return self._adapter.open_orders()

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
        logger.info("strategy replaced for %s: %s -> %s", self._symbol, previous, strategy.name)

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
                # Journal-first, like fills: a cancelled order whose row
                # still says open would resurrect on the next restart.
                if self._order_store is not None:
                    await self._order_store.mark_cancelled(order.client_order_id, utc_now())
                await self._adapter.cancel(order.client_order_id)
                self._journaled_triggered.discard(order.client_order_id)
                self._submitted_entries.pop(order.client_order_id, None)
                self._risk_manager.on_order_cancelled(order.client_order_id)
                if order.client_order_id == self._pending_exit_order:
                    # The latched exit is one of the orders we just cancelled,
                    # so it is no longer in flight: clear the latch, or the
                    # "exit already in flight" check below would suppress the
                    # kill's own market flatten and leave the position open
                    # and unprotected.
                    self._pending_exit_order = None
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
        if self._pending_exit_order is not None:
            logger.warning("kill switch: an exit is already in flight; halted, awaiting its fill")
            return True
        last_close = self._last_candle.close_quote
        signal = Signal(
            signal_id=(
                f"{self._signal_id_scope}kill:{self._symbol}:"
                f"{self._last_candle.close_time.isoformat()}"
            ),
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
        await self._submit(exit_order)
        self._pending_exit_order = exit_order.client_order_id
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
        await self._journal_trigger_transitions()
        # Post-fill equity mark: the breakers must judge the same books the
        # strategy is about to see.
        self._risk_manager.on_candle(candle)
        await self._manage_protective_stop(candle)
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
        if signal.side == Side.SELL and self._pending_exit_order is not None:
            logger.info(
                "exit already in flight for %s; %s exit superseded",
                signal.symbol,
                signal.strategy_name,
            )
            await self._record_decision(signal, DecisionOutcome.SUPERSEDED)
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
        if order.side == Side.SELL:
            # The strategy exit replaces the protective stop: both SELL the
            # full position, so leaving the stop armed while the exit is in
            # flight could fill twice and go short.
            await self._cancel_protective_stops()
        await self._submit(order)
        if order.side == Side.SELL:
            self._pending_exit_order = order.client_order_id
        await self._record_decision(signal, DecisionOutcome.SUBMITTED)

    async def _submit(self, order: Order) -> None:
        """Journal the order intent as open, then hand it to the adapter.

        Journal-first, same ordering as fills: an order the adapter holds
        but the journal does not would vanish on restart, which is exactly
        the gap this store closes. A journal write failure therefore aborts
        the submission.
        """
        if self._order_store is not None:
            await self._order_store.record_submitted(order)
        await self._adapter.submit(order)
        if order.protective_exit is not None:
            self._submitted_entries[order.client_order_id] = order

    def _resting_protective_stops(self) -> tuple[Order, ...]:
        """Return the resting stop-limit exits (at most one with single positions)."""
        return tuple(
            order
            for order in self._adapter.open_orders()
            if order.order_type == OrderType.STOP_LIMIT and order.side == Side.SELL
        )

    async def _cancel_protective_stops(self) -> None:
        """Cancel resting protective stops, journal-first like the kill path."""
        for stop in self._resting_protective_stops():
            if self._order_store is not None:
                await self._order_store.mark_cancelled(stop.client_order_id, utc_now())
            await self._adapter.cancel(stop.client_order_id)
            self._journaled_triggered.discard(stop.client_order_id)
            logger.info("protective stop %s cancelled ahead of exit", stop.client_order_id)

    def resting_protective_stop(self) -> Order | None:
        """Return the resting protective stop order, if one is working."""
        resting = self._resting_protective_stops()
        return resting[0] if resting else None

    def has_resting_exit(self) -> bool:
        """Whether any SELL order (protective stop or exit) is working.

        Restart reconciliation re-protects a position only when nothing is:
        re-arming a stop next to a restored exit order would double-sell.
        """
        return any(order.side == Side.SELL for order in self._adapter.open_orders())

    async def submit_protective_stop(self, entry: Order, quantity_base: Decimal) -> None:
        """Re-protect an open position from its entry's persisted plan.

        Recovery path for the crash window between an entry fill and its
        stop placement; the normal path arms the stop inside the fill
        handler. Idempotent via the stop's deterministic client_order_id.
        """
        stop_order = self._risk_manager.protective_exit_order(entry, quantity_base, utc_now())
        await self._submit(stop_order)
        logger.warning(
            "re-armed protective stop %s for %s at %s",
            stop_order.client_order_id,
            stop_order.symbol,
            stop_order.stop_price_quote,
        )

    async def replay_gap_candle(self, candle: Candle) -> None:
        """Evaluate one missed candle against open orders only (boot recovery).

        Orders that were resting while the process was down must meet the
        candles that actually happened, not fill later at the post-restart
        price. Only the adapter sees the candle: the strategy must not emit
        signals for a market that has already moved on, and the stop level
        stays where the venue last knew it — nothing would have ratcheted a
        resting order while the bot was down. Fills run the full handler
        chain (journal, stop arming, portfolio, breaker streaks), exactly
        as they would have live.
        """
        if self._symbol is not None and candle.symbol != self._symbol:
            raise ValueError(f"engine is bound to {self._symbol}, got {candle.symbol}")
        self._last_candle = candle
        await self._adapter.process_candle(candle)
        await self._journal_trigger_transitions()

    def restore_order(self, open_order: OpenOrder) -> None:
        """Re-arm one persisted open order after a restart (recovery only).

        The order was journaled when first submitted, so it goes straight
        to the adapter without re-journaling; a restored trigger latch is
        adopted as already journaled.
        """
        self._adapter.restore_order(open_order.order, triggered=open_order.triggered)
        if open_order.triggered:
            self._journaled_triggered.add(open_order.order.client_order_id)
        if open_order.order.protective_exit is not None:
            # A restored pending entry must still arm its stop when it fills.
            self._submitted_entries[open_order.order.client_order_id] = open_order.order
        if (
            open_order.order.side == Side.SELL
            and open_order.order.order_type != OrderType.STOP_LIMIT
        ):
            # A restored non-stop exit (market/limit SELL still working after a
            # restart) is an exit in flight: re-adopt the latch so a fresh
            # strategy SELL is superseded instead of double-selling the
            # position short. Resting protective stops are deliberately left
            # out — a new strategy exit is meant to replace the stop, and the
            # SELL path cancels it before submitting (see _evaluate path).
            self._pending_exit_order = open_order.order.client_order_id
        logger.info(
            "restored open order %s: %s %s %s",
            open_order.order.client_order_id,
            open_order.order.side,
            open_order.order.order_type,
            open_order.order.symbol,
        )

    async def _journal_trigger_transitions(self) -> None:
        """Persist stop-limit trigger latches the adapter flipped this candle.

        Sorted for deterministic write order; the set comparison keeps this
        O(open stop-limits), which is hot-path safe because the set is tiny
        and almost always unchanged.
        """
        if self._order_store is None:
            return
        for order_id in sorted(self._adapter.triggered_order_ids() - self._journaled_triggered):
            await self._order_store.mark_triggered(order_id)
            self._journaled_triggered.add(order_id)

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
        await self._submit(order)
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

    def arm_managed_stop(self, stop: ManagedStop, entry: Order | None = None) -> None:
        """Arm (or re-arm) the managed stop level for the held position.

        Restart recovery: journal replay restores the position and any
        resting stop order, but the in-memory ratchet state is gone. With a
        plan-bearing ``entry`` the engine keeps cancel/replacing the resting
        order as the level ratchets; without one (approximate recovery of a
        plan-less position) the market-exit backstop enforces the level.
        """
        self._managed_stop = stop
        self._armed_entry = entry

    @property
    def protective_stop_quote(self) -> str | None:
        """The current stop level, for status surfaces; ``None`` unarmed."""
        stop = self._managed_stop
        return str(stop.stop_price_quote) if stop is not None else None

    async def _manage_protective_stop(self, candle: Candle) -> None:
        """Keep the resting stop-limit in line with the managed stop level.

        Runs after the adapter evaluated the candle — a breach is judged
        against the level the stop actually rested at, never one this
        candle's own high just ratcheted up — and before the strategy:
        protective management is never muted by pause and never depends on
        strategy logic being alive.

        The resting stop-limit order is the enforcement (gap-through risk
        included, as on a real venue). The market-exit backstop below only
        covers positions with no resting order to fall back on: a plan-less
        position recovered by approximation after a restart.
        """
        stop = self._managed_stop
        if stop is None or self._symbol is None:
            return
        if self._portfolio.position(self._symbol) is None:
            return  # already flat (e.g. kill switch); fill handler disarms
        if self._pending_exit_order is not None:
            return  # an exit is already on its way to the books
        resting = self._resting_protective_stops()
        gapped = tuple(
            order
            for order in resting
            if order.client_order_id in self._adapter.triggered_order_ids()
        )
        if gapped:
            # The native stop failed: it triggered but the market gapped
            # through its limit floor, leaving the order resting above the
            # market while the position bleeds below it. Bot-side market
            # exit is the §4.4 fallback for exactly this case.
            for order in gapped:
                if self._order_store is not None:
                    await self._order_store.mark_cancelled(order.client_order_id, candle.close_time)
                await self._adapter.cancel(order.client_order_id)
                self._journaled_triggered.discard(order.client_order_id)
            await self._backstop_exit(
                candle, stop, "stop-limit triggered but gapped through its limit"
            )
            return
        if not resting and stop.is_breached_by(candle):
            await self._backstop_exit(candle, stop, "no resting stop to enforce it")
            return
        previous_level = stop.stop_price_quote
        ratcheted = stop.ratchet(candle)
        if ratcheted == previous_level or self._armed_entry is None or not resting:
            return
        # The level moved: cancel/replace the resting order so the venue
        # enforces the ratcheted stop, not the stale one. Same deterministic
        # id; its journal row reopens carrying the new level.
        for stale in resting:
            if self._order_store is not None:
                await self._order_store.mark_cancelled(stale.client_order_id, candle.close_time)
            await self._adapter.cancel(stale.client_order_id)
            self._journaled_triggered.discard(stale.client_order_id)
        replacement = self._risk_manager.protective_exit_order(
            self._armed_entry,
            resting[0].quantity_base,
            candle.close_time,
            stop_price_quote=ratcheted,
        )
        await self._submit(replacement)
        logger.info(
            "protective stop for %s ratcheted %s -> %s",
            self._symbol,
            previous_level,
            ratcheted,
        )

    async def _backstop_exit(self, candle: Candle, stop: ManagedStop, why: str) -> None:
        """Exit at market when the resting stop cannot protect the position.

        The §4.4 fallback: bot-side stop logic only where the native resting
        order failed (gapped through) or never existed (plan-less recovery).
        The exit goes through the risk manager like every order.
        """
        signal = Signal(
            signal_id=f"{self._signal_id_scope}stop:{self._symbol}:{candle.close_time.isoformat()}",
            strategy_name="protective_stop",
            symbol=self._symbol or candle.symbol,
            side=Side.SELL,
            confidence=1.0,
            stop_price_quote=stop.stop_price_quote,
            reasons=(
                f"protective stop at {stop.stop_price_quote} traded through "
                f"(candle low {candle.low_quote}); {why}",
            ),
            created_at=candle.close_time,
        )
        self._managed_stop = None  # one exit order; never resubmit per candle
        self._armed_entry = None
        exit_order = self._risk_manager.evaluate(signal, candle.close_quote)
        if exit_order is None:  # pragma: no cover - exits always pass; defensive
            logger.error("protective stop exit was vetoed; position unprotected")
            return
        await self._submit(exit_order)
        self._pending_exit_order = exit_order.client_order_id
        logger.warning(
            "protective stop hit for %s: exiting %s at market (%s)",
            self._symbol,
            exit_order.quantity_base,
            why,
        )
        await self._record_decision(signal, DecisionOutcome.SUBMITTED)

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
        # An order still held by the adapter filled only partially; its row
        # stays open (with the fill journal recording the filled part) so a
        # restart restores exactly the remainder.
        completed = all(
            open_order.client_order_id != fill.client_order_id
            for open_order in self._adapter.open_orders()
        )
        if self._order_store is not None and completed:
            # After the fill journal on purpose: if this write is lost, the
            # quantity-aware guard in fetch_open still keeps a fully filled
            # order out of restoration — fills outrank the order row.
            await self._order_store.mark_filled(fill.client_order_id, fill.filled_at)
        self._journaled_triggered.discard(fill.client_order_id)
        entry = self._submitted_entries.get(fill.client_order_id)
        if completed:
            self._submitted_entries.pop(fill.client_order_id, None)
        self._portfolio.apply_fill(fill)
        self._risk_manager.on_fill(fill)
        self._fills.append(fill)
        plan = None if entry is None else entry.protective_exit
        if entry is not None and plan is not None:
            position = self._portfolio.position(fill.symbol)
            if position is not None:
                # The position exists from this fill on; its protection goes
                # to the venue immediately (CLAUDE.md invariant 5), sized to
                # the whole position so each partial fill re-arms the stop
                # with the cumulative quantity at the true average entry. A
                # crash from here is covered by boot reconciliation.
                await self._cancel_protective_stops()
                stop_order = self._risk_manager.protective_exit_order(
                    entry, position.quantity_base, fill.filled_at
                )
                await self._submit(stop_order)
                self._managed_stop = ManagedStop(
                    entry_price_quote=position.average_entry_price_quote,
                    initial_stop_quote=plan.stop_price_quote,
                    breakeven_at_r=plan.breakeven_at_r,
                    trail_distance_quote=plan.trail_distance_quote,
                )
                self._armed_entry = entry
                logger.info(
                    "protective stop %s armed for %s: trigger %s, limit %s, quantity %s",
                    stop_order.client_order_id,
                    fill.symbol,
                    stop_order.stop_price_quote,
                    stop_order.limit_price_quote,
                    stop_order.quantity_base,
                )
        if fill.side == Side.SELL:
            if fill.client_order_id == self._pending_exit_order:
                self._pending_exit_order = None
            if self._portfolio.position(fill.symbol) is None:
                # Flat again: nothing left to protect or ratchet.
                self._managed_stop = None
                self._armed_entry = None
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
