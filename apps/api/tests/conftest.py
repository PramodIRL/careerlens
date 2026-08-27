import asyncio
from collections.abc import AsyncGenerator, Callable, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  (registers ORM models onto Base.metadata)
from app.db import Base, build_session_factory
from app.main import app as fastapi_app
from app.settings import get_settings

# Dedicated Postgres schema used only by the test suite, so tests never
# read or write the dev database's default ("public") schema.
TEST_SCHEMA = "careerlens_test"
# `public` trails the test schema on the search path SOLELY so that the
# bare `vector` type name inside `embeddings`'s column DDL resolves — an
# extension belongs to exactly one schema per database, and both
# `infra/postgres/init.sql` and the enable-pgvector migration install it
# into `public`.
#
# THIS NO LONGER DETERMINES WHERE TABLES LIVE. On 2026-08-27, exactly
# this search path — combined with `create_all(checkfirst=True)` on a
# session where `public` already held every table (the dev database) —
# caused table creation to be silently skipped in TEST_SCHEMA, which
# left `_clean_tables`' unqualified TRUNCATE below with nothing to
# resolve to except `public`, and it wiped the real development data.
# The fix is not a different search path — it's that table CREATION
# (via `schema_translate_map`, see `_isolated_test_schema`) and table
# CLEANUP (via literal `TEST_SCHEMA.<table>` names, see `_clean_tables`)
# no longer consult this value at all. It is kept, deliberately narrowed
# to "the extension type's schema", not "where things resolve".
_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": f"{TEST_SCHEMA},public"}}

# The complete set of tables `_clean_tables` truncates before every
# test. A single tuple, not a list duplicated into both the safety
# assertion and the TRUNCATE statement, so the two can never drift out
# of step with each other.
_MANAGED_TABLES = (
    "refresh_tokens",
    "users",
    "profiles",
    "skills",
    "profile_target_roles",
    "profile_target_skills",
    "resumes",
    "skill_aliases",
    "skill_relations",
    "candidate_skills",
    "skill_evidence",
    "github_connections",
    "github_repositories",
    "github_repository_languages",
    "github_repository_topics",
    "github_ingestion_runs",
    "candidate_qualifications",
    "job_eligibility_requirements",
    "job_eligibility_requirement_values",
    "embeddings",
)


async def _assert_isolated_before_destructive_operation(conn: AsyncConnection) -> None:
    """Refuse to proceed unless every managed table demonstrably lives in
    `TEST_SCHEMA` — called immediately before `_clean_tables`' TRUNCATE,
    and right after `_isolated_test_schema` creates the tables, so a
    broken setup is caught at session start rather than on first use.

    DELIBERATELY NOT A `search_path` CHECK. `to_regclass()` given a
    dot-qualified name (`'careerlens_test.users'`) resolves ONLY that
    literal schema — per the PostgreSQL documentation, a schema-qualified
    name bypasses `search_path` entirely — so a pass here is not "the
    search path currently happens to point the right way", it is "this
    exact relation exists in this exact schema", independent of any
    connection setting. That is what makes this assertion a genuine
    second, independent proof, not a restatement of the same mechanism
    that failed on 2026-08-27.
    """
    assert TEST_SCHEMA == "careerlens_test", (
        f"refusing to touch test data: TEST_SCHEMA is {TEST_SCHEMA!r}, not the "
        "expected 'careerlens_test' — every safety check in this file assumes "
        "this constant names the isolated schema, so a change here must not "
        "pass silently"
    )

    missing = (
        await conn.scalars(
            # CAST(:names AS text[]) — not left to asyncpg to infer: an
            # untyped array parameter makes `unnest` ambiguous (it is
            # overloaded for every array element type), which fails
            # outright rather than silently picking the wrong one. Note
            # this is NOT written as `:names::text[]` — text()'s own
            # bind-parameter parser treats a colon immediately following
            # a parameter name as the START of a second parameter, not a
            # cast, so that spelling fails to bind at all.
            text(
                "select t.table_name "
                "from unnest(CAST(:names AS text[])) as t(table_name) "
                "where to_regclass(:schema || '.' || t.table_name) is null"
            ).bindparams(names=list(_MANAGED_TABLES), schema=TEST_SCHEMA)
        )
    ).all()
    if missing:
        raise AssertionError(
            f"refusing to run test cleanup: {sorted(missing)} do not exist in "
            f"schema {TEST_SCHEMA!r}. Test cleanup must NEVER fall through to "
            "another schema — this is exactly how the 2026-08-27 incident "
            "wiped the development database. Fix the isolated schema; do not "
            "relax this check."
        )


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
                # schema_translate_map={None: TEST_SCHEMA} is what places
                # every table (none of our models set an explicit
                # `schema=`) into TEST_SCHEMA — an execution-time DDL
                # rewrite, NOT a search_path lookup, so it is unaffected
                # by whatever `search_path` this connection carries.
                # `checkfirst=False` remains required alongside it: with
                # `public` still on the search path for the `vector`
                # type (see `_SEARCH_PATH_CONNECT_ARGS`), the default
                # checkfirst=True would still ask "does this table exist
                # ANYWHERE on the path" — find it in the dev schema `public`
                # — and skip creating it here, leaving TEST_SCHEMA empty
                # exactly as it did in the 2026-08-27 incident. Creating
                # unconditionally into a schema we just made empty is
                # what keeps the isolation real regardless of search_path.
                qualified = await conn.execution_options(schema_translate_map={None: TEST_SCHEMA})
                await qualified.run_sync(Base.metadata.create_all, checkfirst=False)
                # Verify setup succeeded before any test runs, rather
                # than discovering a broken schema only when the first
                # test's cleanup assertion fails.
                await _assert_isolated_before_destructive_operation(qualified)
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


async def truncate_managed_tables() -> None:
    """Truncate every table in `_MANAGED_TABLES`, scoped to `TEST_SCHEMA`.

    Module-level (not nested inside the `_clean_tables` fixture below)
    so that tests/test_schema_safety.py can call the EXACT same cleanup
    path the fixture uses on every test, rather than a copy of it that
    could drift out of sync and stop proving anything.
    """
    engine = create_async_engine(
        get_settings().database_url,
        connect_args=_SEARCH_PATH_CONNECT_ARGS,
        poolclass=NullPool,
    )
    try:
        async with engine.begin() as conn:
            # The safety assertion runs INSIDE the same transaction as
            # the TRUNCATE it guards, immediately before it — not
            # earlier, and not on a different connection — so there is
            # no window between "verified safe" and "destructive
            # statement runs" for anything to change in between.
            await _assert_isolated_before_destructive_operation(conn)

            # Every table name is schema-qualified (`TEST_SCHEMA.<table>`),
            # generated from the single `_MANAGED_TABLES` tuple. This is
            # what actually prevents a repeat of the 2026-08-27 incident:
            # an unqualified TRUNCATE resolves through search_path and
            # can silently land somewhere else if that path is ever
            # misconfigured (which is exactly what happened); a
            # schema-qualified one names its target outright and fails
            # loudly rather than falling through to `public` if
            # `TEST_SCHEMA.<table>` does not exist.
            qualified_tables = ", ".join(f"{TEST_SCHEMA}.{table}" for table in _MANAGED_TABLES)
            await conn.execute(text(f"TRUNCATE {qualified_tables} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables() -> None:
    """Truncate product tables before each test, so tests never see
    leftover rows from a previous test. A no-op for test files that
    don't touch the database."""
    asyncio.run(truncate_managed_tables())
