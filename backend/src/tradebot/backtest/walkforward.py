"""Walk-forward window splitting.

Walk-forward validation optimizes parameters on a training window and judges
them on the unseen window that follows, rolling forward through history
(ARCHITECTURE.md 4.6). This module owns only the splitting; the optimizer
that consumes the windows arrives with the research loop.
"""

from __future__ import annotations

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
