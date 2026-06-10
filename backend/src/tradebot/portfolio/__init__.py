"""Portfolio accounting: positions, balances, and PnL.

The single source of truth for what the bot holds (ARCHITECTURE.md 4.5).
All amounts are ``Decimal`` in the configured quote currency or the base
asset — exact arithmetic, no rounding; presentation layers round for display.
Postgres persistence wraps this state in a later PR; the accounting rules
live here so backtest, paper, and live all share them.
"""

from tradebot.portfolio.accounting import Portfolio, Position

__all__ = ["Portfolio", "Position"]
