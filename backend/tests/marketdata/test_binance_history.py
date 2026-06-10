import io
import zipfile
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from tradebot.marketdata import download_history, download_month, month_range, parse_kline_csv

# 2024-06-01 00:00 and 00:01 UTC in milliseconds.
MS_ROWS = (
    "1717200000000,67000.1,67100.5,66950.0,67050.2,12.5,1717200059999,837000,150,6.1,409000,0\n"
    "1717200060000,67050.2,67200.0,67000.0,67150.9,8.25,1717200119999,553000,90,4.0,268000,0\n"
)
# Same instants expressed in microseconds (2025+ dump format).
US_ROWS = (
    "1717200000000000,67000.1,67100.5,66950.0,67050.2,12.5,x,x,x,x,x,0\n"
    "1717200060000000,67050.2,67200.0,67000.0,67150.9,8.25,x,x,x,x,x,0\n"
)


class TestParsing:
    def test_parses_millisecond_rows_to_candles(self) -> None:
        candles = parse_kline_csv(MS_ROWS, "BTC/USDT")

        assert len(candles) == 2
        first = candles[0]
        assert first.open_time == datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
        assert first.close_time == datetime(2024, 6, 1, 0, 1, tzinfo=UTC)
        assert first.open_quote == Decimal("67000.1")
        assert first.high_quote == Decimal("67100.5")
        assert first.low_quote == Decimal("66950.0")
        assert first.close_quote == Decimal("67050.2")
        assert first.volume_base == Decimal("12.5")

    def test_microsecond_timestamps_are_detected(self) -> None:
        ms_candles = parse_kline_csv(MS_ROWS, "BTC/USDT")
        us_candles = parse_kline_csv(US_ROWS, "BTC/USDT")
        assert [c.open_time for c in us_candles] == [c.open_time for c in ms_candles]

    def test_header_row_is_skipped(self) -> None:
        with_header = "open_time,open,high,low,close,volume,close_time\n" + MS_ROWS
        assert len(parse_kline_csv(with_header, "BTC/USDT")) == 2

    def test_blank_lines_are_ignored(self) -> None:
        assert len(parse_kline_csv("\n" + MS_ROWS + "\n\n", "BTC/USDT")) == 2

    def test_malformed_row_raises(self) -> None:
        with pytest.raises(ValueError, match="malformed kline row"):
            parse_kline_csv("1717200000000,67000.1\n", "BTC/USDT")

    def test_prices_are_exact_decimals(self) -> None:
        row = "1717200000000,0.00001234,0.00001240,0.00001230,0.00001236,1000.5,x,x,x,x,x,0\n"
        (candle,) = parse_kline_csv(row, "PEPE/USDT")
        assert candle.open_quote == Decimal("0.00001234")  # no float dust


class TestMonthRange:
    def test_spans_year_boundary(self) -> None:
        assert list(month_range(2025, 11, 2026, 2)) == [
            (2025, 11),
            (2025, 12),
            (2026, 1),
            (2026, 2),
        ]

    def test_single_month(self) -> None:
        assert list(month_range(2026, 3, 2026, 3)) == [(2026, 3)]

    def test_reversed_range_raises(self) -> None:
        with pytest.raises(ValueError, match="is after end"):
            list(month_range(2026, 4, 2026, 3))

    def test_invalid_month_raises(self) -> None:
        with pytest.raises(ValueError, match=r"1\.\.12"):
            list(month_range(2026, 0, 2026, 3))


def zip_bytes(csv_text: str, name: str = "klines.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, csv_text)
    return buffer.getvalue()


def make_mock_client(responses: dict[str, bytes]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in responses:
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, content=responses[url])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


DUMP_URL = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-06.zip"


class TestDownload:
    async def test_download_month_fetches_and_parses(self) -> None:
        client = make_mock_client({DUMP_URL: zip_bytes(MS_ROWS)})
        candles = await download_month(client, "BTC/USDT", 2024, 6)

        assert len(candles) == 2
        assert candles[0].symbol == "BTC/USDT"

    async def test_missing_dump_raises_http_error(self) -> None:
        client = make_mock_client({})
        with pytest.raises(httpx.HTTPStatusError):
            await download_month(client, "BTC/USDT", 2024, 6)

    async def test_symbol_without_slash_is_rejected(self) -> None:
        client = make_mock_client({})
        with pytest.raises(ValueError, match="must look like"):
            await download_month(client, "BTCUSDT", 2024, 6)

    async def test_history_stitches_contiguous_months(self) -> None:
        # 2024-06-30 23:59 UTC then 2024-07-01 00:00 UTC: contiguous boundary.
        june_row = "1719791940000,100,101,99,100,1,x,x,x,x,x,0\n"
        july_row = "1719792000000,100,101,99,100,1,x,x,x,x,x,0\n"
        july_url = DUMP_URL.replace("2024-06", "2024-07")
        client = make_mock_client({DUMP_URL: zip_bytes(june_row), july_url: zip_bytes(july_row)})
        candles = await download_history(client, "BTC/USDT", 2024, 6, 2024, 7)

        assert len(candles) == 2

    async def test_history_rejects_month_boundary_gap(self) -> None:
        june_row = "1719791940000,100,101,99,100,1,x,x,x,x,x,0\n"
        # July starts one minute late: a download problem, not an outage.
        july_row = "1719792060000,100,101,99,100,1,x,x,x,x,x,0\n"
        july_url = DUMP_URL.replace("2024-06", "2024-07")
        client = make_mock_client({DUMP_URL: zip_bytes(june_row), july_url: zip_bytes(july_row)})
        with pytest.raises(ValueError, match="month boundary mismatch"):
            await download_history(client, "BTC/USDT", 2024, 6, 2024, 7)

    async def test_history_rejects_empty_month(self) -> None:
        client = make_mock_client({DUMP_URL: zip_bytes("")})
        with pytest.raises(ValueError, match="empty dump"):
            await download_history(client, "BTC/USDT", 2024, 6, 2024, 6)
