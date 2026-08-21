from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

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
) -> async_sessionmaker[AsyncSession]:
    """Build a session factory for the given database URL.

    Kept as a plain function (rather than only a module-level singleton)
    so tests can point it at an isolated schema, or at a deliberately
    unreachable address, without touching the app's real configuration.
    """
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        # Fail fast on a readiness check instead of hanging on a dead host.
        connect_args={"timeout": 5, **(connect_args or {})},
    )
    return async_sessionmaker(engine, expire_on_commit=False)


_session_factory = build_session_factory(get_settings().database_url)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    async with _session_factory() as session:
        yield session
