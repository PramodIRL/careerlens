"""Regression tests for the test-database safety hardening added to
tests/conftest.py after the 2026-08-27 incident: a widened search_path,
combined with `create_all(checkfirst=True)`, let the isolated test
schema end up empty of tables, and the per-test TRUNCATE fell through
`search_path` into the development `public` schema and wiped real data.

Narrowly scoped to the three things that actually need proving here:

1. cleanup operates on `careerlens_test`
2. `public` is never touched by it, even when it plausibly could be
3. the pgvector `vector` type is still usable after the fix

Everything else about `_clean_tables` / `_isolated_test_schema` is
already exercised implicitly by every other test file in this suite —
each one depends on cleanup actually isolating it. This file is only
about the safety property itself.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models.embedding import EMBEDDING_DIMENSION, Embedding
from app.models.user import User
from app.settings import get_settings
from tests.conftest import (
    _MANAGED_TABLES,
    _SEARCH_PATH_CONNECT_ARGS,
    _assert_isolated_before_destructive_operation,
    truncate_managed_tables,
)

# Marks the one row this file writes into the developer's real `public`
# schema. Deliberately unmistakable: it identifies the row for cleanup
# (including self-healing after a crashed run) and could never collide
# with a real account.
_SENTINEL_EMAIL_PREFIX = "schema-safety-sentinel-"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """A session scoped to TEST_SCHEMA — same pattern every other
    DB-backed test file in this suite uses."""
    engine = create_async_engine(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
async def public_engine() -> AsyncGenerator[AsyncEngine, None]:
    """A connection to the SAME database with no test search-path
    override, for asserting about the development schema directly — and
    always by explicit `public.<table>` qualification, never by relying
    on where an unqualified name happens to resolve."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


# --- the safety assertion itself ---------------------------------------


@pytest.mark.anyio
async def test_the_assertion_passes_against_the_real_isolated_schema(db: AsyncSession) -> None:
    """The positive path: every table this suite manages genuinely lives
    in TEST_SCHEMA, so the guard that runs before every TRUNCATE does not
    raise."""
    await _assert_isolated_before_destructive_operation(await db.connection())


