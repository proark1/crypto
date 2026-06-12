"""Position sizing and trade vetoes.

The risk manager owns the signal → order boundary. Sizing is stop-based:
risk at most ``risk_per_trade_fraction`` of current equity between entry and
stop, then cap by maximum position exposure and by what the free balance can
actually pay for (including a fee buffer). Any check that cannot pass vetoes
the trade by returning ``None`` — silently undersizing instead would hide
risk-limit pressure from the operator.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.logging import log_event
from tradebot.core.models import (
    ACCOUNTING_RESOLUTION,
    Candle,
    Fill,
    Order,
    OrderType,
    ProtectiveExitPlan,
    Side,
    Signal,
    SymbolFilters,
)
from tradebot.portfolio import Portfolio
from tradebot.risk.breakers import BreakerConfig, CircuitBreakers

logger = logging.getLogger(__name__)


class RiskConfig(BaseModel):
    """Account-level risk limits; defaults are deliberately conservative."""

    model_config = ConfigDict(frozen=True)

    risk_per_trade_fraction: Decimal = Decimal("0.01")
    """Maximum fraction of equity lost if the entry's stop is hit."""

    max_position_fraction: Decimal = Decimal("0.25")
    """Maximum fraction of equity in any single position at entry."""

    max_total_exposure_fraction: Decimal = Decimal("0.5")
    """Maximum fraction of equity across *all* open positions at entry.

    Correlation-aware in the conservative limit: crypto spot positions are
    treated as one fully correlated block (majors routinely draw down
    together), so per-coin caps alone understate account risk. A new entry
    must fit under this account-wide ceiling however many coins are open;
    estimating pairwise correlations to loosen it is deliberately out of
    scope — a wrong correlation estimate fails toward more risk."""

    fee_buffer_fraction: Decimal = Decimal("0.005")
    """Headroom kept when spending free balance, so fees can never overdraw."""

    protective_stop_limit_offset_fraction: Decimal = Decimal("0.005")
    """How far below the stop trigger the stop-limit's limit price sits.

    Wide enough that an ordinary fast candle still fills, tight enough to
    bound how far past the invalidation level a gap can execute. Gapping
    through the limit leaves the order resting (real exchange behavior) —
    the unfilled-stop tail risk stays visible instead of being papered over.
    """

    breakers: BreakerConfig = BreakerConfig()
    """Account-level circuit breakers (daily loss, drawdown, streaks, caps)."""


