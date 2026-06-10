"""Persistence tests run against a real Postgres.

CI provides one as a service container; locally, set ``TEST_DATABASE_URL``
(or run a default-config Postgres). SQLite is deliberately not used as a
stand-in: it stores numerics as floats, which would fake-pass the exact
Decimal round-trip tests that matter most here.
"""

import os
from collections.abc import AsyncIterator

import pytest

from tradebot.persistence import Database
from tradebot.persistence.database import metadata

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
