"""Funding-history ingestion: symbol mapping, row parsing, paged backfill.

The backfiller's persistence is covered against real Postgres in the
``FundingStore`` tests; here a fast in-memory store stands in so the paging,
resume, and fail-safe parsing logic is exercised without a database.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tradebot.core.models import FundingRate
from tradebot.marketdata.funding import (
    FundingBackfiller,
    FundingRow,
    _row_to_funding_rate,
    perp_symbol_for,
)
from tradebot.persistence import FundingStore

# Anchored a few hours back so a cold-start fetch (now - history_days) always
# reaches them, whatever the wall clock; funding prints every 8h on Binance.
_BASE = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)


def _ms(hours: int) -> int:
    return int((_BASE + timedelta(hours=hours)).timestamp() * 1000)


def _row(hours: int, rate: float = 0.0001) -> dict[str, Any]:
    """One CCXT funding-history entry, perp-keyed as the venue returns it."""
    return {"symbol": "BTC/USDT:USDT", "fundingRate": rate, "timestamp": _ms(hours)}


class _FakeFundingStore(FundingStore):
    """In-memory stand-in: only the two methods the backfiller calls, no DB."""

    def __init__(self) -> None:
        self.rows: list[FundingRate] = []

    async def insert_batch(self, rates: Sequence[FundingRate]) -> None:
        seen = {(r.symbol, r.funding_time) for r in self.rows}
        self.rows.extend(r for r in rates if (r.symbol, r.funding_time) not in seen)

    async def latest_funding_time(self, symbol: str) -> datetime | None:
        times = [r.funding_time for r in self.rows if r.symbol == symbol]
        return max(times) if times else None


class _ScriptedFundingExchange:
    """Serves CCXT-shaped funding rows from ``since`` forward, capped at ``limit``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = sorted(rows, key=lambda r: int(r["timestamp"]))
        self.calls: list[tuple[str, int | None, int | None]] = []

    async def fetch_funding_rate_history(
        self, symbol: str, since: int | None = None, limit: int | None = None
    ) -> list[FundingRow]:
        self.calls.append((symbol, since, limit))
        rows = [r for r in self._rows if since is None or int(r["timestamp"]) >= since]
        return list(rows if limit is None else rows[:limit])


class TestPerpSymbolFor:
    def test_usdt_spot_maps_to_linear_perp(self) -> None:
        assert perp_symbol_for("BTC/USDT") == "BTC/USDT:USDT"

    def test_already_a_contract_is_unchanged(self) -> None:
        assert perp_symbol_for("BTC/USDT:USDT") == "BTC/USDT:USDT"

    def test_non_usdt_is_left_alone(self) -> None:
        # No clean perp mapping — returned as-is so the fetch simply finds nothing.
        assert perp_symbol_for("BTC/USDC") == "BTC/USDC"


class TestRowToFundingRate:
    def test_parses_a_well_formed_row(self) -> None:
        rate = _row_to_funding_rate(_row(0, rate=-0.0002), "BTC/USDT")
        assert rate is not None
        assert rate.symbol == "BTC/USDT"  # keyed by spot, not the perp fetched
        assert rate.rate == Decimal("-0.0002")  # signed and exact

    @pytest.mark.parametrize(
        "row",
        [
            {"symbol": "BTC/USDT:USDT", "timestamp": _ms(0)},  # no rate
            {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001},  # no timestamp
            {"symbol": "BTC/USDT:USDT", "fundingRate": "n/a", "timestamp": _ms(0)},  # unparseable
        ],
    )
    def test_malformed_row_is_dropped(self, row: dict[str, object]) -> None:
        assert _row_to_funding_rate(row, "BTC/USDT") is None


class TestFundingBackfiller:
    async def test_persists_every_print_keyed_by_spot(self) -> None:
        store = _FakeFundingStore()
        exchange = _ScriptedFundingExchange([_row(0), _row(8), _row(16)])
        backfiller = FundingBackfiller(exchange, store, "BTC/USDT", history_days=3650)

        inserted = await backfiller.backfill()

        assert inserted == 3
        assert [r.symbol for r in store.rows] == ["BTC/USDT"] * 3  # spot key
        assert exchange.calls[0][0] == "BTC/USDT:USDT"  # perp fetched

    async def test_resumes_from_the_latest_stored_print(self) -> None:
        store = _FakeFundingStore()
        store.rows.append(
            FundingRate(symbol="BTC/USDT", funding_time=_BASE, rate=Decimal("0.0001"))
        )
        exchange = _ScriptedFundingExchange([_row(0), _row(8), _row(16)])
        backfiller = FundingBackfiller(exchange, store, "BTC/USDT", history_days=3650)

        inserted = await backfiller.backfill()

        assert inserted == 2  # only the two newer than the seeded print
        # The first fetch resumes just past the latest stored time, not the cold start.
        assert exchange.calls[0][1] is not None and exchange.calls[0][1] > _ms(0)

    async def test_pages_through_the_limit(self) -> None:
        store = _FakeFundingStore()
        exchange = _ScriptedFundingExchange([_row(h) for h in (0, 8, 16, 24, 32)])
        backfiller = FundingBackfiller(exchange, store, "BTC/USDT", history_days=3650, page_limit=2)

        inserted = await backfiller.backfill()

        assert inserted == 5
        assert len(exchange.calls) >= 3  # 2 + 2 + 1, each a separate page

    async def test_drops_malformed_rows(self) -> None:
        store = _FakeFundingStore()
        exchange = _ScriptedFundingExchange(
            [_row(0), {"symbol": "BTC/USDT:USDT", "timestamp": _ms(8)}, _row(16)]
        )
        backfiller = FundingBackfiller(exchange, store, "BTC/USDT", history_days=3650)

        inserted = await backfiller.backfill()

        assert inserted == 2  # the rate-less middle row is skipped, not fatal

    async def test_empty_history_is_a_noop(self) -> None:
        store = _FakeFundingStore()
        backfiller = FundingBackfiller(
            _ScriptedFundingExchange([]), store, "BTC/USDT", history_days=3650
        )

        assert await backfiller.backfill() == 0
        assert store.rows == []
