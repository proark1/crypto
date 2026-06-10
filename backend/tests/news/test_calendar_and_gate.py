"""Calendar windows, flag TTLs, and the news gate's blocking behavior."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Side, Signal
from tradebot.news import EventCalendar, NewsFlags, NewsGate, NewsItem, classify

NOW = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)

FOMC_JSON = '[{"name": "FOMC", "time": "2026-06-17T18:00:00Z", "window_minutes": 120}]'


def make_signal(symbol: str, at: datetime) -> Signal:
    return Signal(
        signal_id=f"test:{symbol}:{at.isoformat()}",
        strategy_name="trend_following",
        symbol=symbol,
        side=Side.BUY,
        confidence=0.8,
        stop_price_quote=Decimal("95"),
        reasons=("fast EMA crossed above slow EMA",),
        created_at=at,
    )


def flag_solana(flags: NewsFlags, at: datetime) -> None:
    item = NewsItem(
        external_id="42",
        source="test",
        title="Exchange will delist SOL pairs",
        currencies=("SOL",),
        published_at=at,
    )
    flags.flag("SOL", classify(item), at)


class TestEventCalendar:
    def test_window_covers_the_configured_span(self) -> None:
        calendar = EventCalendar.from_json(FOMC_JSON)

        assert calendar.active_event(NOW - timedelta(minutes=119)) is not None
        assert calendar.active_event(NOW + timedelta(minutes=119)) is not None
        assert calendar.active_event(NOW - timedelta(minutes=121)) is None
        assert calendar.active_event(NOW + timedelta(minutes=120)) is None  # end exclusive

    def test_empty_and_blank_config_mean_no_windows(self) -> None:
        assert EventCalendar.from_json("").events == ()
        assert EventCalendar.from_json("  ").events == ()
        assert EventCalendar.from_json("[]").events == ()

    def test_bad_config_fails_loudly_at_load(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            EventCalendar.from_json("{nope")
        with pytest.raises(ValueError, match="JSON list"):
            EventCalendar.from_json('{"name": "FOMC"}')
        with pytest.raises(ValueError, match="needs 'name' and 'time'"):
            EventCalendar.from_json('[{"name": "FOMC"}]')


class TestNewsFlags:
    def test_flags_expire_after_their_ttl(self) -> None:
        flags = NewsFlags(ttl=timedelta(hours=24))
        flag_solana(flags, NOW)

        assert flags.active_flag("SOL", NOW + timedelta(hours=23)) is not None
        assert flags.active_flag("SOL", NOW + timedelta(hours=24)) is None
        assert flags.active_flag("BTC", NOW) is None  # only the named coin

    def test_clear_is_an_explicit_operator_action(self) -> None:
        flags = NewsFlags()
        flag_solana(flags, NOW)

        assert flags.clear("SOL") is True
        assert flags.active_flag("SOL", NOW) is None
        assert flags.clear("SOL") is False  # nothing left to clear


class TestNewsGate:
    def test_flagged_coin_is_blocked_others_pass(self) -> None:
        flags = NewsFlags()
        flag_solana(flags, NOW)
        gate = NewsGate(flags, EventCalendar(()))

        blocked = gate.evaluate(make_signal("SOL/USDT", NOW + timedelta(hours=1)))
        passed = gate.evaluate(make_signal("BTC/USDT", NOW + timedelta(hours=1)))

        assert blocked.allowed is False
        assert any("delisting flag on SOL" in reason for reason in blocked.reasons)
        assert passed.allowed is True

    def test_event_window_blocks_every_coin(self) -> None:
        gate = NewsGate(NewsFlags(), EventCalendar.from_json(FOMC_JSON))

        inside = gate.evaluate(make_signal("BTC/USDT", NOW))
        outside = gate.evaluate(make_signal("BTC/USDT", NOW + timedelta(hours=3)))

        assert inside.allowed is False
        assert any("FOMC window" in reason for reason in inside.reasons)
        assert outside.allowed is True

    def test_expired_flag_no_longer_blocks(self) -> None:
        """The gate judges by the signal's own clock, so expiry needs no sweeper."""
        flags = NewsFlags(ttl=timedelta(hours=24))
        flag_solana(flags, NOW)
        gate = NewsGate(flags, EventCalendar(()))

        assert gate.evaluate(make_signal("SOL/USDT", NOW + timedelta(hours=25))).allowed is True