@pytest.mark.anyio
async def test_the_assertion_aborts_loudly_if_a_managed_table_is_missing(
    monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """Simulates the exact failure mode from 2026-08-27: a managed table
    that does not exist in TEST_SCHEMA. Must raise BEFORE any TRUNCATE
    would run, never silently continue."""
    monkeypatch.setattr(
        "tests.conftest._MANAGED_TABLES", (*_MANAGED_TABLES, "table_that_does_not_exist")
    )

    with pytest.raises(AssertionError, match="table_that_does_not_exist"):
        await _assert_isolated_before_destructive_operation(await db.connection())


@pytest.mark.anyio
async def test_the_assertion_aborts_loudly_if_test_schema_constant_drifts(
    monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """Every safety check in this file assumes TEST_SCHEMA names the
    isolated schema. If that constant were ever edited to something
    else, the assertion must fail immediately rather than silently
    checking (and later cleaning) the wrong schema."""
    monkeypatch.setattr("tests.conftest.TEST_SCHEMA", "not_careerlens_test")

    with pytest.raises(AssertionError, match="TEST_SCHEMA"):
        await _assert_isolated_before_destructive_operation(await db.connection())


# --- the load-bearing regression: public is never touched --------------


@pytest.mark.anyio
async def test_cleanup_truncates_test_schema_and_never_touches_public(
    db: AsyncSession, public_engine: AsyncEngine
) -> None:
    """Writes a sentinel row directly into `public.users` (explicitly
    schema-qualified — never by relying on where an unqualified name
    resolves), writes another into `careerlens_test.users` through the
    ordinary ORM path every ordinary test uses, runs the EXACT SAME
    cleanup function `_clean_tables` calls before every test, and then
    proves:

      * the TEST_SCHEMA row is gone (cleanup did its job)
      * the PUBLIC row survives completely untouched (cleanup did not
        reach across schemas — the 2026-08-27 failure mode)

    The public-schema sentinel is removed in a `finally` block regardless
    of outcome: this test must never itself leave stray data behind in
    the real development database.
    """
    sentinel_id = uuid.uuid4()
    sentinel_email = f"{_SENTINEL_EMAIL_PREFIX}{sentinel_id}@example.com"

    try:
        async with public_engine.begin() as conn:
            # Self-heal first: if a previous run of this test was killed
            # between its INSERT and its `finally`, its sentinel is still
            # sitting in the developer's real `public.users`. Clearing
            # stale sentinels by their unmistakable prefix keeps a crash
            # from accumulating junk in the dev database run after run.
            # Scoped to this test's own marker — it can never match a
            # real account.
            await conn.execute(
                text("DELETE FROM public.users WHERE email LIKE :prefix || '%'"),
                {"prefix": _SENTINEL_EMAIL_PREFIX},
            )
            await conn.execute(
                text(
                    "INSERT INTO public.users (id, email, hashed_password) "
                    "VALUES (:id, :email, 'not-a-real-hash')"
                ),
                {"id": sentinel_id, "email": sentinel_email},
            )

        db.add(User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", hashed_password="x"))
        await db.commit()
        assert await db.scalar(text("select count(*) from users")) == 1

        # Release this session's transaction before truncating. TRUNCATE
        # takes an ACCESS EXCLUSIVE lock, and the SELECT above leaves
        # this connection "idle in transaction" holding a conflicting
        # ACCESS SHARE lock — so `truncate_managed_tables()`, which runs
        # on its own connection, would block forever waiting on us.
        await db.rollback()

        await truncate_managed_tables()

        db.expire_all()
        assert await db.scalar(text("select count(*) from users")) == 0, (
            "cleanup must empty careerlens_test.users"
        )
        await db.rollback()

        async with public_engine.begin() as conn:
            surviving = await conn.scalar(
                text("select count(*) from public.users where id = :id"), {"id": sentinel_id}
            )
        assert surviving == 1, (
            "cleanup touched the development `public` schema — this is exactly "
            "the 2026-08-27 incident"
        )
    finally:
        async with public_engine.begin() as conn:
            await conn.execute(text("DELETE FROM public.users WHERE id = :id"), {"id": sentinel_id})
            assert (
                await conn.scalar(
                    text("select count(*) from public.users where email like :prefix || '%'"),
                    {"prefix": _SENTINEL_EMAIL_PREFIX},
                )
                == 0
            ), "this test must not leave its sentinel behind in the dev database"


# --- pgvector still works after the fix ---------------------------------


@pytest.mark.anyio
async def test_pgvector_vector_type_still_works_in_test_schema(db: AsyncSession) -> None:
    """Requirement: the hardening must not break the `vector` type
    resolving for DDL/DML inside TEST_SCHEMA. A direct, minimal round
    trip through the real `embeddings` table, through the ORM path (so
    the `Vector` column type's CAST/bind machinery is genuinely
    exercised) — independent of app/embeddings/store.py, which has its
    own exhaustive tests."""
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="x"))
    await db.commit()

    vector = [0.5] * EMBEDDING_DIMENSION
    db.add(
        Embedding(
            id=uuid.uuid4(),
            user_id=user_id,
            source_type="saved_job_description",
            source_id=str(uuid.uuid4()),
            chunk_index=0,
            content_hash="0" * 64,
            model_identifier="mock-deterministic-v1",
            embedding=vector,
        )
    )
    await db.commit()
    db.expire_all()

    # Read through the ORM, not raw text() SQL: a text() statement
    # carries no type information, so the `Vector` column type's
    # result_processor never runs and the raw pgvector text form comes
    # back as a string. Going through the typed column exercises the
    # bind AND result halves of the type, which is the point here.
    stored = await db.scalar(select(Embedding.embedding).where(Embedding.user_id == user_id))
    assert stored is not None
    assert len(stored) == EMBEDDING_DIMENSION
    assert stored == vector
