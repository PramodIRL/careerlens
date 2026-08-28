from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import Pool

from app.settings import get_settings


class Base(DeclarativeBase):
    """Declarative base for ORM models.

    Intentionally empty — Prompt 0.3 only enables the pgvector extension.
    No user/profile/resume/job/skill or other product tables are defined
    here; those belong to later prompts.
    """


def build_session_factory(
    database_url: str,
    *,
    connect_args: dict[str, object] | None = None,
    poolclass: type[Pool] | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Build a session factory for the given database URL.

    Kept as a plain function (rather than only a module-level singleton)
    so tests can point it at an isolated schema, or at a deliberately
    unreachable address, without touching the app's real configuration.

    `poolclass` exists for the same reason: the test suite passes
    NullPool so that a connection is opened and closed inside the single
    event loop that uses it. Pooled connections outlive the loop that
    created them, and an async connection cannot be closed from a
    different loop — see tests/conftest.py. Production leaves this None
    and gets SQLAlchemy's normal pooling.
    """
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        # Fail fast on a readiness check instead of hanging on a dead host.
        connect_args={"timeout": 5, **(connect_args or {})},
        **({"poolclass": poolclass} if poolclass is not None else {}),
    )
    return async_sessionmaker(engine, expire_on_commit=False)


_session_factory = build_session_factory(get_settings().database_url)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    async with _session_factory() as session:
        yield session


async def release_session(db: AsyncSession) -> None:
    """Hand the pooled connection back BEFORE a long wait (Prompt 7.1b).

    WHY THIS EXISTS. `get_db` holds one session for the whole request,
    and a session keeps its connection checked out from the first query
    until it is closed. That is exactly right for a request that only
    talks to the database — and wrong for the two that then wait on a
    local language model for up to `explanation_timeout_seconds`, which
    is 180. Those routes read nothing after the model answers, so the
    connection was being held for a wait that cannot use it.

    The cost is not theoretical: the engine's default pool is five
    connections plus ten overflow. Fifteen concurrent explanations —
    a candidate expanding five saved jobs across three tabs — would hold
    every connection in the pool while generating, and `/match`, `/gaps`
    and every other endpoint would queue behind `pool_timeout` and then
    fail. The optional AI layer would have taken down the deterministic
    product, which is the one thing this architecture exists to prevent.

    WHAT THE CALLER MUST GUARANTEE. Every value still needed after this
    returns must already be MATERIALISED — a Pydantic model, a
    dataclass, or a plain scalar. `close()` expunges the session's
    instances, so an ORM object read afterwards would be detached and a
    lazy load would raise. Both call sites assemble their facts and
    their response inputs first, and neither touches the session again.

    NOT A ROLLBACK OF ANYTHING MEANINGFUL. Both callers are read-only —
    no INSERT, no UPDATE, no commit — so there is no work in flight to
    lose. `close()` ends the implicitly-begun read transaction and
    returns the connection to the pool.

    SAFE TO BE FOLLOWED BY `get_db`'s OWN TEARDOWN. The dependency's
    context manager closes the session again when the request ends;
    closing twice is a no-op.
    """
    await db.close()
