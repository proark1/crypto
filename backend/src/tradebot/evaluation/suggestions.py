"""Suggested evaluations: fitted history windows the operator can just run.

Choosing an evaluation shape by hand means knowing how deep each coin's
stored history reaches and how many candles a timeframe yields per day —
get it wrong and the run silently grades on a sliver. This module does
that fitting server-side: for every active coin it proposes exactly three
ready-to-run shapes, one per granularity rung, each reaching as far back
as the stored data allows so every sample is as large as it can be.

The three rungs probe different granularities over windows fitted to the
data: a whole ~4-year halving cycle on 4h candles, the same full cycle on
the 1h trading timeframe (the ladder's biggest sample), and the most
recent quarter on 15m. A coin whose stored history is shallower than
a rung's target simply gets the window clamped to what exists — the
suggestion is always runnable as-is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from tradebot.core.models import CandleInterval
from tradebot.evaluation.sweep import DEFAULT_SCENARIO_COUNT


class HistoryDepthStore(Protocol):
    """The single store capability suggestion-building needs."""

    async def earliest_open_time(self, symbol: str, interval: CandleInterval) -> datetime | None:
        """Return the oldest stored open time, or ``None`` when nothing is stored."""
        ...


class SuggestedEvaluation(BaseModel):
    """One ready-to-run evaluation shape, fitted to a coin's stored history."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: str
    history_days: int = Field(gt=0)
    expected_candles: int
    """Sample size the window yields at this timeframe — what the rung maximizes."""

    scenario_count: int
    title: str
    rationale: str


class _Rung(BaseModel):
    """One granularity rung of the suggestion ladder."""

    model_config = ConfigDict(frozen=True)

    interval: CandleInterval
    target_days: int
    title: str
    rationale: str


_LADDER: tuple[_Rung, ...] = (
    _Rung(
        interval=CandleInterval.H4,
        target_days=1460,
        title="full cycle",
        rationale=(
            "4h candles across a whole ~4-year halving cycle — bull, bear, "
            "and chop in one sample, so no regime is left untested"
        ),
    ),
    _Rung(
        interval=CandleInterval.H1,
        target_days=1460,
        title="deep cycle",
        rationale=(
            "1h candles across the same ~4-year cycle — the timeframe the "
            "bot trades, at four times the resolution, making it the "
            "ladder's biggest sample"
        ),
    ),
    _Rung(
        interval=CandleInterval.M15,
        target_days=91,
        title="recent quarter",
        rationale=(
            "15m candles across the latest quarter — fine-grained entries "
            "and exits under current market conditions"
        ),
    ),
)
"""Both cycle rungs reach the full backfill horizon — every regime the
database holds, with the 1h rung the heavyweight (~35k candles at full
depth) because it is the timeframe the bot trades; the 15m rung stays a
quarter so its fine-grained read reflects current conditions, not a
years-old microstructure. Always exactly three per coin (CLAUDE.md:
research needs sample sizes as large as the data allows)."""


def _fit(rung: _Rung, symbol: str, available_days: int) -> SuggestedEvaluation:
    """Clamp one rung to the coin's stored depth; the result is always runnable."""
    history_days = max(1, min(rung.target_days, available_days))
    expected_candles = int(timedelta(days=history_days) / rung.interval.duration)
    rationale = rung.rationale
    if history_days < rung.target_days:
        rationale += f" (clamped to the {history_days} days stored so far)"
    return SuggestedEvaluation(
        symbol=symbol,
        timeframe=rung.interval.value,
        history_days=history_days,
        expected_candles=expected_candles,
        scenario_count=DEFAULT_SCENARIO_COUNT,
        title=rung.title,
        rationale=rationale,
    )


async def build_suggestions(
    store: HistoryDepthStore,
    symbols: list[str],
    now: datetime | None = None,
) -> list[SuggestedEvaluation]:
    """Propose three fitted evaluation shapes per coin with stored history.

    Depth is measured on the 1m base series — the resolution every
    evaluation fetches and aggregates from — so the fit reflects what a
    run would actually find. Coins with nothing stored yet get no
    suggestions: there is no history to evaluate, and a backfill is
    already on its way.
    """
    reference = now if now is not None else datetime.now(UTC)
    suggestions: list[SuggestedEvaluation] = []
    for symbol in symbols:
        earliest = await store.earliest_open_time(symbol, CandleInterval.M1)
        if earliest is None:
            continue
        available_days = int((reference - earliest) / timedelta(days=1))
        suggestions.extend(_fit(rung, symbol, available_days) for rung in _LADDER)
    return suggestions
