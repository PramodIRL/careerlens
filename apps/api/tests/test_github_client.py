"""Tests for the real GitHub REST client (app/github/http.py).

Driven entirely through `httpx.MockTransport` — httpx's own built-in
test transport, so no new dependency and, more importantly, NO NETWORK.
Nothing in this suite ever reaches api.github.com.

WHY THIS FILE EXISTS SEPARATELY FROM tests/test_github_connection_api.py.
Those tests swap in a fake client that raises canned domain errors,
which proves the route maps errors to statuses correctly — but proves
nothing whatsoever about whether the timeout, rate-limit and
payload-validation code actually works, because none of it runs. The
four scenarios Prompt 3.1 names (valid profile, nonexistent user,
timeout, rate limit) are only genuinely exercised here, against real
`httpx.Response` objects going through the real client.

Test functions are `async def` with an anyio backend, since the client
is async and there is no Celery-style `asyncio.run()` bridge in the way
(contrast tests/test_extraction.py, which must use sync defs).
"""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from app.github.base import (
    GitHubRateLimited,
    GitHubTimeout,
    GitHubUnavailable,
    GitHubUserNotFound,
)
from app.github.http import HttpGitHubClient

_BASE_URL = "https://api.github.test"
_USER_AGENT = "CareerLens-Test/0.1"

# A realistic GitHub user payload, trimmed to what matters plus a couple
# of fields we deliberately ignore — `email` and `avatar_url` are here
# precisely to show they are read and dropped, never stored.
_VALID_PAYLOAD = {
    "id": 583231,
    "login": "Octocat",
    "type": "User",
    "public_repos": 8,
    "email": "not-stored@example.com",
    "avatar_url": "https://example.com/avatar.png",
    "bio": "not stored either",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpGitHubClient:
    """A real HttpGitHubClient whose transport is `handler`, so every
    branch runs for real against a synthetic response."""
    return HttpGitHubClient(
        base_url=_BASE_URL,
        timeout_seconds=1.0,
        user_agent=_USER_AGENT,
        transport=httpx.MockTransport(handler),
    )


def _responds(
    status_code: int, *, json: object = None, headers: dict[str, str] | None = None, text: str = ""
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if json is not None:
            return httpx.Response(status_code, json=json, headers=headers)
        return httpx.Response(status_code, text=text, headers=headers)

    return handler


@pytest.mark.anyio
async def test_valid_profile_is_parsed(anyio_backend: str) -> None:
    user = await _client(_responds(200, json=_VALID_PAYLOAD)).get_user("octocat")

    assert user.id == 583231
    assert user.login == "Octocat"
    assert user.type == "User"
    assert user.public_repos == 8
    # The fields this product has no reason to keep are not on the model
    # at all — dropped at the parse boundary rather than "not persisted
    # later", which is a much harder guarantee to accidentally lose.
    assert not hasattr(user, "email")
    assert not hasattr(user, "avatar_url")
    assert not hasattr(user, "bio")


@pytest.mark.anyio
async def test_request_shape_and_absence_of_any_credential(anyio_backend: str) -> None:
    """The load-bearing security assertion of this prompt.

    CareerLens must never authenticate to GitHub in this flow, so the
    outgoing request is inspected directly: correct URL, the headers
    GitHub requires, and NO Authorization header. A regression here
    would be silent otherwise — an accidentally-added token would make
    everything work *better*, not worse.
    """
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json=_VALID_PAYLOAD)

    await _client(handler).get_user("octocat")
    request = seen["request"]

    assert str(request.url) == f"{_BASE_URL}/users/octocat"
    assert request.method == "GET"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert request.headers["User-Agent"] == _USER_AGENT

    assert "authorization" not in {name.lower() for name in request.headers}
    # No credential smuggled into the query string either.
    assert request.url.query == b""


@pytest.mark.anyio
async def test_nonexistent_user_raises_not_found(anyio_backend: str) -> None:
    with pytest.raises(GitHubUserNotFound):
        await _client(_responds(404, json={"message": "Not Found"})).get_user("nobody-here")


@pytest.mark.anyio
async def test_timeout_raises_github_timeout(anyio_backend: str) -> None:
    """A real httpx timeout exception raised by the transport, not a
    stubbed domain error — this is what proves the except clause in
    get_user() is wired to the right exception type."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout", request=request)

    with pytest.raises(GitHubTimeout):
        await _client(handler).get_user("octocat")


@pytest.mark.anyio
async def test_connect_timeout_also_raises_github_timeout(anyio_backend: str) -> None:
    """Both timeout phases map to the same domain error: from the user's
    side, "GitHub never answered" is one event."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated connect timeout", request=request)

    with pytest.raises(GitHubTimeout):
        await _client(handler).get_user("octocat")