class RiskManager:
    """Turns signals into sized orders, or vetoes them.

    Exits are not vetoed: a proposal to close an open position always passes
    (capital protection must never be blocked by entry-oriented limits) and
    is sized to the full held quantity.

    One manager serves every symbol's engine: the breakers and equity caps
    are account-level, so they must see all positions through one pair of
    eyes. Other symbols' positions are marked at their last seen close
    (cached in :meth:`on_candle`).

    Venue rules (lot step, minimum quantity/notional, price tick) are
    applied here, at order construction — the one gate every order passes
    (CLAUDE.md invariant 4) — from the per-symbol filters the worker fills
    out of the exchange catalog. Notional committed to submitted-but-
    unfilled entries counts against exposure and spendable balance, so two
    same-candle entries can never both claim the same headroom.
    """

    def __init__(
        self,
        config: RiskConfig,
        portfolio: Portfolio,
        filters_by_symbol: Mapping[str, SymbolFilters] | None = None,
    ) -> None:
        """Bind the limits to the portfolio whose equity they protect.

        ``filters_by_symbol`` is a live view of each pair's venue rules
        (the worker fills it from the exchange's market catalog and keeps
        it updated as coins are added); absent or empty means unconstrained
        — every backtest runs that way, so research needs no venue lookup.
        """
        self._config = config
        self._portfolio = portfolio
        self._filters_by_symbol: Mapping[str, SymbolFilters] = (
            filters_by_symbol if filters_by_symbol is not None else {}
        )
        self._breakers = CircuitBreakers(config.breakers)
        self._observed_realized_pnl: dict[str, Decimal] = {}
        self._open_round_trip_pnl: dict[str, Decimal] = {}
        # Notional committed to submitted-but-unfilled entries, keyed by
        # client_order_id: counted against exposure and spendable balance
        # so two same-candle entries cannot both claim the same headroom.
        self._committed_entries_quote: dict[str, Decimal] = {}
        self._last_price_quote: dict[str, Decimal] = {}

    @property
    def breakers(self) -> CircuitBreakers:
        """Breaker state, for status reporting and the operator reset."""
        return self._breakers

    def reset(self) -> None:
        """Clear all account-level state for a capital reset.

        Fresh breakers (no trip, cooldown, day anchor, or peak) and no
        committed/observed tracking. The portfolio it guards is reset
        separately by the caller. For an operator account reset only — never
        part of normal trading.
        """
        self._breakers = CircuitBreakers(self._config.breakers)
        self._observed_realized_pnl = {}
        self._open_round_trip_pnl = {}
        self._committed_entries_quote = {}
        self._last_price_quote = {}

    def on_candle(self, candle: Candle) -> None:
        """Feed one closed candle's equity mark to the circuit breakers.

        Called by the engine after fills are applied, so the breakers see
        the same post-fill equity in backtest, paper, and live. One manager
        is shared by every symbol's engine (the breakers are account-level),
        so the latest close per symbol is cached to mark all open positions;
        until every open position has a mark — e.g. right after a restart
        with positions restored but no candles yet — the observation is
        skipped rather than computed on a guessed price.
        """
        self._last_price_quote[candle.symbol] = candle.close_quote
        marks = self._marks_for_open_positions()
        if marks is None:
            logger.warning(
                "breaker equity observation skipped: an open position has no mark price yet"
            )
            return
        self._breakers.observe(candle.close_time, self._portfolio.equity_quote(marks))

    def rebase_realized_pnl(self) -> None:
        """Adopt the portfolio's current realized PnL as the streak baseline.

        Called after journal replay on restart: history replayed into the
        portfolio is not a fresh round trip, and the first real trade after
        a restart must not inherit the account's lifetime PnL as its result.
        """
        for symbol, realized in self._portfolio.realized_pnl_by_symbol().items():
            self._observed_realized_pnl[symbol] = realized

    def _marks_for_open_positions(self) -> dict[str, Decimal] | None:
        """Return last-known prices covering every open position, else ``None``."""
        marks: dict[str, Decimal] = {}
        for symbol in self._portfolio.positions:
            price = self._last_price_quote.get(symbol)
            if price is None:
                return None
            marks[symbol] = price
        return marks

    def on_order_cancelled(self, client_order_id: str) -> None:
        """Release the committed notional of a cancelled entry (kill switch)."""
        self._committed_entries_quote.pop(client_order_id, None)

    def on_fill(self, fill: Fill) -> None:
        """Feed one applied fill to the loss-streak tracker.

        A buy fill also releases its entry's committed notional: from the
        fill on, the exposure lives in the position, not the order.

        Sells realize PnL, but one exit order can fill in several parts — a
        round trip is only complete (and only counts once toward the loss
        streak) when the position is fully closed, so partial fills of one
        losing exit can never be miscounted as a streak of losses.
        """
        if fill.side != Side.SELL:
            self._committed_entries_quote.pop(fill.client_order_id, None)
            return
        realized = self._portfolio.realized_pnl_quote(fill.symbol)
        delta = realized - self._observed_realized_pnl.get(fill.symbol, Decimal(0))
        self._observed_realized_pnl[fill.symbol] = realized
        self._open_round_trip_pnl[fill.symbol] = (
            self._open_round_trip_pnl.get(fill.symbol, Decimal(0)) + delta
        )
        if self._portfolio.position(fill.symbol) is None:
            round_trip_pnl = self._open_round_trip_pnl.pop(fill.symbol)
            self._breakers.record_closed_trade(round_trip_pnl, fill.filled_at)

    def evaluate(self, signal: Signal, current_price_quote: Decimal) -> Order | None:
        """Size ``signal`` against current equity, or return ``None`` to veto.

        ``current_price_quote`` must be positive; model-validated prices
        upstream guarantee it, so a violation here is a caller bug, not a veto.
        """
        if current_price_quote <= 0:
            raise ValueError(f"current price must be positive, got {current_price_quote}")
        if signal.side == Side.SELL:
            return self._size_exit(signal)
        block_reason = self._breakers.entry_block_reason(signal.created_at)
        if block_reason is not None:
            # Entries only: exits returned above so capital protection can
            # never be blocked by an account-level brake.
            log_event(
                logger,
                logging.WARNING,
                "entry_vetoed",
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                reason=block_reason,
            )
            return None
        order = self._size_entry(signal, current_price_quote)
        if order is not None:
            self._breakers.record_entry(signal.created_at)
        return order

    def _size_exit(self, signal: Signal) -> Order | None:
        position = self._portfolio.position(signal.symbol)
        if position is None:
            return None  # nothing to close; stale signal
        return Order(
            client_order_id=f"ord-{signal.signal_id}",
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity_base=position.quantity_base,
            created_at=signal.created_at,
        )

    def _size_entry(self, signal: Signal, current_price_quote: Decimal) -> Order | None:
        if self._portfolio.position(signal.symbol) is not None:
            return None  # one position per symbol; pyramiding is not supported
        stop_distance = current_price_quote - signal.stop_price_quote
        if stop_distance <= 0:
            return None  # stop above price: no defined risk per unit

        # Equity is account-wide: other symbols' open positions are marked
        # at their last seen close. No mark for an open position means the
        # account cannot be valued — veto rather than size on a guess.
        marks = self._marks_for_open_positions()
        if marks is None:
            logger.warning(
                "entry vetoed for %s: an open position has no mark price yet", signal.symbol
            )
            return None
        marks[signal.symbol] = current_price_quote
        equity = self._portfolio.equity_quote(marks)
        risk_budget = equity * self._config.risk_per_trade_fraction
        quantity = risk_budget / stop_distance

        max_notional = equity * self._config.max_position_fraction
        quantity = min(quantity, max_notional / current_price_quote)

        # Account-wide exposure cap: open positions are assumed fully
        # correlated (see RiskConfig), so the new entry only gets whatever
        # headroom they leave. Vetoing at zero headroom is deliberate and
        # loud — silently sizing to dust would hide the pressure.
        open_notional_quote = sum(
            (
                position.quantity_base * marks[symbol]
                for symbol, position in self._portfolio.positions.items()
            ),
            Decimal(0),
        ) + sum(self._committed_entries_quote.values(), Decimal(0))
        exposure_headroom_quote = (
            equity * self._config.max_total_exposure_fraction - open_notional_quote
        )
        if exposure_headroom_quote <= 0:
            logger.warning(
                "entry vetoed for %s: open positions hold %s quote of a %s total exposure budget",
                signal.symbol,
                open_notional_quote,
                equity * self._config.max_total_exposure_fraction,
            )
            return None
        quantity = min(quantity, exposure_headroom_quote / current_price_quote)

        committed = sum(self._committed_entries_quote.values(), Decimal(0))
        spendable = (self._portfolio.quote_balance - committed) * (
            1 - self._config.fee_buffer_fraction
        )
        if spendable <= 0:
            logger.warning(
                "entry vetoed for %s: balance fully committed to unfilled entries",
                signal.symbol,
            )
            return None
        quantity = min(quantity, spendable / current_price_quote)

        quantity = quantity.quantize(ACCOUNTING_RESOLUTION, rounding=ROUND_DOWN)
        filters = self._filters_by_symbol.get(signal.symbol)
        if filters is not None:
            # Venue rules last, after every risk cap: aligning down only
            # shrinks, so no cap can be re-breached by rounding.
            quantity = filters.align_quantity(quantity)
            if quantity <= 0:
                return None
            block_reason = filters.entry_block_reason(quantity, current_price_quote)
            if block_reason is not None:
                log_event(
                    logger,
                    logging.WARNING,
                    "entry_vetoed",
                    signal_id=signal.signal_id,
                    symbol=signal.symbol,
                    strategy_name=signal.strategy_name,
                    reason=block_reason,
                )
                return None
        if quantity <= 0:
            return None
        # Commit the notional the moment the order exists: the engine
        # always submits an approved order, and a same-candle entry on
        # another coin must see this one's claim on the account.
        self._committed_entries_quote[f"ord-{signal.signal_id}"] = quantity * current_price_quote
        return Order(
            client_order_id=f"ord-{signal.signal_id}",
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity_base=quantity,
            protective_exit=self._plan_protective_exit(signal),
            created_at=signal.created_at,
        )

    def _plan_protective_exit(self, signal: Signal) -> ProtectiveExitPlan:
        """Turn the signal's invalidation level into an exchange stop-limit plan.

        The initial trigger is the invalidation level itself — the same
        price the position was sized against, so enforced risk equals sized
        risk. The limit floor sits ``protective_stop_limit_offset_fraction``
        below it, and the signal's ratchet policy (breakeven, trail) rides
        along so it survives restarts with the entry order.
        """
        return ProtectiveExitPlan(
            stop_price_quote=signal.stop_price_quote,
            limit_price_quote=self._stop_limit_floor(signal.stop_price_quote),
            breakeven_at_r=signal.breakeven_at_r,
            trail_distance_quote=signal.trail_distance_quote,
        )

    def _stop_limit_floor(self, trigger_quote: Decimal) -> Decimal:
        """Return the limit price a stop-limit rests with for a given trigger."""
        return (trigger_quote * (1 - self._config.protective_stop_limit_offset_fraction)).quantize(
            ACCOUNTING_RESOLUTION, rounding=ROUND_DOWN
        )

    def protective_exit_order(
        self,
        entry: Order,
        quantity_base: Decimal,
        at: datetime,
        stop_price_quote: Decimal | None = None,
    ) -> Order:
        """Construct the resting stop-limit that protects ``entry``'s position.

        Called by the engine when the entry fills (quantity = the filled
        amount), per ratchet when the managed level moves
        (``stop_price_quote`` = the new level, limit floor re-derived at the
        configured offset), and by restart reconciliation when a crash left
        a position without its stop. Deterministic id per entry, so
        re-arming or replacing after a disconnect is idempotent. Raises
        ``ValueError`` if the entry carries no plan — silently leaving a
        position unprotected is the failure mode this method exists to end.
        """
        if entry.protective_exit is None:
            raise ValueError(f"entry order {entry.client_order_id!r} has no protective exit plan")
        trigger = (
            entry.protective_exit.stop_price_quote if stop_price_quote is None else stop_price_quote
        )
        limit = (
            entry.protective_exit.limit_price_quote
            if stop_price_quote is None
            else self._stop_limit_floor(trigger)
        )
        filters = self._filters_by_symbol.get(entry.symbol)
        if filters is not None:
            # Down-tick both prices: a sell stop rounded up could trigger
            # above the level the position was sized against.
            trigger = filters.align_price_down(trigger)
            limit = filters.align_price_down(limit)
        return Order(
            client_order_id=f"stop-{entry.client_order_id}",
            signal_id=entry.signal_id,
            symbol=entry.symbol,
            side=Side.SELL,
            order_type=OrderType.STOP_LIMIT,
            quantity_base=quantity_base,
            stop_price_quote=trigger,
            limit_price_quote=limit,
            created_at=at,
        )
