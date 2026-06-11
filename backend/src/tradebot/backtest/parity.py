"""Live-vs-backtest divergence: the paper gate's first-class metric.

The paper validation gate (ARCHITECTURE.md §10, gate 2) requires that live
paper decisions match what a backtest over the same candles would have
decided. Because paper and backtest share one code path, the *strategy and
execution* portion should diverge by exactly nothing — what legitimately
diverges is everything stateful around it: the live regime gate (wall-clock
warm-up vs. self-classified history), news windows, operator pauses,
co-pilot approvals, and positions carried in from before the window. This
metric makes that divergence measurable instead of anecdotal: zero means
the one-code-path invariant is holding; a sustained non-zero number is
either expected gating (check the journal) or a parity bug worth chasing.

Comparison is at the fill level — fills are the journaled ground truth of
what actually traded, timestamped by candle, so two streams over the same
candles compare exactly.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import Fill


class DivergenceReport(BaseModel):
    """How far live paper fills strayed from a same-candle replay."""

    model_config = ConfigDict(frozen=True)

    window_start: datetime
    window_end: datetime
    live_fill_count: int
    replay_fill_count: int
    matched_count: int
    divergence_fraction: float
    """0.0 = every fill matched both ways; 1.0 = nothing matched."""

    mismatches: tuple[str, ...]
    """Human-readable description of each unmatched fill, both directions."""


def compare_fills(
    live: Sequence[Fill],
    replayed: Sequence[Fill],
    window_start: datetime,
    window_end: datetime,
) -> DivergenceReport:
    """Match two fill streams by (side, fill time); report the divergence.

    Price and quantity are deliberately not part of the match key: equity
    paths drift after the first divergent fill, so sizing differences are
    a *consequence* of divergence, not independent evidence of it. A fill
    is matched when the other stream filled the same side at the same
    candle time; the divergence fraction is the share of all fills (both
    streams) left unmatched.
    """
    live_keys = Counter((fill.side, fill.filled_at) for fill in live)
    replay_keys = Counter((fill.side, fill.filled_at) for fill in replayed)
    matched = sum((live_keys & replay_keys).values())
    total = len(live) + len(replayed)
    unmatched_live = live_keys - replay_keys
    unmatched_replay = replay_keys - live_keys
    mismatches = tuple(
        f"live only: {side} at {at.isoformat()} x{count}"
        for (side, at), count in sorted(unmatched_live.items(), key=lambda item: item[0][1])
    ) + tuple(
        f"replay only: {side} at {at.isoformat()} x{count}"
        for (side, at), count in sorted(unmatched_replay.items(), key=lambda item: item[0][1])
    )
    return DivergenceReport(
        window_start=window_start,
        window_end=window_end,
        live_fill_count=len(live),
        replay_fill_count=len(replayed),
        matched_count=matched,
        divergence_fraction=0.0 if total == 0 else 1.0 - (2 * matched) / total,
        mismatches=mismatches,
    )
