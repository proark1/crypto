"""The backtest loop: candles through strategy, risk, simulator, portfolio.

This is the one-code-path invariant working end to end: the strategy and the
risk manager run here exactly as they will under paper and live adapters —
only the execution adapter differs. Per candle the order of operations is
fixed and deterministic:

1. the simulator evaluates open orders against the new candle (orders placed
   on the previous close fill no earlier than this open);
2. fills update the portfolio;
3. the strategy sees the closed candle and the *post-fill* position;
4. a resulting signal is sized or vetoed by the risk manager and submitted;
5. equity is marked at the candle close.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import Candle, Fill
from tradebot.execution import SimulatedExecutionAdapter
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskManager
from tradebot.strategies import Strategy


class BacktestResult(BaseModel):
    """Everything a report needs, in execution order."""

    model_config = ConfigDict(frozen=True)

    fills: tuple[Fill, ...]
    equity_curve: tuple[tuple[datetime, Decimal], ...]
    """Equity marked at each candle close, in candle order."""

    final_equity_quote: Decimal
    realized_pnl_quote: Decimal


class BacktestRunner:
    """Drives one strategy over one symbol's candle history."""

    def __init__(
        self,
        strategy: Strategy,
        risk_manager: RiskManager,
        portfolio: Portfolio,
        adapter: SimulatedExecutionAdapter,
    ) -> None:
        """Wire the production components around the simulated adapter."""
        self._strategy = strategy
        self._risk_manager = risk_manager
        self._portfolio = portfolio
        self._adapter = adapter
        self._fills: list[Fill] = []

    async def run(self, candles: Sequence[Candle]) -> BacktestResult:
        """Replay ``candles`` (one symbol, time-ordered) and report the outcome."""
        if not candles:
            raise ValueError("cannot backtest an empty candle series")
        symbols = {candle.symbol for candle in candles}
        if len(symbols) > 1:
            raise ValueError(f"runner replays one symbol at a time, got {sorted(symbols)}")

        self._adapter.set_fill_handler(self._on_fill)
        equity_curve: list[tuple[datetime, Decimal]] = []
        for candle in candles:
            await self._adapter.process_candle(candle)
            signal = self._strategy.on_candle(candle, self._portfolio.position(candle.symbol))
            if signal is not None:
                order = self._risk_manager.evaluate(signal, candle.close_quote)
                if order is not None:
                    await self._adapter.submit(order)
            equity_curve.append(
                (
                    candle.close_time,
                    self._portfolio.equity_quote({candle.symbol: candle.close_quote}),
                )
            )

        return BacktestResult(
            fills=tuple(self._fills),
            equity_curve=tuple(equity_curve),
            final_equity_quote=equity_curve[-1][1],
            realized_pnl_quote=self._portfolio.realized_pnl_quote(),
        )

    async def _on_fill(self, fill: Fill) -> None:
        self._portfolio.apply_fill(fill)
        self._fills.append(fill)
