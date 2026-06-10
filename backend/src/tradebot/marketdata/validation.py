"""Candle sanity checks — the quarantine gate for incoming market data.

Type-level guarantees (Decimal, UTC) live on the models; *shape* problems —
impossible OHLC relationships, negative volume, wrong interval span — are an
upstream data-quality issue. The bot pauses a coin on flagged data instead of
trading on it (ARCHITECTURE.md section 11), so this function reports every
issue found rather than failing fast on the first.
"""

from __future__ import annotations

from tradebot.core.models import Candle


def validate_candle(candle: Candle) -> tuple[str, ...]:
    """Return human-readable issues with ``candle``; empty means clean."""
    issues: list[str] = []
    if candle.high_quote < candle.low_quote:
        issues.append(f"high {candle.high_quote} is below low {candle.low_quote}")
    if candle.high_quote < candle.open_quote or candle.high_quote < candle.close_quote:
        issues.append(f"high {candle.high_quote} is below open or close")
    if candle.low_quote > candle.open_quote or candle.low_quote > candle.close_quote:
        issues.append(f"low {candle.low_quote} is above open or close")
    if candle.volume_base < 0:
        issues.append(f"volume {candle.volume_base} is negative")
    expected_close_time = candle.open_time + candle.interval.duration
    if candle.close_time != expected_close_time:
        issues.append(
            f"close_time {candle.close_time.isoformat()} does not match "
            f"open_time + {candle.interval.value}"
        )
    return tuple(issues)
