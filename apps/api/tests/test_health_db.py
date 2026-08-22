from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from tests.conftest import db_override_for, isolated_schema_override


def test_health_db_reports_ok_when_database_is_reachable(client: TestClient) -> None:
    app.dependency_overrides[get_db] = isolated_schema_override()
    try:
        response = client.get("/api/v1/health/db")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_db_reports_degraded_when_database_is_unreachable(client: TestClient) -> None:
    # Port 1 has nothing listening — connection refused, no real network
    # dependency, and fast (no need to wait out the connect timeout).
    unreachable_url = "postgresql+asyncpg://careerlens:changeme@localhost:1/careerlens"
    app.dependency_overrides[get_db] = db_override_for(unreachable_url)
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
