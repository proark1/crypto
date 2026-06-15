"""Campaign holdout honesty read (ARCHITECTURE.md §12.7).

A research campaign reserves the most-recent slice of history as an
untouched holdout — no round ever sweeps into it (``SweepConfig.window_end``).
This module grades the campaign's *net* move on that slice: the
configuration it started from and the one it ended on, both decided blind
through the same scenario pipeline (one code path) over **byte-identical**
holdout scenarios, so the only variable is the configuration — the
fair-comparison rule the bake-off already uses.

The read is non-gating by design (the §12.5 cost-sensitivity precedent):
every promotion was already walk-forward validated, so this never vetoes —
it reports, in plain words, whether the cumulative move still looks good on
data the search never touched, and arms the human's one-click revert when
it does not. It never raises: a span too short to host a scenario, or too
few trades to compare, resolves to a read that says so.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Protocol

from tradebot.core.models import ACCOUNTING_RESOLUTION, Candle, CandleInterval, utc_now
from tradebot.evaluation.campaign import HoldoutGrader
from tradebot.evaluation.engine import ScenarioEvaluator
from tradebot.evaluation.generator import GeneratorConfig, generate_specs
from tradebot.evaluation.sweep import DEFAULT_SCENARIO_COUNT, MIN_SWEEP_TRADES
from tradebot.execution import FillSimulatorConfig
from tradebot.marketdata import aggregate_candles
from tradebot.strategies import Strategy

logger = logging.getLogger(__name__)

StrategyForParams = Callable[[Mapping[str, Mapping[str, Any]]], Strategy]
"""Build the target's traded strategy from a per-family parameter snapshot.

Target-aware — a research family builds that family, ``production`` builds
the regime router — so the worker supplies it (it owns that wiring). It
must build a *fresh* strategy each call (the evaluator re-primes per
scenario)."""


class CandleSpanReader(Protocol):
    """The slice of ``CandleStore`` the holdout read fetches its span through."""

    async def fetch_range(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        """Return stored candles in ``[start, end)`` for one symbol/interval."""
        ...


def make_holdout_grader(
    *,
    symbol: str,
    timeframe: str,
    candles: CandleSpanReader,
    strategy_for: StrategyForParams,
    scenario_count: int = DEFAULT_SCENARIO_COUNT,
    lookback_candles: int = 200,
    horizon_candles: int = 60,
    seed: int = 7,
    fills: FillSimulatorConfig | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> HoldoutGrader:
    """Build the campaign's ``HoldoutGrader`` for one (target, symbol).

    The returned callable fetches the reserved holdout span
    ``[holdout_start, now]``, decides the start and final configurations
    blind over the **same** sampled scenarios, and returns a plain-words
    read. Money is reported as strings (Decimal-safe). It never raises.
    """
    fill_config = fills or FillSimulatorConfig()

    async def grade(
        start_params: Mapping[str, Mapping[str, Any]],
        final_params: Mapping[str, Mapping[str, Any]],
        holdout_start: datetime,
    ) -> dict[str, Any] | None:
        interval = CandleInterval(timeframe)
        end = clock()
        base = await candles.fetch_range(symbol, CandleInterval.M1, holdout_start, end)
        series = base if interval == CandleInterval.M1 else aggregate_candles(base, interval)
        generator = GeneratorConfig(
            scenario_count=scenario_count,
            lookback_candles=lookback_candles,
            horizon_candles=horizon_candles,
            seed=seed,
        )
        try:
            specs = generate_specs(series, generator)
        except ValueError:
            # The reserved slice is too short to host even one scenario; say so
            # rather than fail the campaign (the read is informative, not a gate).
            return _read(holdout_start, end, len(series), None, 0, None, 0)
        # Both configurations face the identical sampled scenarios — the only
        # variable is the configuration, so the comparison is honest.
        start_eval = ScenarioEvaluator(lambda: strategy_for(start_params), fill_config)
        final_eval = ScenarioEvaluator(lambda: strategy_for(final_params), fill_config)
        start_r: list[Decimal] = []
        final_r: list[Decimal] = []
        for spec, _ in specs:
            start_outcome = start_eval.evaluate(series, spec)
            final_outcome = final_eval.evaluate(series, spec)
            if start_outcome.r_multiple is not None:
                start_r.append(start_outcome.r_multiple)
            if final_outcome.r_multiple is not None:
                final_r.append(final_outcome.r_multiple)
            await asyncio.sleep(0)  # never starve the live candle loop
        return _read(
            holdout_start,
            end,
            len(series),
            _expectancy(start_r),
            len(start_r),
            _expectancy(final_r),
            len(final_r),
        )

    return grade


def _expectancy(r_values: Sequence[Decimal]) -> Decimal | None:
    """Mean R over the holdout trades, or ``None`` when nothing traded."""
    if not r_values:
        return None
    return (sum(r_values, Decimal(0)) / Decimal(len(r_values))).quantize(
        ACCOUNTING_RESOLUTION, rounding=ROUND_HALF_EVEN
    )


def _read(
    holdout_start: datetime,
    holdout_end: datetime,
    holdout_candles: int,
    start_expectancy: Decimal | None,
    start_trades: int,
    final_expectancy: Decimal | None,
    final_trades: int,
) -> dict[str, Any]:
    """Compose the plain-words read; ``judged`` gates the improvement claim.

    A comparison is only honest when both configurations cleared
    ``MIN_SWEEP_TRADES`` on the holdout; below that the read carries the
    numbers but withholds the verdict (``judged`` false), so a thin slice is
    never mistaken for proof either way.
    """
    judged = (
        start_expectancy is not None
        and final_expectancy is not None
        and start_trades >= MIN_SWEEP_TRADES
        and final_trades >= MIN_SWEEP_TRADES
    )
    delta = (
        final_expectancy - start_expectancy
        if start_expectancy is not None and final_expectancy is not None
        else None
    )
    improved = bool(judged and delta is not None and delta > 0)
    if judged:
        explanation = (
            f"on {holdout_candles} untouched holdout candles the campaign moved expectancy "
            f"from {start_expectancy}R to {final_expectancy}R "
            f"({'an improvement' if improved else 'no improvement'} out of sample)"
        )
    else:
        explanation = (
            f"the holdout was too thin to judge: {start_trades} vs {final_trades} graded "
            f"trades over {holdout_candles} candles (need {MIN_SWEEP_TRADES} each)"
        )
    return {
        "holdout_start": holdout_start.isoformat(),
        "holdout_end": holdout_end.isoformat(),
        "holdout_candles": holdout_candles,
        "start_expectancy_r": str(start_expectancy) if start_expectancy is not None else None,
        "final_expectancy_r": str(final_expectancy) if final_expectancy is not None else None,
        "start_trades": start_trades,
        "final_trades": final_trades,
        "delta_r": str(delta) if delta is not None else None,
        "judged": judged,
        "improved": improved,
        "explanation": explanation,
    }
