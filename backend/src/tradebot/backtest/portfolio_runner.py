"""Account-level backtests: many symbols, one shared book and one brake.

The single-symbol :class:`~tradebot.backtest.runner.BacktestRunner` proves a
strategy; this runner proves the *account*: per-coin caps alone understate
crypto risk (RiskConfig treats open positions as one fully correlated
block), so exposure ceilings, shared circuit breakers, and balance
contention only show their behavior when several symbols compete for the
same equity — exactly how the paper worker trades.

The wiring mirrors the worker one to one: one strategy instance and one
adapter per symbol, one portfolio and one risk manager for the account.
Candles interleave in (open time, symbol) order, so simultaneous candles
process deterministically and a tie always resolves the same way.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal

from tradebot.backtest.runner import BacktestResult
from tradebot.core.models import Candle
from tradebot.engine import TradingEngine
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskManager
from tradebot.strategies import Strategy


class PortfolioBacktestResult(BacktestResult):
    """Account-level outcome: everything per-run, plus what one book lacks."""

    exposure_curve: tuple[tuple[datetime, Decimal], ...]
    """Total open-position notional (quote) at each candle close, marked at
    the same last-seen prices the equity curve uses."""

    realized_pnl_by_symbol: dict[str, Decimal]
    """Net realized PnL per traded symbol (fees included)."""


class PortfolioBacktestRunner:
    """Drives one strategy per symbol through one shared account."""

    def __init__(
        self,
        strategy_factory: Callable[[], Strategy],
        risk_manager: RiskManager,
        portfolio: Portfolio,
        fill_config: FillSimulatorConfig | None = None,
    ) -> None:
        """Wire the shared account; engines are built per symbol seen.

        ``strategy_factory`` returns a *fresh* strategy instance per call —
        indicator state must never bleed across symbols. The risk manager
        and portfolio are shared on purpose: account-level limits are the
        point of this runner.
        """
        self._strategy_factory = strategy_factory
        self._risk_manager = risk_manager
        self._portfolio = portfolio
        self._fill_config = fill_config if fill_config is not None else FillSimulatorConfig()
        self._engines: dict[str, TradingEngine] = {}
        self._consumed = False

    async def run(self, candles: Sequence[Candle]) -> PortfolioBacktestResult:
        """Replay ``candles`` (any symbol mix) and report the account outcome.

        Single-use, like the single-symbol runner: every component carries
        state from the run. The equity curve is marked at every candle
        close across all symbols, each position valued at its last seen
        close — the same marks the shared risk manager sizes against.
        """
        if self._consumed:
            raise RuntimeError("PortfolioBacktestRunner is single-use; build a fresh one per run")
        self._consumed = True
        if not candles:
            raise ValueError("cannot backtest an empty candle series")
        ordered = sorted(candles, key=lambda candle: (candle.open_time, candle.symbol))

        marks: dict[str, Decimal] = {}
        # Keyed by close time: several symbols closing the same minute must
        # yield one equity point — the value once every book is marked —
        # not one stale-marked point per symbol. Insertion order is
        # chronological because the sort gives non-decreasing close times.
        equity_by_close: dict[datetime, Decimal] = {}
        exposure_by_close: dict[datetime, Decimal] = {}
        for candle in ordered:
            engine = self._engines.get(candle.symbol)
            if engine is None:
                engine = TradingEngine(
                    self._strategy_factory(),
                    self._risk_manager,
                    self._portfolio,
                    SimulatedExecutionAdapter(self._fill_config),
                    symbol=candle.symbol,
                )
                self._engines[candle.symbol] = engine
            await engine.process_candle(candle)
            marks[candle.symbol] = candle.close_quote
            equity_by_close[candle.close_time] = self._portfolio.equity_quote(marks)
            exposure_by_close[candle.close_time] = sum(
                (
                    position.quantity_base * marks[symbol]
                    for symbol, position in self._portfolio.positions.items()
                    if symbol in marks
                ),
                Decimal(0),
            )

        # Per-engine fills are already time-ordered; the merge key adds the
        # symbol and order id so simultaneous fills order deterministically.
        fills = sorted(
            (fill for engine in self._engines.values() for fill in engine.fills),
            key=lambda fill: (fill.filled_at, fill.symbol, fill.client_order_id),
        )
        equity_curve = tuple(equity_by_close.items())
        return PortfolioBacktestResult(
            fills=tuple(fills),
            equity_curve=equity_curve,
            final_equity_quote=equity_curve[-1][1],
            realized_pnl_quote=self._portfolio.realized_pnl_quote(),
            exposure_curve=tuple(exposure_by_close.items()),
            realized_pnl_by_symbol=dict(self._portfolio.realized_pnl_by_symbol()),
        )
