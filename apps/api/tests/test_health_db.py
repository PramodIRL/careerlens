from collections.abc import AsyncGenerator

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import build_session_factory, get_db
from app.main import app
from app.settings import get_settings
from tests.conftest import TEST_SCHEMA

client = TestClient(app)


def _override_get_db(
    database_url: str,
    *,
    connect_args: dict[str, object] | None = None,
) -> object:
    """Build a get_db override pointed at an isolated database URL,
    without touching the app's real configuration."""
    factory = build_session_factory(database_url, connect_args=connect_args)

    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    return _get_db


def test_health_db_reports_ok_when_database_is_reachable() -> None:
    settings = get_settings()
    app.dependency_overrides[get_db] = _override_get_db(
        settings.database_url,
        connect_args={"server_settings": {"search_path": TEST_SCHEMA}},
    )
    try:
        response = client.get("/api/v1/health/db")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_db_reports_degraded_when_database_is_unreachable() -> None:
    # Port 1 has nothing listening — connection refused, no real network
    # dependency, and fast (no need to wait out the connect timeout).
    unreachable_url = "postgresql+asyncpg://careerlens:changeme@localhost:1/careerlens"
    app.dependency_overrides[get_db] = _override_get_db(unreachable_url)
    try:
        response = client.get("/api/v1/health/db")
    finally:
        app.dependency_overrides.pop(get_db, None)

    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "degraded"
    # The response must never leak connection details or credentials.
    assert "changeme" not in response.text
    assert "localhost:1" not in response.text
