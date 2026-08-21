import asyncio
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.settings import get_settings

# Dedicated Postgres schema used only by the test suite, so tests never
# read or write the dev database's default ("public") schema — even
# though there's no product data to collide with yet, this establishes
# the isolation pattern before later prompts add real tables.
TEST_SCHEMA = "careerlens_test"


@pytest.fixture(scope="session", autouse=True)
def _isolated_test_schema() -> Generator[None, None, None]:
    """Create an isolated schema for the test session, then drop it.

    Requires a reachable Postgres (e.g. `make start`, or
    `docker compose up -d postgres`) — the same instance local dev uses,
    just a separate schema within it.
    """
    settings = get_settings()

    # A fresh engine per asyncio.run() call, disposed within the same call:
    # reusing one engine across two separate asyncio.run() invocations
    # binds its connections to the first (already-closed) event loop and
    # raises "Future attached to a different loop" on the second use.
    async def _create() -> None:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{TEST_SCHEMA}"'))
        finally:
            await engine.dispose()

    async def _drop() -> None:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
        finally:
            await engine.dispose()

    asyncio.run(_create())
    yield
    asyncio.run(_drop())
