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

import base64
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from app.github.base import (
    GitHubRateLimited,
    GitHubRepositoryNotFound,
    GitHubTimeout,
    GitHubUnavailable,
    GitHubUserNotFound,
)
from app.github.http import HttpGitHubClient
from app.settings import get_settings

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


# --------------------------------------------------------------------
# Prompt 3.2: repository listing, languages, README
# --------------------------------------------------------------------


def _repo_payload(**overrides: object) -> dict[str, object]:
    """A realistic repository payload, including fields we deliberately
    drop — `clone_url` and `owner` are here to show they are read and
    discarded, never stored."""
    payload: dict[str, object] = {
        "id": 1296269,
        "name": "hello-world",
        "full_name": "octocat/hello-world",
        "private": False,
        "fork": False,
        "archived": False,
        "description": "My first repository",
        "language": "Python",
        "stargazers_count": 42,
        "forks_count": 7,
        "topics": ["python", "cli"],
        "pushed_at": "2026-08-01T10:00:00Z",
        "created_at": "2025-01-01T10:00:00Z",
        "updated_at": "2026-08-01T10:00:00Z",
        "clone_url": "https://github.test/octocat/hello-world.git",
        "owner": {"login": "octocat", "avatar_url": "https://example.com/a.png"},
    }
    payload.update(overrides)
    return payload


@pytest.mark.anyio
async def test_list_repositories_parses_and_drops_what_it_should(anyio_backend: str) -> None:
    listing = await _client(_responds(200, json=[_repo_payload()])).list_repositories("octocat")

    assert listing.complete is True
    assert len(listing.repositories) == 1
    repo = listing.repositories[0]
    assert repo.id == 1296269
    assert repo.full_name == "octocat/hello-world"
    assert repo.topics == ["python", "cli"]
    assert repo.stargazers_count == 42
    # Dropped at the parse boundary — not merely "not persisted later".
    assert not hasattr(repo, "clone_url")
    assert not hasattr(repo, "owner")


@pytest.mark.anyio
async def test_list_repositories_follows_link_header_across_pages(anyio_backend: str) -> None:
    """Pagination follows the server's own rel="next" link rather than
    incrementing a counter — a counter silently truncates when a
    repository is added or removed mid-walk."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if "page=2" in str(request.url):
            # Last page: no Link header at all.
            return httpx.Response(200, json=[_repo_payload(id=2, name="second")])
        return httpx.Response(
            200,
            json=[_repo_payload(id=1, name="first")],
            headers={"Link": f'<{_BASE_URL}/users/octocat/repos?page=2>; rel="next"'},
        )

    listing = await _client(handler).list_repositories("octocat")

    assert listing.complete is True
    assert [repo.id for repo in listing.repositories] == [1, 2]
    assert len(requested) == 2
    assert "per_page=100" in requested[0]
    assert "type=owner" in requested[0]
    assert "sort=pushed" in requested[0]


@pytest.mark.anyio
async def test_list_repositories_stops_at_the_last_page(anyio_backend: str) -> None:
    """A Link header with only rel="prev" must not be mistaken for a
    next page — otherwise the walk never terminates."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json=[_repo_payload()],
            headers={"Link": f'<{_BASE_URL}/users/octocat/repos?page=1>; rel="prev"'},
        )

    listing = await _client(handler).list_repositories("octocat")

    assert calls["n"] == 1
    assert listing.complete is True


@pytest.mark.anyio
async def test_list_repositories_marks_incomplete_at_the_page_cap(
    anyio_backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing flag: a listing cut short must report
    complete=False, because the caller uses it to decide whether it may
    soft-delete repositories it did not see."""
    settings = get_settings()
    monkeypatch.setattr(settings, "github_max_repository_pages", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_repo_payload(id=1)],
            headers={"Link": f'<{_BASE_URL}/users/octocat/repos?page=99>; rel="next"'},
        )

    listing = await _client(handler).list_repositories("octocat")

    assert listing.complete is False
    assert len(listing.repositories) == 2


@pytest.mark.anyio
async def test_list_repositories_rate_limited_mid_pagination_raises(anyio_backend: str) -> None:
    """A rate limit on page two is still a rate limit — it must not be
    reported as a short-but-complete listing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"X-RateLimit-Remaining": "0"},
            )
        return httpx.Response(
            200,
            json=[_repo_payload()],
            headers={"Link": f'<{_BASE_URL}/users/octocat/repos?page=2>; rel="next"'},
        )

    with pytest.raises(GitHubRateLimited):
        await _client(handler).list_repositories("octocat")


