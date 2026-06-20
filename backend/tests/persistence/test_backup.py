"""Backups: exact round trip, pinned SigV4 signature, and upload mechanics."""

import gzip
import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tradebot.core.models import Candle, CandleInterval, Fill, Side
from tradebot.persistence import CandleStore, Database, FillStore
from tradebot.persistence.backup import (
    S3Config,
    S3Uploader,
    dump_tables,
    restore_tables,
    run_backup,
    sigv4_headers,
)
from tradebot.persistence.database import metadata

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
DEFAULT_URL = "postgresql+asyncpg://postgres:test@localhost:5432/tradebot_test"


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)
    db = Database(url)
    try:
        async with db.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.run_sync(metadata.create_all)
    except Exception as error:  # pragma: no cover - environment-dependent
        await db.engine.dispose()
        pytest.skip(f"Postgres unavailable at {url}: {error}")
    async with db:
        yield db


async def seed(database: Database) -> None:
    await CandleStore(database).insert_batch(
        [
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=BASE_TIME,
                close_time=BASE_TIME + timedelta(minutes=1),
                open_quote=Decimal("100.1"),
                high_quote=Decimal("101"),
                low_quote=Decimal("99"),
                close_quote=Decimal("100.59999999"),  # exactness must survive
                volume_base=Decimal("2.5"),
            )
        ]
    )
    await FillStore(database).append(
        Fill(
            client_order_id="ord-1",
            symbol="BTC/USDT",
            side=Side.BUY,
            price_quote=Decimal("100.1"),
            quantity_base=Decimal("0.05"),
            fee_quote=Decimal("0.005005"),
            filled_at=BASE_TIME,
        )
    )


class TestDumpAndRestore:
    async def test_round_trip_is_exact(self, database: Database) -> None:
        await seed(database)
        archive = await dump_tables(database)

        # Wipe and restore into a fresh schema — the disaster scenario.
        async with database.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.run_sync(metadata.create_all)
        counts = await restore_tables(database, archive)

        assert counts == {"candles": 1, "fills": 1}
        (candle,) = await CandleStore(database).fetch_recent("BTC/USDT", CandleInterval.M1)
        assert candle.close_quote == Decimal("100.59999999")  # Decimal, not float
        assert candle.open_time == BASE_TIME  # timezone-aware UTC survived
        (fill,) = await FillStore(database).fetch_all()
        assert fill.fee_quote == Decimal("0.005005")

    async def test_round_trip_streams_many_rows_across_tables(self, database: Database) -> None:
        """Multi-row tables must dump and restore in full, not just a first chunk."""
        candle_store = CandleStore(database)
        await candle_store.insert_batch(
            [
                Candle(
                    symbol="BTC/USDT",
                    interval=CandleInterval.M1,
                    open_time=BASE_TIME + timedelta(minutes=minute),
                    close_time=BASE_TIME + timedelta(minutes=minute + 1),
                    open_quote=Decimal("100"),
                    high_quote=Decimal("101"),
                    low_quote=Decimal("99"),
                    close_quote=Decimal(f"100.{minute:02d}"),
                    volume_base=Decimal("1"),
                )
                for minute in range(50)
            ]
        )
        fill_store = FillStore(database)
        for index in range(20):
            await fill_store.append(
                Fill(
                    client_order_id=f"ord-{index}",
                    symbol="BTC/USDT",
                    side=Side.BUY,
                    price_quote=Decimal("100"),
                    quantity_base=Decimal("0.01"),
                    fee_quote=Decimal("0.001"),
                    filled_at=BASE_TIME + timedelta(minutes=index),
                )
            )

        archive = await dump_tables(database)
        async with database.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.run_sync(metadata.create_all)
        counts = await restore_tables(database, archive)

        assert counts == {"candles": 50, "fills": 20}
        assert len(await candle_store.fetch_recent("BTC/USDT", CandleInterval.M1, 100)) == 50
        assert len(await FillStore(database).fetch_all()) == 20

    async def test_unknown_version_is_refused(self, database: Database) -> None:
        bogus = gzip.compress(b'{"backup_version": 99}\n')
        with pytest.raises(ValueError, match="unsupported backup version"):
            await restore_tables(database, bogus)

    async def test_writes_after_restore_do_not_collide_on_autoincrement_id(
        self, database: Database
    ) -> None:
        """The recovered bot must be able to trade.

        Restore inserts rows with their original ids without advancing the
        owning sequence, so the next autoincrement INSERT would reuse id=1 and
        crash on the primary key. The first post-recovery write — exactly what
        a recovered bot does — must get a fresh id, not collide.
        """
        await seed(database)  # one fill -> id 1
        archive = await dump_tables(database)
        async with database.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.run_sync(metadata.create_all)
        await restore_tables(database, archive)

        fill_store = FillStore(database)
        # This is the write that crashed before the sequence reset.
        await fill_store.append(
            Fill(
                client_order_id="ord-after-restore",
                symbol="BTC/USDT",
                side=Side.BUY,
                price_quote=Decimal("100"),
                quantity_base=Decimal("0.01"),
                fee_quote=Decimal("0.001"),
                filled_at=BASE_TIME + timedelta(minutes=1),
            )
        )

        order_ids = {fill.client_order_id for fill in await fill_store.fetch_all()}
        assert order_ids == {"ord-1", "ord-after-restore"}  # both rows, no collision


class TestSigV4:
    def test_signature_matches_the_published_aws_test_vector(self) -> None:
        """AWS's documented S3 GET example: a wrong signer fails here, not in prod."""
        headers = sigv4_headers(
            method="GET",
            host="examplebucket.s3.amazonaws.com",
            path="/test.txt",
            payload_hash=hashlib.sha256(b"").hexdigest(),
            moment=datetime(2013, 5, 24, 0, 0, tzinfo=UTC),
            region="us-east-1",
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        )
        assert headers["x-amz-date"] == "20130524T000000Z"
        assert (
            "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request"
            in headers["Authorization"]
        )
        # Recomputed independently with botocore's reference signer for this
        # exact request shape (same signed-header set, frozen timestamp).
        assert headers["Authorization"].endswith(
            "Signature=df548e2ce037944d03f3e68682813b093763996d597cf890ca3d9037fd231eb4"
        )


class TestUploader:
    async def test_run_backup_puts_a_gzipped_archive(self, database: Database) -> None:
        await seed(database)
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        config = S3Config(
            endpoint="https://account.r2.cloudflarestorage.com",
            bucket="tradebot-backups",
            access_key="key",
            secret_key="secret",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            key = await run_backup(database, S3Uploader(config, client), "tradebot")

        (request,) = requests
        assert request.method == "PUT"
        assert str(request.url).startswith(
            "https://account.r2.cloudflarestorage.com/tradebot-backups/tradebot/"
        )
        assert key.endswith(".jsonl.gz")
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256")
        body = gzip.decompress(request.content).decode()
        assert '"table": "candles"' in body

    async def test_rejected_upload_raises_to_the_scheduler(self, database: Database) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="signature mismatch")

        config = S3Config(endpoint="https://s3.test", bucket="b", access_key="k", secret_key="s")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await run_backup(database, S3Uploader(config, client), "tradebot")
