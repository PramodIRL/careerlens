"""Tests for the app-level CORS configuration (app/main.py): the
browser's preflight (OPTIONS + Access-Control-Request-Method) must
allow every HTTP method the API's routes actually use, for the
configured web origin — and must still reject an unconfigured origin.

Regression coverage for the Prompt 1.3 CORS bug: the profile endpoints
added PATCH, but CORSMiddleware's `allow_methods` wasn't updated to
match, so the browser's preflight for `PATCH /api/v1/profiles/{id}`
was rejected with a 400 *by the middleware itself* — before the real
PATCH request was ever sent, and regardless of what the route allowed.
Reproduced live: the FastAPI dev server logged
`OPTIONS /api/v1/profiles/{id} HTTP/1.1" 400 Bad Request` and the
browser's Network panel showed no follow-up PATCH at all.
"""

import uuid

from fastapi.testclient import TestClient

_ALLOWED_ORIGIN = "http://localhost:3000"  # main.py's WEB_ORIGIN default
_DISALLOWED_ORIGIN = "http://evil.example.com"


def test_preflight_allows_patch_on_the_profile_endpoint(client: TestClient) -> None:
    response = client.options(
        f"/api/v1/profiles/{uuid.uuid4()}",
        headers={
            "Origin": _ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "PATCH",
        },
    )

    assert response.status_code == 200
    assert "PATCH" in response.headers.get("access-control-allow-methods", "")


def test_preflight_still_allows_post_on_auth_endpoints(client: TestClient) -> None:
    """Non-regression: the fix must not have narrowed what was already
    allowed for the existing auth endpoints (Prompt 1.1)."""
    response = client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": _ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert "POST" in response.headers.get("access-control-allow-methods", "")


def test_preflight_from_an_unconfigured_origin_is_still_rejected(client: TestClient) -> None:
    """The fix widened allow_methods only — allow_origins is untouched,
    so a browser origin that was never configured must still be
    refused (no Access-Control-Allow-Origin header for it to see)."""
    response = client.options(
        f"/api/v1/profiles/{uuid.uuid4()}",
        headers={
            "Origin": _DISALLOWED_ORIGIN,
            "Access-Control-Request-Method": "PATCH",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
