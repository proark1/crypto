"""Metrics over a backtest result: the numbers a strategy lives or dies by.

Reports lead with the honest figures (ARCHITECTURE.md 6.3): net return after
fees, max drawdown, and performance against buy-and-hold — the benchmark a
strategy must beat to justify existing. ``buy_and_hold_return_fraction`` is
the pure price return of the same candles; strategy return is net of fees
and slippage, so the comparison is biased *against* the strategy, never for.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.backtest.runner import BacktestResult
from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, Side
from tradebot.portfolio import Portfolio


class BacktestReport(BaseModel):
    """Aggregate performance figures for one backtest run."""

    model_config = ConfigDict(frozen=True)

    initial_balance_quote: Decimal
    final_equity_quote: Decimal
    total_return_fraction: Decimal
    max_drawdown_fraction: Decimal
    total_fees_quote: Decimal
    round_trips: int
    winning_round_trips: int
    win_rate: float | None
    """Fraction of round trips with positive net PnL; ``None`` with no trades."""

    profit_factor: float | None
    """Gross profits / gross losses; ``None`` when there are no losing trades."""

    buy_and_hold_return_fraction: Decimal
    beats_buy_and_hold: bool


def build_report(
    result: BacktestResult,
    candles: Sequence[Candle],
    initial_balance_quote: Decimal,
) -> BacktestReport:
    """Compute the report for ``result`` produced over ``candles``."""
    if not candles:
        raise ValueError("cannot report on an empty candle series")

    total_return = _fraction(
        result.final_equity_quote - initial_balance_quote, initial_balance_quote
    )
    first_close = candles[0].close_quote
    last_close = candles[-1].close_quote
    buy_and_hold = _fraction(last_close - first_close, first_close)

    round_trip_pnls = _round_trip_pnls(result, initial_balance_quote)
    wins = [pnl for pnl in round_trip_pnls if pnl > 0]
    losses = [pnl for pnl in round_trip_pnls if pnl <= 0]
    gross_profit = sum(wins, Decimal(0))
    gross_loss = -sum(losses, Decimal(0))

    return BacktestReport(
        initial_balance_quote=initial_balance_quote,
        final_equity_quote=result.final_equity_quote,
        total_return_fraction=total_return,
        max_drawdown_fraction=_max_drawdown(result.equity_curve),
        total_fees_quote=sum((fill.fee_quote for fill in result.fills), Decimal(0)),
        round_trips=len(round_trip_pnls),
        winning_round_trips=len(wins),
        win_rate=len(wins) / len(round_trip_pnls) if round_trip_pnls else None,
        profit_factor=float(gross_profit / gross_loss) if gross_loss > 0 else None,
        buy_and_hold_return_fraction=buy_and_hold,
        beats_buy_and_hold=total_return > buy_and_hold,
    )


def _fraction(numerator: Decimal, denominator: Decimal) -> Decimal:
    return (numerator / denominator).quantize(ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN)


def _max_drawdown(equity_curve: tuple[tuple[object, Decimal], ...]) -> Decimal:
    """Largest peak-to-trough equity decline, as a fraction of the peak."""
    max_drawdown = Decimal(0)
    peak: Decimal | None = None
    for _, equity in equity_curve:
        if peak is None or equity > peak:
            peak = equity
        elif peak > 0:
            max_drawdown = max(max_drawdown, _fraction(peak - equity, peak))
    return max_drawdown


def _round_trip_pnls(result: BacktestResult, initial_balance_quote: Decimal) -> list[Decimal]:
    """Net PnL per closed round trip, by replaying fills through accounting.

    Reuses ``Portfolio`` rather than re-deriving fee/cost-basis rules: the
    report must agree with the books by construction, not by parallel math.
    """
    replay = Portfolio(initial_balance_quote)
    pnls: list[Decimal] = []
    previous_realized = Decimal(0)
    for fill in result.fills:
        replay.apply_fill(fill)
        if fill.side == Side.SELL:
            realized = replay.realized_pnl_quote()
            pnls.append(realized - previous_realized)
            previous_realized = realized
    return pnls
