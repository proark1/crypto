"""Walk-forward window splitting.

Walk-forward validation optimizes parameters on a training window and judges
them on the unseen window that follows, rolling forward through history
(ARCHITECTURE.md 4.6). This module owns only the splitting; the parameter
sweep (``evaluation/sweep.py``) is the optimizer that consumes the windows.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from tradebot.core.models import Candle


class WalkForwardWindow(BaseModel):
    """One (train, validation) pair; validation always follows training in time."""

    model_config = ConfigDict(frozen=True)

    train: tuple[Candle, ...]
    validation: tuple[Candle, ...]


def split_walk_forward(
    candles: Sequence[Candle], train_size: int, validate_size: int
) -> list[WalkForwardWindow]:
    """Split ``candles`` into rolling windows stepping one validation at a time.

    Windows are anchored so every candle after the first training window is
    validated exactly once; the tail short of a full validation window is
    dropped rather than judged on a fragment. Raises if even one full window
    does not fit — silently returning nothing would look like a clean pass.
    """
    if train_size < 1 or validate_size < 1:
        raise ValueError("train_size and validate_size must both be >= 1")
    if len(candles) < train_size + validate_size:
        raise ValueError(
            f"need at least {train_size + validate_size} candles for one window, got {len(candles)}"
        )
    windows: list[WalkForwardWindow] = []
    start = 0
    while start + train_size + validate_size <= len(candles):
        train_end = start + train_size
        windows.append(
            WalkForwardWindow(
                train=tuple(candles[start:train_end]),
                validation=tuple(candles[train_end : train_end + validate_size]),
            )
        )
        start += validate_size
    return windows


def split_rolling_by_fraction(
    candles: Sequence[Candle], training_fraction: float, window_count: int
) -> list[WalkForwardWindow]:
    """Split into exactly ``window_count`` rolling windows by fraction.

    The training size is ``training_fraction`` of the series; the remainder
    is divided into ``window_count`` chronological validation slices (sizes
    differ by at most one candle), and each window trains on the candles
    immediately preceding its validation slice. Every candle after the
    first training window is validated exactly once and nothing is dropped
    — the most recent data is always judged, never discarded to rounding.

    Raises when the series cannot host even one candle per validation
    slice; silently returning fewer windows than asked would make a sweep
    look more validated than it is.
    """
    if window_count < 1:
        raise ValueError(f"window_count must be >= 1, got {window_count}")
    if not 0.0 < training_fraction < 1.0:
        raise ValueError(f"training_fraction must be in (0, 1), got {training_fraction}")
    train_size = int(len(candles) * training_fraction)
    remaining = len(candles) - train_size
    if train_size < 1 or remaining < window_count:
        raise ValueError(
            f"{len(candles)} candles cannot host a {training_fraction:g} training split "
            f"with {window_count} validation windows"
        )
    boundaries = [train_size + (remaining * k) // window_count for k in range(window_count + 1)]
    return [
        WalkForwardWindow(
            train=tuple(candles[start - train_size : start]),
            validation=tuple(candles[start:end]),
        )
        for start, end in itertools.pairwise(boundaries)
    ]
