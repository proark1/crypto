from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.risk import BreakerConfig, CircuitBreakers

BASE_TIME = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def make_breakers(
    daily_loss: str = "0.03",
    drawdown: str = "0.20",
    streak: int = 3,
    cooldown_hours: int = 4,
    entries_per_day: int = 10,
) -> CircuitBreakers:
    return CircuitBreakers(
        BreakerConfig(
            max_daily_loss_fraction=Decimal(daily_loss),
            max_drawdown_fraction=Decimal(drawdown),
            loss_streak_threshold=streak,
            loss_streak_cooldown=timedelta(hours=cooldown_hours),
            max_entries_per_day=entries_per_day,
        )
    )


class TestDailyLossTrip:
    def test_trips_when_equity_falls_past_day_start_fraction(self) -> None:
        breakers = make_breakers(daily_loss="0.03")
        breakers.observe(BASE_TIME, Decimal("10000"))
        breakers.observe(BASE_TIME + timedelta(minutes=1), Decimal("9700"))  # exactly -3%

        assert breakers.tripped_reason is not None
        assert "daily loss" in breakers.tripped_reason
        assert breakers.entry_block_reason(BASE_TIME + timedelta(minutes=2)) is not None

    def test_does_not_trip_above_the_limit(self) -> None:
        breakers = make_breakers(daily_loss="0.03")
        breakers.observe(BASE_TIME, Decimal("10000"))
        breakers.observe(BASE_TIME + timedelta(minutes=1), Decimal("9701"))

        assert breakers.tripped_reason is None

    def test_day_rollover_rebases_the_loss_limit(self) -> None:
        breakers = make_breakers(daily_loss="0.03")
        breakers.observe(BASE_TIME, Decimal("10000"))
        next_day = BASE_TIME + timedelta(days=1)
        # -2.5% from the original day start, but the new day rebased at 9750.
        breakers.observe(next_day, Decimal("9750"))

        assert breakers.tripped_reason is None
        # From the new base, -3% now trips.
        breakers.observe(next_day + timedelta(minutes=1), Decimal("9457.5"))
        assert breakers.tripped_reason is not None

    def test_trip_latches_until_reset(self) -> None:
        breakers = make_breakers(daily_loss="0.03")
        breakers.observe(BASE_TIME, Decimal("10000"))
        breakers.observe(BASE_TIME + timedelta(minutes=1), Decimal("9000"))
        # Recovery does not clear it: a human must look first.
        breakers.observe(BASE_TIME + timedelta(minutes=2), Decimal("10500"))

        assert breakers.tripped_reason is not None
        breakers.reset()
        assert breakers.tripped_reason is None
        assert breakers.entry_block_reason(BASE_TIME + timedelta(minutes=3)) is None


class TestDrawdownTrip:
    def test_trips_when_equity_falls_past_fraction_of_peak(self) -> None:
        breakers = make_breakers(daily_loss="0.99", drawdown="0.20")
        breakers.observe(BASE_TIME, Decimal("10000"))
        # New peak on a later day, then a fall past 20% of it.
        breakers.observe(BASE_TIME + timedelta(days=1), Decimal("12000"))
        breakers.observe(BASE_TIME + timedelta(days=2), Decimal("9600"))  # exactly -20%

        assert breakers.tripped_reason is not None
        assert "drawdown" in breakers.tripped_reason

    def test_peak_only_rises(self) -> None:
        breakers = make_breakers(daily_loss="0.99", drawdown="0.20")
        breakers.observe(BASE_TIME, Decimal("12000"))
        breakers.observe(BASE_TIME + timedelta(days=1), Decimal("10000"))  # -16.7%: ok
        breakers.observe(BASE_TIME + timedelta(days=2), Decimal("9601"))  # -19.99%: ok

        assert breakers.tripped_reason is None


class TestLossStreakCooldown:
    def test_streak_starts_cooldown_then_expires(self) -> None:
        breakers = make_breakers(streak=3, cooldown_hours=4)
        for minute in range(3):
            breakers.record_closed_trade(Decimal("-10"), BASE_TIME + timedelta(minutes=minute))

        during = BASE_TIME + timedelta(hours=1)
        after = BASE_TIME + timedelta(hours=5)
        assert breakers.entry_block_reason(during) is not None
        assert "cooldown" in str(breakers.entry_block_reason(during))
        assert breakers.entry_block_reason(after) is None  # auto-clears by time

    def test_winning_trade_clears_the_streak(self) -> None:
        breakers = make_breakers(streak=3)
        breakers.record_closed_trade(Decimal("-10"), BASE_TIME)
        breakers.record_closed_trade(Decimal("-10"), BASE_TIME)
        breakers.record_closed_trade(Decimal("5"), BASE_TIME)  # streak broken
        breakers.record_closed_trade(Decimal("-10"), BASE_TIME)
        breakers.record_closed_trade(Decimal("-10"), BASE_TIME)

        assert breakers.entry_block_reason(BASE_TIME) is None

    def test_reset_clears_an_active_cooldown(self) -> None:
        breakers = make_breakers(streak=1, cooldown_hours=4)
        breakers.record_closed_trade(Decimal("-10"), BASE_TIME)
        assert breakers.entry_block_reason(BASE_TIME) is not None

        breakers.reset()
        assert breakers.entry_block_reason(BASE_TIME) is None


class TestDailyEntryCap:
    def test_cap_blocks_further_entries_same_day(self) -> None:
        breakers = make_breakers(entries_per_day=2)
        breakers.observe(BASE_TIME, Decimal("10000"))
        breakers.record_entry(BASE_TIME)
        breakers.record_entry(BASE_TIME)

        reason = breakers.entry_block_reason(BASE_TIME + timedelta(minutes=1))
        assert reason is not None
        assert "entry cap" in reason

    def test_cap_resets_on_utc_day_rollover(self) -> None:
        breakers = make_breakers(entries_per_day=1)
        breakers.observe(BASE_TIME, Decimal("10000"))
        breakers.record_entry(BASE_TIME)
        assert breakers.entry_block_reason(BASE_TIME) is not None

        next_day = BASE_TIME + timedelta(days=1)
        breakers.observe(next_day, Decimal("10000"))
        assert breakers.entry_block_reason(next_day) is None
        assert breakers.entries_today == 0


class TestTimeHandling:
    def test_naive_datetimes_are_rejected_everywhere(self) -> None:
        breakers = make_breakers()
        naive = datetime(2026, 1, 2, 12, 0)
        with pytest.raises(ValueError, match="naive datetime"):
            breakers.observe(naive, Decimal("10000"))
        with pytest.raises(ValueError, match="naive datetime"):
            breakers.record_closed_trade(Decimal("-1"), naive)
        with pytest.raises(ValueError, match="naive datetime"):
            breakers.record_entry(naive)
        with pytest.raises(ValueError, match="naive datetime"):
            breakers.entry_block_reason(naive)

    def test_non_utc_zones_are_normalized_to_utc_days(self) -> None:
        from datetime import timezone

        breakers = make_breakers(entries_per_day=1)
        # 23:00 UTC expressed as 01:00 +02:00 the next calendar day locally.
        plus_two = timezone(timedelta(hours=2))
        local = datetime(2026, 1, 3, 1, 0, tzinfo=plus_two)  # == 2026-01-02 23:00 UTC
        breakers.observe(local, Decimal("10000"))
        breakers.record_entry(local)

        # One UTC hour later it is still the same UTC day: cap holds.
        assert breakers.entry_block_reason(datetime(2026, 1, 2, 23, 30, tzinfo=UTC)) is not None