@pytest.mark.anyio
async def test_rate_limit_403_carries_the_reset_time(anyio_backend: str) -> None:
    reset = datetime(2026, 8, 24, 15, 30, tzinfo=UTC)
    handler = _responds(
        403,
        json={"message": "API rate limit exceeded"},
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(reset.timestamp())),
        },
    )

    with pytest.raises(GitHubRateLimited) as excinfo:
        await _client(handler).get_user("octocat")

    assert excinfo.value.reset_at == reset


@pytest.mark.anyio
async def test_rate_limit_429_is_recognised(anyio_backend: str) -> None:
    handler = _responds(
        429, json={"message": "Too Many Requests"}, headers={"X-RateLimit-Remaining": "0"}
    )

    with pytest.raises(GitHubRateLimited) as excinfo:
        await _client(handler).get_user("octocat")

    # No reset header — the error still classifies correctly, it just
    # can't say when to come back.
    assert excinfo.value.reset_at is None


@pytest.mark.anyio
async def test_rate_limit_with_unparseable_reset_header_still_classifies(
    anyio_backend: str,
) -> None:
    handler = _responds(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "not-a-number"},
    )

    with pytest.raises(GitHubRateLimited) as excinfo:
        await _client(handler).get_user("octocat")

    assert excinfo.value.reset_at is None


@pytest.mark.anyio
async def test_plain_403_is_not_reported_as_a_rate_limit(anyio_backend: str) -> None:
    """The distinction the X-RateLimit-Remaining check exists to make.

    A 403 with quota remaining is a permission or abuse-detection
    response, not an exhausted budget — telling the user "come back in
    an hour" would be wrong and unactionable.
    """
    handler = _responds(403, json={"message": "Forbidden"}, headers={"X-RateLimit-Remaining": "57"})

    with pytest.raises(GitHubUnavailable):
        await _client(handler).get_user("octocat")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"login": "octocat", "type": "User", "public_repos": 8},  # missing id
        {"id": 1, "type": "User", "public_repos": 8},  # missing login
        {"id": 1, "login": "octocat", "public_repos": 8},  # missing type
        {"id": 1, "login": "octocat", "type": "User"},  # missing public_repos
        {"id": "not-an-int", "login": "octocat", "type": "User", "public_repos": 8},
        {"id": 1, "login": "octocat", "type": "User", "public_repos": "many"},
        [],  # a JSON array where an object was promised
        "just a string",
    ],
)
async def test_malformed_payload_raises_unavailable(payload: object, anyio_backend: str) -> None:
    """A 200 is not a promise about the body's shape. Every one of these
    would otherwise reach the database as a half-built row."""
    with pytest.raises(GitHubUnavailable):
        await _client(_responds(200, json=payload)).get_user("octocat")


@pytest.mark.anyio
async def test_non_json_body_raises_unavailable(anyio_backend: str) -> None:
    handler = _responds(200, text="<html>maintenance</html>")

    with pytest.raises(GitHubUnavailable):
        await _client(handler).get_user("octocat")


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_server_errors_raise_unavailable(status_code: int, anyio_backend: str) -> None:
    with pytest.raises(GitHubUnavailable):
        await _client(_responds(status_code, text="upstream error")).get_user("octocat")


@pytest.mark.anyio
async def test_transport_error_raises_unavailable(anyio_backend: str) -> None:
    """DNS failure, refused connection, TLS error — all "GitHub is not
    reachable", none of them allowed to escape as a raw httpx exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    with pytest.raises(GitHubUnavailable):
        await _client(handler).get_user("octocat")


@pytest.mark.anyio
async def test_organization_payload_is_returned_for_the_route_to_judge(
    anyio_backend: str,
) -> None:
    """The client reports what GitHub said; deciding that an
    organization is not a personal account is a product rule, and lives
    in app/api/v1/github_connection.py rather than here."""
    org = {"id": 6154722, "login": "example-org", "type": "Organization", "public_repos": 400}

    user = await _client(_responds(200, json=org)).get_user("example-org")

    assert user.type == "Organization"
