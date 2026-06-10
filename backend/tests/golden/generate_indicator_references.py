"""Regenerate ``indicator_references.json`` from TA-Lib.

Run manually when adding an indicator or changing the reference dataset:

    uv run --with ta-lib python tests/golden/generate_indicator_references.py

CI never runs this — it only consumes the committed JSON — so the heavy TA-Lib
dependency stays out of the regular dev environment. The input series is a
deterministic seeded random walk, so regeneration is reproducible.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
import talib

SERIES_LENGTH = 250
PERIODS = [2, 14, 50]
OUTPUT_PATH = Path(__file__).parent / "indicator_references.json"


def make_candles() -> tuple[list[float], list[float], list[float]]:
    """Build a deterministic OHLC-ish random walk (high, low, close)."""
    rng = random.Random(42)
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    price = 100.0
    for _ in range(SERIES_LENGTH):
        price = max(1.0, price * (1.0 + rng.gauss(0.0, 0.02)))
        spread = abs(rng.gauss(0.0, 0.01)) * price
        closes.append(price)
        highs.append(price + spread)
        lows.append(max(0.5, price - spread))
    return highs, lows, closes


def nan_to_none(values: np.ndarray) -> list[float | None]:
    """Convert TA-Lib's NaN warm-up padding to JSON-friendly nulls."""
    return [None if np.isnan(v) else float(v) for v in values]


def main() -> None:
    """Compute references for every (indicator, period) pair and write JSON."""
    highs, lows, closes = make_candles()
    high_arr = np.asarray(highs)
    low_arr = np.asarray(lows)
    close_arr = np.asarray(closes)

    references: dict[str, object] = {
        "high": highs,
        "low": lows,
        "close": closes,
        "ema": {str(p): nan_to_none(talib.EMA(close_arr, timeperiod=p)) for p in PERIODS},
        "rsi": {str(p): nan_to_none(talib.RSI(close_arr, timeperiod=p)) for p in PERIODS},
        "atr": {
            str(p): nan_to_none(talib.ATR(high_arr, low_arr, close_arr, timeperiod=p))
            for p in PERIODS
        },
    }
    OUTPUT_PATH.write_text(json.dumps(references) + "\n")
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
