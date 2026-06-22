"""Shared promotion gates for automated research loops.

The evaluation stack may still grade short timeframes for diagnosis, but
automated settings changes must come from bars whose live evidence has held
up better. Keeping the rule here prevents the single-sweep improver and the
iterated campaign loop from drifting apart.
"""

PROMOTION_ELIGIBLE_TIMEFRAMES = frozenset({"4h", "1d"})
"""Research timeframes allowed to auto-promote a validated challenger."""


def promotion_timeframe_allowed(timeframe: str) -> bool:
    """Return whether an automated research verdict may change settings."""
    return timeframe in PROMOTION_ELIGIBLE_TIMEFRAMES
