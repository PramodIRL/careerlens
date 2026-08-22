import asyncio
from collections.abc import AsyncGenerator, Callable, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.models  # noqa: F401  (registers ORM models onto Base.metadata)
from app.db import Base, build_session_factory
from app.main import app as fastapi_app
from app.settings import get_settings

# Dedicated Postgres schema used only by the test suite, so tests never
# read or write the dev database's default ("public") schema.
TEST_SCHEMA = "careerlens_test"
_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}


def db_override_for(
    database_url: str,
    *,
    connect_args: dict[str, object] | None = None,
) -> Callable[[], AsyncGenerator[AsyncSession, None]]:
    """Build a FastAPI `get_db` override bound to the given database URL,
    without touching the app's real configuration. Shared by every test
    module that needs the database, rather than each reinventing this."""
    factory = build_session_factory(database_url, connect_args=connect_args)

    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    return _get_db


def isolated_schema_override() -> Callable[[], AsyncGenerator[AsyncSession, None]]:
    """The common case: a get_db override pointed at the isolated test
    schema, using the app's real Postgres connection settings."""
    return db_override_for(get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """A TestClient used as a context manager.

    Starlette's TestClient only keeps a stable event loop/portal alive
    for the lifetime of its `with` block. Without this, a test that
    makes more than one request can fail on the second call with
    "Event loop is closed" — the app's async DB engine's pooled
    connections stay bound to the first (by-then-closed) loop. This only
    surfaces in multi-request tests (first hit in Prompt 1.1's auth
    tests), which is why earlier single-request test files got away
    without it.
    """
    with TestClient(fastapi_app) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def _isolated_test_schema() -> Generator[None, None, None]:
    """Create an isolated schema — and its tables — for the test session,
    then drop it.

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
        engine = create_async_engine(settings.database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{TEST_SCHEMA}"'))
            async with engine.begin() as conn:
                # Tables are created straight from current ORM metadata,
                # not by replaying the Alembic migration chain — faster
                # for tests. Migration correctness itself is verified
                # separately, by actually running `alembic upgrade head`.
                await conn.run_sync(Base.metadata.create_all)
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


@pytest.fixture(autouse=True)
def _clean_tables() -> None:
    """Truncate product tables before each test, so tests never see
    leftover rows from a previous test. A no-op for test files that
    don't touch the database."""
    settings = get_settings()

    async def _truncate() -> None:
        engine = create_async_engine(settings.database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("TRUNCATE refresh_tokens, users RESTART IDENTITY CASCADE"))
        finally:
            await engine.dispose()

    asyncio.run(_truncate())
