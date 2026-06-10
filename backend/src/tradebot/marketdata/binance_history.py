"""Historical 1m klines from Binance's public data dumps.

``data.binance.vision`` serves monthly zip archives of kline CSVs for free,
no API key — the P0 backtesting dataset from ARCHITECTURE.md 5.1. Parsing is
separated from downloading so the parser is fully testable offline and the
downloader is a thin, mockable HTTP shell around it.

Binance quirks handled here, because they will bite anyone who ignores them:
- dumps switched ``open_time`` from milliseconds to microseconds starting
  with the 2025-01 files; the unit is detected per row by magnitude;
- some archives carry a CSV header row, some do not;
- the dump's ``close_time`` field is ``open_time + 59_999ms``; we discard it
  and derive our half-open convention (``open_time + 1m``) instead.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from tradebot.core.models import Candle, CandleInterval
from tradebot.marketdata.gaps import find_gaps

_DUMP_URL_TEMPLATE = (
    "https://data.binance.vision/data/spot/monthly/klines/"
    "{pair}/1m/{pair}-1m-{year:04d}-{month:02d}.zip"
)
_MICROSECOND_THRESHOLD = 10**14
"""Timestamps >= this are microseconds (~year 5138 in ms, ~1973 in us)."""


def _pair_for_dump(symbol: str) -> str:
    """Map our ``BASE/QUOTE`` symbol to Binance's concatenated pair name."""
    if "/" not in symbol:
        raise ValueError(f"symbol must look like 'BTC/USDT', got {symbol!r}")
    return symbol.replace("/", "")


def _parse_open_time(raw: str) -> datetime:
    value = int(raw)
    if value >= _MICROSECOND_THRESHOLD:
        return datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    return datetime.fromtimestamp(value / 1_000, tz=UTC)


def parse_kline_csv(csv_text: str, symbol: str) -> list[Candle]:
    """Parse one dump CSV into 1m candles, in file order.

    Prices and volumes go straight from string to ``Decimal`` — floats never
    touch the data path.
    """
    candles: list[Candle] = []
    for line in csv_text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split(",")
        if len(fields) < 6:
            raise ValueError(f"malformed kline row (need >= 6 fields): {line!r}")
        if not fields[0].isdigit():
            continue  # header row
        open_time = _parse_open_time(fields[0])
        candles.append(
            Candle(
                symbol=symbol,
                interval=CandleInterval.M1,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open_quote=Decimal(fields[1]),
                high_quote=Decimal(fields[2]),
                low_quote=Decimal(fields[3]),
                close_quote=Decimal(fields[4]),
                volume_base=Decimal(fields[5]),
            )
        )
    return candles


def month_range(
    start_year: int, start_month: int, end_year: int, end_month: int
) -> Iterator[tuple[int, int]]:
    """Yield (year, month) pairs from start to end inclusive; raises if reversed."""
    if not (1 <= start_month <= 12 and 1 <= end_month <= 12):
        raise ValueError("months must be in 1..12")
    if (start_year, start_month) > (end_year, end_month):
        raise ValueError(
            f"start {start_year}-{start_month:02d} is after end {end_year}-{end_month:02d}"
        )
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        yield year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1


async def download_month(
    client: httpx.AsyncClient, symbol: str, year: int, month: int
) -> list[Candle]:
    """Download and parse one monthly dump for ``symbol``."""
    url = _DUMP_URL_TEMPLATE.format(pair=_pair_for_dump(symbol), year=year, month=month)
    response = await client.get(url)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise ValueError(f"expected exactly one CSV in {url}, found {names}")
        csv_text = archive.read(names[0]).decode("utf-8")
    return parse_kline_csv(csv_text, symbol)


async def download_history(
    client: httpx.AsyncClient,
    symbol: str,
    start_year: int,
    start_month: int,
    end_year: int,
    end_month: int,
) -> list[Candle]:
    """Download a contiguous span of months and report data-quality findings.

    Months are fetched sequentially (Binance rate-limits aggressive pulls and
    backfills are not latency-sensitive). Raises if the stitched series is
    out of order; intra-series gaps are possible in real exchange data
    (outages) and are *allowed*, but the boundaries between months must line
    up — a gap exactly at a month boundary means a download problem, not an
    exchange outage, so it raises.
    """
    all_candles: list[Candle] = []
    for year, month in month_range(start_year, start_month, end_year, end_month):
        month_candles = await download_month(client, symbol, year, month)
        if not month_candles:
            raise ValueError(f"empty dump for {symbol} {year}-{month:02d}")
        if all_candles:
            boundary_gap = month_candles[0].open_time - all_candles[-1].open_time
            if boundary_gap != CandleInterval.M1.duration:
                raise ValueError(
                    f"month boundary mismatch entering {year}-{month:02d}: "
                    f"{all_candles[-1].open_time.isoformat()} -> "
                    f"{month_candles[0].open_time.isoformat()}"
                )
        all_candles.extend(month_candles)
    # Validates ordering as a side effect; gaps inside months are tolerated.
    find_gaps(all_candles, CandleInterval.M1)
    return all_candles
