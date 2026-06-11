"""The suggestion ladder must always be runnable as-is, whatever is stored."""

from datetime import UTC, datetime, timedelta

from tradebot.core.models import CandleInterval
from tradebot.evaluation.suggestions import build_suggestions
from tradebot.evaluation.sweep import DEFAULT_SCENARIO_COUNT

NOW = datetime(2026, 6, 11, tzinfo=UTC)


class DepthStub:
    """A store that only knows how deep each coin's history reaches."""

    def __init__(self, earliest_by_symbol: dict[str, datetime | None]) -> None:
        self._earliest_by_symbol = earliest_by_symbol

    async def earliest_open_time(self, symbol: str, interval: CandleInterval) -> datetime | None:
        # Depth must be measured on the base series every run aggregates from.
        assert interval is CandleInterval.M1
        return self._earliest_by_symbol[symbol]


async def test_deep_history_yields_three_full_rungs() -> None:
    store = DepthStub({"BTC/USDT": NOW - timedelta(days=5 * 365)})

    suggestions = await build_suggestions(store, ["BTC/USDT"], now=NOW)

    assert [(s.timeframe, s.history_days) for s in suggestions] == [
        ("4h", 1460),
        ("1h", 365),
        ("15m", 91),
    ]
    # The rungs are tuned to comparable sample sizes — none dwarfs another.
    assert [s.expected_candles for s in suggestions] == [8760, 8760, 8736]
    assert all(s.scenario_count == DEFAULT_SCENARIO_COUNT for s in suggestions)
    assert all(s.symbol == "BTC/USDT" for s in suggestions)


async def test_shallow_history_clamps_every_rung_to_what_exists() -> None:
    store = DepthStub({"BTC/USDT": NOW - timedelta(days=30)})

    suggestions = await build_suggestions(store, ["BTC/USDT"], now=NOW)

    assert [s.history_days for s in suggestions] == [30, 30, 30]
    assert all("clamped" in s.rationale for s in suggestions)


async def test_sub_day_history_is_still_runnable() -> None:
    """history_days must satisfy the run config's gt=0 even on a fresh coin."""
    store = DepthStub({"BTC/USDT": NOW - timedelta(hours=6)})

    suggestions = await build_suggestions(store, ["BTC/USDT"], now=NOW)

    assert [s.history_days for s in suggestions] == [1, 1, 1]


async def test_coin_with_nothing_stored_gets_no_suggestions() -> None:
    store = DepthStub({"NEW/USDT": None, "BTC/USDT": NOW - timedelta(days=400)})

    suggestions = await build_suggestions(store, ["NEW/USDT", "BTC/USDT"], now=NOW)

    assert all(s.symbol == "BTC/USDT" for s in suggestions)
    assert len(suggestions) == 3


async def test_three_suggestions_per_coin_in_symbol_order() -> None:
    earliest = NOW - timedelta(days=2000)
    store = DepthStub({"BTC/USDT": earliest, "ETH/USDT": earliest})

    suggestions = await build_suggestions(store, ["BTC/USDT", "ETH/USDT"], now=NOW)

    assert [s.symbol for s in suggestions] == ["BTC/USDT"] * 3 + ["ETH/USDT"] * 3
