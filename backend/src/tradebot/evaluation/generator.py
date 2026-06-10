"""Stratified scenario generation over a candle series (ARCHITECTURE.md §12.1).

Candidate decision points are taken at a fixed stride across the whole
series, labeled by the condition classifier, grouped into strata, and then
sampled round-robin across strata — so the report can speak about chop and
pumps even when the period was mostly a calm uptrend. Everything is seeded:
the same series, config, and seed produce the same scenarios, byte for byte.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from tradebot.core.models import Candle
from tradebot.evaluation.classifier import classify_window, window_volatility
from tradebot.evaluation.engine import ScenarioSpec
from tradebot.evaluation.models import MarketConditions

CANDIDATE_OVERSAMPLING = 3
"""Candidates per requested scenario, so strata still fill up when some
conditions are rare in the period."""


class GeneratorConfig(BaseModel):
    """Shape of one run's scenario sampling."""

    model_config = ConfigDict(frozen=True)

    scenario_count: int = Field(gt=0)
    lookback_candles: int = Field(default=200, ge=60)
    """Context the bot sees; must cover indicator warm-up (50-EMA + slack)."""

    horizon_candles: int = Field(default=60, gt=0)
    """How much future is revealed for grading after the decision."""

    seed: int = 7
    """Sampling seed; part of the run config snapshot for reproducibility."""


def generate_specs(
    candles: Sequence[Candle], config: GeneratorConfig
) -> list[tuple[ScenarioSpec, MarketConditions]]:
    """Sample stratified decision points from ``candles`` (one symbol/timeframe).

    Returns (spec, conditions) pairs in decision-time order. Raises
    ``ValueError`` when the series cannot host even one scenario.
    """
    first = config.lookback_candles
    last = len(candles) - config.horizon_candles
    if last <= first:
        raise ValueError(
            f"series of {len(candles)} candles cannot host lookback "
            f"{config.lookback_candles} + horizon {config.horizon_candles}"
        )
    wanted_candidates = max(config.scenario_count * CANDIDATE_OVERSAMPLING, 1)
    stride = max(1, (last - first) // wanted_candidates)
    candidates = list(range(first, last, stride))

    windows = {index: candles[index - config.lookback_candles : index] for index in candidates}
    # The volatility label needs a dataset-wide yardstick: the median
    # candidate-window volatility of this very run.
    reference = statistics.median(window_volatility(window) for window in windows.values())
    labeled = {index: classify_window(windows[index], reference) for index in candidates}

    strata: dict[tuple[str, str, bool], list[int]] = {}
    for index, conditions in labeled.items():
        key = (
            conditions.trend.value,
            conditions.volatility.value,
            bool(conditions.events),
        )
        strata.setdefault(key, []).append(index)

    rng = random.Random(config.seed)
    for members in strata.values():
        rng.shuffle(members)
    # Round-robin across strata (in a stable key order) so rare conditions
    # are represented before common ones are exhausted.
    chosen: list[int] = []
    ordered_strata = [strata[key] for key in sorted(strata)]
    while len(chosen) < config.scenario_count and any(ordered_strata):
        for members in ordered_strata:
            if members and len(chosen) < config.scenario_count:
                chosen.append(members.pop())

    chosen.sort()
    return [
        (
            ScenarioSpec(
                decision_index=index,
                lookback=config.lookback_candles,
                horizon=config.horizon_candles,
            ),
            labeled[index],
        )
        for index in chosen
    ]
