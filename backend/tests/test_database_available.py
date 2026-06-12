"""Guard against a silent false-green when Postgres is meant to be present.

Every DB-backed test skips itself when Postgres is unreachable — the right
default on a laptop without a database. But where the full suite is expected to
run (CI, release checks), a Postgres that failed to come up would let the whole
persistence/worker/API suite skip to a green that proves nothing. With
``TRADEBOT_REQUIRE_DB=1`` set this asserts the database is actually reachable,
so a broken service fails loudly instead of passing by omission.
"""

import os

import pytest
from sqlalchemy import text

from tradebot.persistence import Database

DEFAULT_URL = "postgresql+asyncpg://postgres:test@localhost:5432/tradebot_test"


@pytest.mark.skipif(
    os.environ.get("TRADEBOT_REQUIRE_DB") != "1",
    reason="DB is optional here; set TRADEBOT_REQUIRE_DB=1 to require it (CI does)",
)
async def test_postgres_is_reachable() -> None:
    """Fail (not skip) when the test database cannot be reached."""
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)
    database = Database(url)
    try:
        async with database.engine.connect() as connection:
            assert (await connection.execute(text("select 1"))).scalar_one() == 1
    finally:
        await database.engine.dispose()
