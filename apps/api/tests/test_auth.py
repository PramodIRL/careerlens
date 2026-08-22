"""Tests for email/password authentication: registration, login,
protected routes, refresh-token rotation/revocation, logout, and
expired access tokens."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.db import get_db
from app.main import app
from app.rate_limit import _request_log
from app.settings import get_settings
from tests.conftest import isolated_schema_override

_EMAIL = "alice@example.com"
_PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    # The rate limiter's counters are process-global state (see
    # app/rate_limit.py) — without resetting between tests, earlier
    # tests' requests would count against later, unrelated tests.
    _request_log.clear()


def _register(client: TestClient, email: str = _EMAIL, password: str = _PASSWORD) -> Response:
    return client.post("/api/v1/auth/register", json={"email": email, "password": password})


def _login(client: TestClient, email: str = _EMAIL, password: str = _PASSWORD) -> Response:
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_register_creates_user_without_leaking_password(client: TestClient) -> None:
    response = _register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == _EMAIL
    assert "password" not in body
    assert "hashed_password" not in body


def test_register_duplicate_email_is_rejected(client: TestClient) -> None:
    assert _register(client).status_code == 201

    response = _register(client, password="a-different-password")

    assert response.status_code == 409
    assert "already registered" in response.json()["detail"]


def test_login_succeeds_with_correct_credentials(client: TestClient) -> None:
    _register(client)

    response = _login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] == get_settings().jwt_access_token_expire_minutes * 60


def test_login_with_bad_password_returns_the_same_error_as_unknown_email(
    client: TestClient,
) -> None:
    _register(client)

    wrong_password = _login(client, password="wrong-password")
    unknown_email = _login(client, email="nobody@example.com")

    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401
    # Identical body in both cases: no signal about which part was wrong.
    assert wrong_password.json() == unknown_email.json()


def test_protected_route_requires_a_token(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401


def test_protected_route_accepts_a_valid_token(client: TestClient) -> None:
    _register(client)
    access_token = _login(client).json()["access_token"]

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})

    assert response.status_code == 200
    assert response.json()["email"] == _EMAIL


def test_refresh_rotates_the_token_and_rejects_the_old_one(client: TestClient) -> None:
    _register(client)
    old_refresh_token = _login(client).json()["refresh_token"]

    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh_token})
    assert refreshed.status_code == 200
    new_refresh_token = refreshed.json()["refresh_token"]
    assert new_refresh_token != old_refresh_token

    reuse_attempt = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh_token})
    assert reuse_attempt.status_code == 401


def test_reusing_a_revoked_refresh_token_revokes_the_whole_session(client: TestClient) -> None:
    _register(client)
    old_refresh_token = _login(client).json()["refresh_token"]
    new_refresh_token = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": old_refresh_token}
    ).json()["refresh_token"]

    # Reusing the now-rotated-away old token is a compromise signal: it
    # should revoke the token that replaced it too, not just itself.
    client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh_token})
    still_using_new_token = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": new_refresh_token}
    )

    assert still_using_new_token.status_code == 401


def test_logout_revokes_the_refresh_token(client: TestClient) -> None:
    _register(client)
    refresh_token = _login(client).json()["refresh_token"]

    logout = client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout.status_code == 204

    reuse_after_logout = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert reuse_after_logout.status_code == 401


def test_expired_access_token_is_rejected(client: TestClient) -> None:
    _register(client)
    access_token = _login(client).json()["access_token"]
    user_id = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    ).json()["id"]

    settings = get_settings()
    expired_token = jwt.encode(
        {
            "sub": user_id,
            "type": "access",
            "iat": datetime.now(UTC) - timedelta(minutes=20),
            "exp": datetime.now(UTC) - timedelta(minutes=5),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired_token}"})

    assert response.status_code == 401


def test_register_rejects_oversized_password_with_a_validation_error(client: TestClient) -> None:
    # 100 ASCII bytes: over bcrypt's 72-byte limit, under the (now-moot)
    # 128-character field limit — this is exactly the gap the bug lived in.
    response = _register(client, password="a" * 100)

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "Value error, password must be at most 72 bytes"

    # And no user was actually created — a follow-up register with the
    # same email at a valid length must succeed, not hit "already registered".
    assert _register(client, password="a-valid-password").status_code == 201


def test_register_accepts_a_password_at_the_72_byte_boundary(client: TestClient) -> None:
    response = _register(client, password="a" * 72)

    assert response.status_code == 201


def test_login_rejects_oversized_password_identically_for_existing_and_unknown_email(
    client: TestClient,
) -> None:
    _register(client)  # a real, existing account
    oversized_password = "a" * 100

    existing_email = _login(client, email=_EMAIL, password=oversized_password)
    unknown_email = _login(client, email="nobody@example.com", password=oversized_password)

    # Before the fix, an oversized password against a real email crashed
    # with 500 while an unknown email returned a clean 401 — a status-code
    # side channel that let an attacker detect which emails have accounts,
    # defeating the identical-error design in the other login test above.
    assert existing_email.status_code == 422
    assert unknown_email.status_code == 422
    assert existing_email.json() == unknown_email.json()


def test_rate_limit_blocks_excess_requests_to_auth_endpoints(client: TestClient) -> None:
    settings = get_settings()
    for _ in range(settings.auth_rate_limit_max_requests):
        response = _login(client, email="nobody@example.com", password="x")
        assert response.status_code == 401  # each individually within budget

    blocked = _login(client, email="nobody@example.com", password="x")

    assert blocked.status_code == 429
