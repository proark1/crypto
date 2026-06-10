from datetime import UTC, datetime, timedelta, timezone

import pytest

from tradebot.core.clock import SimulatedClock, WallClock

START = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def test_wall_clock_returns_utc_aware_time() -> None:
    now = WallClock().now()
    assert now.tzinfo == UTC


def test_simulated_clock_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SimulatedClock(datetime(2026, 1, 2, 3, 4))


def test_simulated_clock_normalizes_start_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    clock = SimulatedClock(datetime(2026, 1, 2, 5, 4, tzinfo=plus_two))
    assert clock.now() == START
    assert clock.now().tzinfo == UTC


def test_simulated_clock_advances_forward() -> None:
    clock = SimulatedClock(START)
    later = START + timedelta(minutes=5)
    clock.advance_to(later)
    assert clock.now() == later


def test_simulated_clock_allows_advancing_to_same_moment() -> None:
    clock = SimulatedClock(START)
    clock.advance_to(START)
    assert clock.now() == START


def test_simulated_clock_refuses_to_go_backwards() -> None:
    clock = SimulatedClock(START)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(START - timedelta(seconds=1))


def test_simulated_clock_rejects_naive_advance() -> None:
    clock = SimulatedClock(START)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.advance_to(datetime(2026, 1, 2, 4, 0))
