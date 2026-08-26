import asyncio
from collections.abc import AsyncGenerator, Callable, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  (registers ORM models onto Base.metadata)
from app.db import Base, build_session_factory
from app.main import app as fastapi_app
from app.settings import get_settings

# Dedicated Postgres schema used only by the test suite, so tests never
# read or write the dev database's default ("public") schema.
TEST_SCHEMA = "careerlens_test"
# `public` trails the test schema on the search path because that is
# where the pgvector EXTENSION lives — an extension belongs to exactly
# one schema per database, and both `infra/postgres/init.sql` and the
# enable-pgvector migration install it into `public`. Without it on the
# path, creating the `embeddings` table fails with `type "vector" does
# not exist`.
#
# This does not weaken the isolation the schema exists for: every
# product table is created in TEST_SCHEMA by `Base.metadata.create_all`
# below, and TEST_SCHEMA comes first, so a table name always resolves
# there. Only names that exist in NEITHER schema-qualified place — in
# practice, the extension's `vector` type — fall through to `public`.
# It is also how a real deployment is arranged: extensions in `public`,
# on the path, with the application's own tables wherever they live.
_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": f"{TEST_SCHEMA},public"}}


# Test engines use NullPool (see the calls below).
#
# Why it matters: a pooled connection outlives the event loop that opened
# it, and an asyncpg connection cannot be closed from a different loop —
# so the TestClient's per-test loop closes while its pooled connections
# stay open server-side, and disposing the engine afterwards cannot
# reclaim them. Left pooled, a full run's open-connection count climbs
# monotonically (measured: 5 -> 98 and still rising) until Postgres
# refuses new connections and whichever test happens to be running fails
# with TooManyConnectionsError, unrelated to the code under test.
#
# NullPool opens a connection per session and closes it when that session
# closes, inside the one loop that used it. Slightly more connection
# churn, in exchange for a suite whose connection use is flat.
def db_override_for(
    database_url: str,
    *,
    connect_args: dict[str, object] | None = None,
) -> Callable[[], AsyncGenerator[AsyncSession, None]]:
    """Build a FastAPI `get_db` override bound to the given database URL,
    without touching the app's real configuration. Shared by every test
    module that needs the database, rather than each reinventing this."""
    factory = build_session_factory(database_url, connect_args=connect_args, poolclass=NullPool)

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
    """A TestClient used as a context manager, with an https:// base URL.

    Two independent Starlette/httpx quirks, neither a production bug:

    1. TestClient only keeps a stable event loop/portal alive for the
       lifetime of its `with` block. Without this, a test that makes
       more than one request can fail on the second call with "Event
       loop is closed" — the app's async DB engine's pooled connections
       stay bound to the first (by-then-closed) loop. Only surfaces in
       multi-request tests (first hit in Prompt 1.1's auth tests).
    2. TestClient's ASGI transport defaults to a plain http:// base URL.
       httpx's cookie jar correctly (per RFC 6265) refuses to resend a
       Secure-flagged cookie on a non-HTTPS request — so the Secure
       refresh-token cookie (Prompt 1.2) would never come back
       automatically on a later request in the same test. There's no
       real TLS handshake here (the transport calls the ASGI app
       in-process), so an https:// base URL costs nothing and makes the
       jar behave the way it will over a real browser's HTTPS
       connection (or its "localhost is a secure context" exception).
    """
    with TestClient(fastapi_app, base_url="https://testserver") as c:
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
        engine = create_async_engine(
            settings.database_url,
            connect_args=_SEARCH_PATH_CONNECT_ARGS,
            poolclass=NullPool,
        )
        try:
            async with engine.begin() as conn:
                # Dropped first, so the schema is always built from
                # scratch. Only ever names TEST_SCHEMA — never the dev
                # schema — and it makes `checkfirst=False` below sound:
                # a run that crashed before its teardown would otherwise
                # leave tables behind for the next run to trip over.
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
                await conn.execute(text(f'CREATE SCHEMA "{TEST_SCHEMA}"'))
            async with engine.begin() as conn:
                # Tables are created straight from current ORM metadata,
                # not by replaying the Alembic migration chain — faster
                # for tests. Migration correctness itself is verified
                # separately, by actually running `alembic upgrade head`.
                #
                # checkfirst=False is REQUIRED, not an optimisation.
                # `public` trails TEST_SCHEMA on the search path (see
                # above), and the default checkfirst=True resolves "does
                # this table exist?" through that path — so it would find
                # the DEV schema's `users`, `skills` and the rest, decide
                # they already exist, and create nothing in TEST_SCHEMA.
                # Every test would then silently read and write the dev
                # database. Creating unconditionally into a schema we
                # just made empty is what keeps the isolation real.
                await conn.run_sync(Base.metadata.create_all, checkfirst=False)
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
        engine = create_async_engine(
            settings.database_url,
            connect_args=_SEARCH_PATH_CONNECT_ARGS,
            poolclass=NullPool,
        )
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "TRUNCATE refresh_tokens, users, profiles, skills, "
                        "profile_target_roles, profile_target_skills, resumes, "
                        "skill_aliases, skill_relations, candidate_skills, "
                        "skill_evidence, github_connections, github_repositories, "
                        "github_repository_languages, github_repository_topics, "
                        "github_ingestion_runs, candidate_qualifications, "
                        "job_eligibility_requirements, job_eligibility_requirement_values, "
                        "embeddings "
                        "RESTART IDENTITY CASCADE"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_truncate())
