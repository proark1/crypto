"""Account-level figures over a multi-symbol run: what one book cannot show.

The per-run :mod:`tradebot.backtest.report` judges a strategy; this report
judges the *account* (ARCHITECTURE.md §12, item 12 of the review): how hard
the equity worked (exposure), how much it churned (turnover), how deep it
fell (drawdown on the account curve), and which coins carried or bled it.
Composed per walk-forward window by the caller — the split logic in
:mod:`tradebot.backtest.walkforward` already yields the candle windows, and
one report per window is the account-level walk-forward story.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.backtest.portfolio_runner import PortfolioBacktestResult


class AccountReport(BaseModel):
    """Aggregate account figures for one multi-symbol run or window."""

    model_config = ConfigDict(frozen=True)

    initial_balance_quote: Decimal
    final_equity_quote: Decimal
    total_return_fraction: Decimal
    max_drawdown_fraction: Decimal
    calmar_ratio: float | None
    """Account return over max drawdown — return per unit of worst
    peak-to-trough pain on the account curve. ``None`` when the account never
    drew down (no division by zero)."""

    total_fees_quote: Decimal
    turnover_quote: Decimal
    """Total traded notional (both sides), the churn fees scale with."""

    turnover_ratio: Decimal
    """Turnover over the initial balance: how many times the account traded
    its own size during the window."""

    average_exposure_fraction: Decimal
    """Mean open-position notional as a fraction of equity, per candle
    close — how hard the capital actually worked."""

    peak_exposure_fraction: Decimal
    """The high-water exposure fraction; compare against the configured
    ``max_total_exposure_fraction`` ceiling."""

    fills_by_symbol: dict[str, int]
    realized_pnl_by_symbol: dict[str, Decimal]
    """Which coins carried the account and which bled it (fees included)."""


def build_account_report(
    result: PortfolioBacktestResult, initial_balance_quote: Decimal
) -> AccountReport:
    """Compute the account report for one portfolio-runner result."""
    if initial_balance_quote <= 0:
        raise ValueError(f"initial balance must be positive, got {initial_balance_quote}")

    peak = initial_balance_quote
    max_drawdown = Decimal(0)
    exposure_fractions: list[Decimal] = []
    exposure_at = dict(result.exposure_curve)
    for at, equity in result.equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
        if equity > 0:
            exposure_fractions.append(exposure_at.get(at, Decimal(0)) / equity)

    turnover = sum((fill.price_quote * fill.quantity_base for fill in result.fills), Decimal(0))
    fills_by_symbol: dict[str, int] = {}
    for fill in result.fills:
        fills_by_symbol[fill.symbol] = fills_by_symbol.get(fill.symbol, 0) + 1
    point_count = len(exposure_fractions)
    total_return = (result.final_equity_quote - initial_balance_quote) / initial_balance_quote
    return AccountReport(
        initial_balance_quote=initial_balance_quote,
        final_equity_quote=result.final_equity_quote,
        total_return_fraction=total_return,
        max_drawdown_fraction=max_drawdown,
        calmar_ratio=float(total_return / max_drawdown) if max_drawdown > 0 else None,
        total_fees_quote=sum((fill.fee_quote for fill in result.fills), Decimal(0)),
        turnover_quote=turnover,
        turnover_ratio=turnover / initial_balance_quote,
        average_exposure_fraction=(
            sum(exposure_fractions, Decimal(0)) / point_count if point_count else Decimal(0)
        ),
        peak_exposure_fraction=max(exposure_fractions, default=Decimal(0)),
        fills_by_symbol=fills_by_symbol,
        realized_pnl_by_symbol=dict(result.realized_pnl_by_symbol),
    )


__all__ = ["AccountReport", "build_account_report"]