@pytest.mark.anyio
async def test_list_repositories_404_is_user_not_found(anyio_backend: str) -> None:
    with pytest.raises(GitHubUserNotFound):
        await _client(_responds(404, json={"message": "Not Found"})).list_repositories("nobody")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a list"},
        [{"name": "missing-id"}],
        ["just a string"],
    ],
)
async def test_list_repositories_rejects_malformed_payloads(
    payload: object, anyio_backend: str
) -> None:
    with pytest.raises(GitHubUnavailable):
        await _client(_responds(200, json=payload)).list_repositories("octocat")


@pytest.mark.anyio
async def test_get_languages_parses_byte_counts(anyio_backend: str) -> None:
    handler = _responds(200, json={"Python": 12345, "HTML": 234})

    assert await _client(handler).get_languages("octocat/hello-world") == {
        "Python": 12345,
        "HTML": 234,
    }


@pytest.mark.anyio
async def test_get_languages_empty_dict_is_valid(anyio_backend: str) -> None:
    """A repository with no detected code answers `200 {}`. That is an
    answer, not a failure."""
    assert await _client(_responds(200, json={})).get_languages("octocat/empty") == {}


@pytest.mark.anyio
async def test_get_languages_404_is_repository_not_found(anyio_backend: str) -> None:
    """Distinct from GitHubUserNotFound: the account is fine, this one
    repository vanished between being listed and being fetched."""
    with pytest.raises(GitHubRepositoryNotFound):
        await _client(_responds(404, json={"message": "Not Found"})).get_languages("octocat/gone")


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [{"Python": "lots"}, ["Python"], "Python"])
async def test_get_languages_rejects_malformed_payloads(
    payload: object, anyio_backend: str
) -> None:
    with pytest.raises(GitHubUnavailable):
        await _client(_responds(200, json=payload)).get_languages("octocat/hello-world")


@pytest.mark.anyio
async def test_get_readme_decodes_base64_and_keeps_the_sha(anyio_backend: str) -> None:
    text = "# Hello World\n\nBuilt with Python and FastAPI.\n"
    handler = _responds(
        200,
        json={
            "encoding": "base64",
            "content": base64.b64encode(text.encode()).decode(),
            "sha": "abc123def456",
            "size": len(text.encode()),
        },
    )

    readme = await _client(handler).get_readme("octocat/hello-world")

    assert readme is not None
    assert readme.text == text
    # The SHA is what makes the snapshot traceable and lets a rerun skip
    # re-storing unchanged content.
    assert readme.sha == "abc123def456"
    assert readme.size_bytes == len(text.encode())


@pytest.mark.anyio
async def test_get_readme_404_returns_none_rather_than_raising(anyio_backend: str) -> None:
    """Most repositories have no README, and GitHub has no way to say
    that other than a 404. Treating it as an error would make the normal
    case look like a failure and inflate repositories_failed."""
    assert await _client(_responds(404, json={"message": "Not Found"})).get_readme("o/r") is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"encoding": "utf-8", "content": "plain", "sha": "abc", "size": 5},
        {"encoding": "base64", "sha": "abc", "size": 5},
        {"encoding": "base64", "content": "!!!not base64!!!", "size": 5},
        ["not an object"],
    ],
)
async def test_get_readme_rejects_unusable_payloads(payload: object, anyio_backend: str) -> None:
    with pytest.raises(GitHubUnavailable):
        await _client(_responds(200, json=payload)).get_readme("octocat/hello-world")


@pytest.mark.anyio
async def test_new_endpoints_send_no_credential_either(anyio_backend: str) -> None:
    """The Prompt 3.1 guarantee, re-asserted over every endpoint Prompt
    3.2 adds — a token accidentally introduced here would make things
    work better, not worse, so nothing else would catch it."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/languages"):
            return httpx.Response(200, json={"Python": 1})
        if request.url.path.endswith("/readme"):
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(b"# hi").decode(),
                    "sha": "s",
                    "size": 4,
                },
            )
        return httpx.Response(200, json=[_repo_payload()])

    client = _client(handler)
    await client.list_repositories("octocat")
    await client.get_languages("octocat/hello-world")
    await client.get_readme("octocat/hello-world")

    assert len(seen) == 3
    for request in seen:
        assert "authorization" not in {name.lower() for name in request.headers}
        assert request.headers["User-Agent"] == _USER_AGENT
