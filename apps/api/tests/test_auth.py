"""Tests for email/password authentication: registration, login,
protected routes, refresh-token rotation/revocation via an HttpOnly
cookie, logout, and expired access tokens."""

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
_REFRESH_COOKIE = "refresh_token"
_REFRESH_COOKIE_PATH = "/api/v1/auth"


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


def _refresh(client: TestClient, *, refresh_token: str | None = None) -> Response:
    """POST /refresh. With refresh_token given, temporarily set exactly
    that value in the client's own cookie jar (restoring whatever was
    there afterward) — used to test presenting an old/foreign/absent
    token without disturbing the rest of the test. Otherwise behaves
    like a real browser: whatever the jar currently holds is sent
    automatically. (Per-request `cookies=` on TestClient.post is
    deprecated by Starlette — this manipulates the jar directly instead,
    as its own deprecation notice recommends.)"""
    if refresh_token is None:
        return client.post("/api/v1/auth/refresh")

    # Operate on the raw jar rather than .get()/.set()/.delete() by name:
    # the server's real cookie is stored under TestClient's internal
    # test-domain, so a same-named synthetic entry (even path-matched)
    # makes those raise httpx.CookieConflict ("multiple cookies exist").
    # This app only ever sets one cookie, so save-clear-restore the whole
    # jar is simple and exact.
    saved_cookies = list(client.cookies.jar)
    client.cookies.jar.clear()
    client.cookies.set(_REFRESH_COOKIE, refresh_token, path=_REFRESH_COOKIE_PATH)
    try:
        return client.post("/api/v1/auth/refresh")
    finally:
        client.cookies.jar.clear()
        for cookie in saved_cookies:
            client.cookies.jar.set_cookie(cookie)


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
    assert body["expires_in"] == get_settings().jwt_access_token_expire_minutes * 60


def test_login_response_never_includes_the_raw_refresh_token(client: TestClient) -> None:
    """The whole point of the HttpOnly cookie: the raw refresh token must
    never appear anywhere JavaScript on the page could read it."""
    _register(client)

    response = _login(client)

    assert "refresh_token" not in response.json()


def test_login_sets_an_httponly_secure_samesite_cookie_scoped_to_auth(
    client: TestClient,
) -> None:
    _register(client)

    response = _login(client)

    assert response.cookies.get(_REFRESH_COOKIE)
    set_cookie = response.headers.get("set-cookie", "")
    assert f"{_REFRESH_COOKIE}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/api/v1/auth" in set_cookie


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


def test_refresh_without_a_cookie_is_rejected(client: TestClient) -> None:
    response = _refresh(client)  # no prior login: jar has no cookie at all

    assert response.status_code == 401


def test_refresh_rotates_the_cookie_and_rejects_the_old_token(client: TestClient) -> None:
    _register(client)
    _login(client)  # cookie now stored in the client's jar, like a browser
    old_refresh_token = client.cookies.get(_REFRESH_COOKIE)
    assert old_refresh_token

    refreshed = _refresh(client)  # uses the jar automatically
    assert refreshed.status_code == 200
    assert "refresh_token" not in refreshed.json()  # never in JSON, on refresh either

    new_refresh_token = client.cookies.get(_REFRESH_COOKIE)  # jar auto-updated
    assert new_refresh_token
    assert new_refresh_token != old_refresh_token

    reuse_attempt = _refresh(client, refresh_token=old_refresh_token)
    assert reuse_attempt.status_code == 401


def test_reusing_a_revoked_refresh_token_revokes_the_whole_session(client: TestClient) -> None:
    _register(client)
    _login(client)
    old_refresh_token = client.cookies.get(_REFRESH_COOKIE)
    assert old_refresh_token

    _refresh(client)  # rotates; jar now holds the new token
    new_refresh_token = client.cookies.get(_REFRESH_COOKIE)
    assert new_refresh_token

    # Reusing the now-rotated-away old token is a compromise signal: it
    # should revoke the token that replaced it too, not just itself.
    _refresh(client, refresh_token=old_refresh_token)
    still_using_new_token = _refresh(client, refresh_token=new_refresh_token)

    assert still_using_new_token.status_code == 401


def test_logout_revokes_and_clears_the_refresh_cookie(client: TestClient) -> None:
    _register(client)
    _login(client)
    refresh_token = client.cookies.get(_REFRESH_COOKIE)
    assert refresh_token

    logout = client.post("/api/v1/auth/logout")  # uses the jar's cookie
    assert logout.status_code == 204
    set_cookie = logout.headers.get("set-cookie", "")
    assert f'{_REFRESH_COOKIE}=""' in set_cookie or f"{_REFRESH_COOKIE}=" in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()

    reuse_after_logout = _refresh(client, refresh_token=refresh_token)
    assert reuse_after_logout.status_code == 401


def test_logout_without_a_session_is_a_no_op(client: TestClient) -> None:
    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 204


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
