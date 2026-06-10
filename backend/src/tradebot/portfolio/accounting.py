"""Spot position and balance accounting from fills.

Fee convention (documented here once, relied on everywhere): buy fees are
capitalized into the position's cost basis; sell fees are subtracted from
realized PnL. Equity therefore always equals
``initial balance + total realized PnL + unrealized PnL`` with no fee terms
left dangling.

The portfolio records fills that *happened* — it is bookkeeping, not
permission. A fill that sells more than is held or spends more quote than the
free balance indicates a reconciliation failure upstream and raises
immediately; accounting that silently goes negative would hide exactly the
errors it exists to surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import ACCOUNTING_RESOLUTION, Fill, Side


class Position(BaseModel):
    """An open spot holding in one symbol.

    ``cost_basis_quote`` includes capitalized buy fees, so
    ``average_entry_price_quote`` reflects the true break-even price.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    quantity_base: Decimal
    cost_basis_quote: Decimal

    @property
    def average_entry_price_quote(self) -> Decimal:
        """Fee-inclusive average entry price (cost basis / quantity)."""
        return self.cost_basis_quote / self.quantity_base

    def unrealized_pnl_quote(self, current_price_quote: Decimal) -> Decimal:
        """Mark-to-market PnL at ``current_price_quote``, before sell fees."""
        return current_price_quote * self.quantity_base - self.cost_basis_quote


class Portfolio:
    """Mutable account state, advanced one fill at a time.

    Single quote currency by design (ARCHITECTURE.md 4.5): every symbol this
    portfolio sees must be quoted in the currency the account is funded with.
    """

    def __init__(self, initial_balance_quote: Decimal) -> None:
        """Start with ``initial_balance_quote`` free quote currency (>= 0)."""
        if initial_balance_quote < 0:
            raise ValueError("initial balance cannot be negative")
        self._quote_balance = initial_balance_quote
        self._positions: dict[str, Position] = {}
        self._realized_pnl: dict[str, Decimal] = {}

    @property
    def quote_balance(self) -> Decimal:
        """Free quote currency available for new buys."""
        return self._quote_balance

    @property
    def positions(self) -> Mapping[str, Position]:
        """Read-only view of open positions by symbol."""
        return dict(self._positions)

    def position(self, symbol: str) -> Position | None:
        """Return the open position in ``symbol``, or ``None`` if flat."""
        return self._positions.get(symbol)

    def realized_pnl_quote(self, symbol: str | None = None) -> Decimal:
        """Cumulative realized PnL, for one symbol or the whole account."""
        if symbol is not None:
            return self._realized_pnl.get(symbol, Decimal(0))
        return sum(self._realized_pnl.values(), Decimal(0))

    def equity_quote(self, current_prices_quote: Mapping[str, Decimal]) -> Decimal:
        """Account value: free balance plus open positions marked to market.

        Requires a price for every open symbol — guessing a mark price would
        make equity (and every risk limit derived from it) quietly wrong.
        """
        equity = self._quote_balance
        for symbol, position in self._positions.items():
            if symbol not in current_prices_quote:
                raise ValueError(f"no current price provided for open position {symbol}")
            equity += position.quantity_base * current_prices_quote[symbol]
        return equity

    def apply_fill(self, fill: Fill) -> None:
        """Update balances, position, and realized PnL for one fill."""
        if fill.side == Side.BUY:
            self._apply_buy(fill)
        else:
            self._apply_sell(fill)

    def _apply_buy(self, fill: Fill) -> None:
        total_cost = fill.price_quote * fill.quantity_base + fill.fee_quote
        if total_cost > self._quote_balance:
            raise ValueError(
                f"buy fill costs {total_cost} but only {self._quote_balance} "
                f"{fill.symbol} quote balance is free — upstream reconciliation error"
            )
        self._quote_balance -= total_cost
        existing = self._positions.get(fill.symbol)
        if existing is None:
            self._positions[fill.symbol] = Position(
                symbol=fill.symbol,
                quantity_base=fill.quantity_base,
                cost_basis_quote=total_cost,
            )
        else:
            self._positions[fill.symbol] = Position(
                symbol=fill.symbol,
                quantity_base=existing.quantity_base + fill.quantity_base,
                cost_basis_quote=existing.cost_basis_quote + total_cost,
            )

    def _apply_sell(self, fill: Fill) -> None:
        existing = self._positions.get(fill.symbol)
        if existing is None or fill.quantity_base > existing.quantity_base:
            held = existing.quantity_base if existing is not None else Decimal(0)
            raise ValueError(
                f"sell fill for {fill.quantity_base} {fill.symbol} exceeds held {held} "
                f"— spot cannot go short; upstream reconciliation error"
            )
        proceeds = fill.price_quote * fill.quantity_base - fill.fee_quote
        if fill.quantity_base == existing.quantity_base:
            # Full close consumes the entire cost basis exactly — deriving it
            # via the (rounded) average price would leak a Decimal residual.
            cost_of_sold = existing.cost_basis_quote
        else:
            cost_of_sold = (
                existing.cost_basis_quote * fill.quantity_base / existing.quantity_base
            ).quantize(ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN)
        self._quote_balance += proceeds
        self._realized_pnl[fill.symbol] = (
            self._realized_pnl.get(fill.symbol, Decimal(0)) + proceeds - cost_of_sold
        )
        remaining = existing.quantity_base - fill.quantity_base
        if remaining == 0:
            del self._positions[fill.symbol]
        else:
            self._positions[fill.symbol] = Position(
                symbol=fill.symbol,
                quantity_base=remaining,
                cost_basis_quote=existing.cost_basis_quote - cost_of_sold,
            )
