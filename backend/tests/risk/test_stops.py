"""Managed stop math: breakeven locks, trails follow, levels never fall."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradebot.core.models import Candle, CandleInterval
from tradebot.risk import ManagedStop

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def make_candle(index: int, low: str, high: str) -> Candle:
    open_time = BASE_TIME + timedelta(minutes=index)
    return Candle(
        symbol="BTC/USDT",
        interval=CandleInterval.M1,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open_quote=Decimal(low),
        high_quote=Decimal(high),
        low_quote=Decimal(low),
        close_quote=Decimal(high),
        volume_base=Decimal("1"),
    )


class TestManagedStop:
    def test_disabled_policies_leave_the_signal_stop_alone(self) -> None:
        stop = ManagedStop(Decimal("100"), Decimal("95"))
        stop.ratchet(make_candle(0, "99", "130"))
        assert stop.stop_price_quote == Decimal("95")

    def test_breakeven_locks_the_entry_after_one_r(self) -> None:
        stop = ManagedStop(Decimal("100"), Decimal("95"), breakeven_at_r=1.0)
        stop.ratchet(make_candle(0, "99", "104"))  # +0.8R: not yet
        assert stop.stop_price_quote == Decimal("95")
        stop.ratchet(make_candle(1, "100", "105"))  # +1R reached
        assert stop.stop_price_quote == Decimal("100")

    def test_trail_follows_the_high_and_never_falls(self) -> None:
        stop = ManagedStop(Decimal("100"), Decimal("95"), trail_distance_quote=Decimal("3"))
        stop.ratchet(make_candle(0, "100", "110"))
        assert stop.stop_price_quote == Decimal("107")
        stop.ratchet(make_candle(1, "101", "104"))  # pullback: stop holds
        assert stop.stop_price_quote == Decimal("107")

    def test_breach_is_judged_before_the_candle_ratchets(self) -> None:
        """A candle must not raise the stop above its own low and then
        claim to have stopped out at the raised level."""
        stop = ManagedStop(Decimal("100"), Decimal("95"), trail_distance_quote=Decimal("1"))
        spike = make_candle(0, "94", "120")  # breaches 95 and rallies
        assert stop.is_breached_by(spike) is True

    def test_from_signal_carries_the_policy(self) -> None:
        from tradebot.core.models import Side, Signal

        signal = Signal(
            strategy_name="trend_following",
            symbol="BTC/USDT",
            side=Side.BUY,
            confidence=1.0,
            stop_price_quote=Decimal("95"),
            breakeven_at_r=1.0,
            trail_distance_quote=Decimal("4"),
            created_at=BASE_TIME,
        )
        stop = ManagedStop.from_signal(signal, Decimal("100"))
        stop.ratchet(make_candle(0, "100", "106"))
        # Both policies armed: breakeven (>=+1R) and trail (106-4=102) —
        # the higher one wins.
        assert stop.stop_price_quote == Decimal("102")
