"""Position sizing and trade vetoes.

The risk manager owns the signal → order boundary. Sizing is stop-based:
risk at most ``risk_per_trade_fraction`` of current equity between entry and
stop, then cap by maximum position exposure and by what the free balance can
actually pay for (including a fee buffer). Any check that cannot pass vetoes
the trade by returning ``None`` — silently undersizing instead would hide
risk-limit pressure from the operator.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Order, OrderType, Side, Signal
from tradebot.portfolio import Portfolio


class RiskConfig(BaseModel):
    """Account-level risk limits; defaults are deliberately conservative."""

    model_config = ConfigDict(frozen=True)

    risk_per_trade_fraction: Decimal = Decimal("0.01")
    """Maximum fraction of equity lost if the entry's stop is hit."""

    max_position_fraction: Decimal = Decimal("0.25")
    """Maximum fraction of equity in any single position at entry."""

    fee_buffer_fraction: Decimal = Decimal("0.005")
    """Headroom kept when spending free balance, so fees can never overdraw."""


class RiskManager:
    """Turns signals into sized orders, or vetoes them.

    Exits are not vetoed: a proposal to close an open position always passes
    (capital protection must never be blocked by entry-oriented limits) and
    is sized to the full held quantity.

    Phase 1 scope (single-symbol backtests). Three known extensions arrive
    with the paper/live engine and are deliberately not faked here:
    ``evaluate`` will take a price map once portfolios hold multiple symbols;
    balance committed to resting orders will be subtracted from spendable
    once an order-state tracker exists; exchange lot-size/min-notional
    rounding stays in the execution engine's pre-submit checks
    (ARCHITECTURE.md 4.8) where the venue rules live.
    """

    def __init__(self, config: RiskConfig, portfolio: Portfolio) -> None:
        """Bind the limits to the portfolio whose equity they protect."""
        self._config = config
        self._portfolio = portfolio

    def evaluate(self, signal: Signal, current_price_quote: Decimal) -> Order | None:
        """Size ``signal`` against current equity, or return ``None`` to veto.

        ``current_price_quote`` must be positive; model-validated prices
        upstream guarantee it, so a violation here is a caller bug, not a veto.
        """
        if current_price_quote <= 0:
            raise ValueError(f"current price must be positive, got {current_price_quote}")
        if signal.side == Side.SELL:
            return self._size_exit(signal)
        return self._size_entry(signal, current_price_quote)

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

        equity = self._portfolio.equity_quote({signal.symbol: current_price_quote})
        risk_budget = equity * self._config.risk_per_trade_fraction
        quantity = risk_budget / stop_distance

        max_notional = equity * self._config.max_position_fraction
        quantity = min(quantity, max_notional / current_price_quote)

        spendable = self._portfolio.quote_balance * (1 - self._config.fee_buffer_fraction)
        quantity = min(quantity, spendable / current_price_quote)

        quantity = quantity.quantize(ACCOUNTING_RESOLUTION, rounding=ROUND_DOWN)
        if quantity <= 0:
            return None
        return Order(
            client_order_id=f"ord-{signal.signal_id}",
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity_base=quantity,
            created_at=signal.created_at,
        )
