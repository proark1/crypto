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
from tradebot.engine import TradingEngine
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
        """Wire the production components around the simulated adapter.

        The per-candle loop is delegated to :class:`TradingEngine` — the
        same object paper trading subscribes to live events — so a backtest
        exercises exactly the code that trades.
        """
        self._portfolio = portfolio
        self._engine = TradingEngine(strategy, risk_manager, portfolio, adapter)
        self._consumed = False

    async def run(self, candles: Sequence[Candle]) -> BacktestResult:
        """Replay ``candles`` (one symbol, time-ordered) and report the outcome.

        A runner is single-use: strategy indicators, portfolio, and adapter
        all carry state from the run, so reuse would produce plausible-looking
        but wrong results. Build a fresh runner (with fresh components) per
        run; resetting only this object's state would hide the stale rest.
        """
        if self._consumed:
            raise RuntimeError("BacktestRunner is single-use; build a fresh one per run")
        self._consumed = True
        if not candles:
            raise ValueError("cannot backtest an empty candle series")
        symbols = {candle.symbol for candle in candles}
        if len(symbols) > 1:
            raise ValueError(f"runner replays one symbol at a time, got {sorted(symbols)}")

        equity_curve: list[tuple[datetime, Decimal]] = []
        for candle in candles:
            await self._engine.process_candle(candle)
            equity_curve.append(
                (
                    candle.close_time,
                    self._portfolio.equity_quote({candle.symbol: candle.close_quote}),
                )
            )

        return BacktestResult(
            fills=self._engine.fills,
            equity_curve=tuple(equity_curve),
            final_equity_quote=equity_curve[-1][1],
            realized_pnl_quote=self._portfolio.realized_pnl_quote(),
        )
