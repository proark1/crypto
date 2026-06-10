"""Incremental technical indicators.

Every indicator here updates in O(1) per candle and never recomputes history
(CLAUDE.md efficiency rules). Values are plain floats: indicator math is the
one place floats are allowed, because outputs feed signal logic — never order
sizes — and the boundary back to ``Decimal`` happens in the risk manager.

All implementations follow TA-Lib's seeding conventions exactly and are tested
against TA-Lib reference outputs (``tests/golden/indicator_references.json``).
``update`` returns ``None`` during the warm-up window, matching TA-Lib's
lookback, so callers cannot act on a half-formed value by accident.
"""

from tradebot.indicators.adx import Adx
from tradebot.indicators.atr import Atr
from tradebot.indicators.ema import Ema
from tradebot.indicators.rsi import Rsi

__all__ = ["Adx", "Atr", "Ema", "Rsi"]
