"""API tests for the public GitHub connection endpoints (Prompt 3.1).

The GitHub client is swapped for a fake through `app.dependency_overrides`
— no network, and no dependence on a real account existing. What these
tests pin down is the *route's* behaviour: status codes, persistence,
ownership, and the mapping from each upstream failure to the one message
a user should read. The client's own timeout/rate-limit/parsing logic is
tested for real, against httpx.MockTransport, in tests/test_github_client.py.

Two things here are security assertions rather than behaviour tests, and
should not be softened: that a request carrying a `password` field is
rejected outright, and that one user's connection is invisible and
untouchable from another user's session.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.github import (
    GitHubClient,
    GitHubRateLimited,
    GitHubTimeout,
    GitHubUnavailable,
    GitHubUser,
    GitHubUserNotFound,
    get_github_client,
)
from app.github.skill_evidence import extract_github_skill_evidence
from app.main import app
from app.models.candidate_skill import CandidateSkill
from app.models.github_connection import GitHubConnection
from app.models.github_repository import GitHubRepository, GitHubRepositoryLanguage
from app.models.skill_evidence import SkillEvidence
from app.rate_limit import _request_log
from app.settings import get_settings
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_BASE = "/api/v1/github-connection"


class FakeGitHubClient:
    """A GitHubClient that answers from a script instead of the network.

    Either returns `user` or raises `error`. Records every username it
    was asked about, so tests can assert what the route actually sent
    downstream — including that a normalized "@octocat" arrives as
    "octocat".
    """

    def __init__(self, *, user: GitHubUser | None = None, error: Exception | None = None) -> None:
        self._user = user
        self._error = error
        self.calls: list[str] = []

    async def get_user(self, username: str) -> GitHubUser:
        self.calls.append(username)
        if self._error is not None:
            raise self._error
        assert self._user is not None, "FakeGitHubClient needs either a user or an error"
        return self._user


def _github_user(
    *, id: int = 583231, login: str = "Octocat", type: str = "User", public_repos: int = 8
) -> GitHubUser:
    return GitHubUser(id=id, login=login, type=type, public_repos=public_repos)


def _use_client(client: GitHubClient) -> None:
    app.dependency_overrides[get_github_client] = lambda: client


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_github_client, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    # Every test registers/logs in at least one user through the shared
    # register/login/refresh budget (app/rate_limit.py). Same fixture as
    # tests/test_auth.py.
    _request_log.clear()


def _run[T](coro_factory: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run one async DB helper on its own engine and loop — the same
    asyncio.run-per-call pattern tests/conftest.py uses, so these sync
    TestClient tests can still read the database directly."""

    async def _inner() -> T:
        engine = create_async_engine(
            get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
        )
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                return await coro_factory(session)
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def _connection_row_count(user_id: str) -> int:
    async def _count(session: AsyncSession) -> int:
        result = await session.scalar(
            select(func.count())
            .select_from(GitHubConnection)
            .where(GitHubConnection.user_id == uuid.UUID(user_id))
        )
        return result or 0

    return _run(_count)


def _register_and_login(client: TestClient, email: str) -> tuple[str, str]:
    register = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    assert register.status_code == 201
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200
    return login.json()["access_token"], register.json()["id"]


def _new_user(client: TestClient) -> tuple[str, str]:
    return _register_and_login(client, f"{uuid.uuid4()}@example.com")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _connect(client: TestClient, token: str, username: str) -> Any:
    return client.put(_BASE, headers=_headers(token), json={"username": username})


# --------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------


def test_connect_valid_username_creates_the_connection(client: TestClient) -> None:
    _use_client(FakeGitHubClient(user=_github_user()))
    token, user_id = _new_user(client)

    response = _connect(client, token, "octocat")

    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] == user_id
    assert body["username"] == "Octocat"
    assert body["github_user_id"] == 583231
    assert body["public_repo_count"] == 8
    assert body["last_verified_at"] is not None
    # Nothing credential-shaped, and none of the public-but-irrelevant
    # profile fields GitHub also returns, is exposed.
    assert set(body) == {
        "user_id",
        "username",
        "github_user_id",
        "public_repo_count",
        "last_verified_at",
        "created_at",
        "updated_at",
    }


def test_connect_stores_githubs_canonical_casing_not_what_was_typed(client: TestClient) -> None:
    """GitHub lookups are case-insensitive, so the account's own spelling
    is the one worth keeping — otherwise the UI shows the user's typo
    back to them as if it were the account name."""
    _use_client(FakeGitHubClient(user=_github_user(login="OctoCat")))
    token, _ = _new_user(client)

    assert _connect(client, token, "OCTOCAT").json()["username"] == "OctoCat"


def test_connect_normalizes_before_calling_github(client: TestClient) -> None:
    fake = FakeGitHubClient(user=_github_user())
    _use_client(fake)
    token, _ = _new_user(client)

    assert _connect(client, token, "  @octocat  ").status_code == 201
    assert fake.calls == ["octocat"]


def test_reconnecting_the_same_username_reverifies(client: TestClient) -> None:
    fake = FakeGitHubClient(user=_github_user(public_repos=8))
    _use_client(fake)
    token, _ = _new_user(client)
    first = _connect(client, token, "octocat")
    assert first.status_code == 201

    _use_client(FakeGitHubClient(user=_github_user(public_repos=11)))
    second = _connect(client, token, "octocat")

    assert second.status_code == 200
    assert second.json()["public_repo_count"] == 11
    assert second.json()["last_verified_at"] >= first.json()["last_verified_at"]


# --------------------------------------------------------------------
# Replacement semantics — the 1:1-per-user contract
# --------------------------------------------------------------------


def test_connecting_a_second_account_replaces_the_first(client: TestClient) -> None:
    """Regression test for the one-connection-per-user contract.

    This is deliberately independent of the (intentionally absent)
    global uniqueness on `github_user_id`: no unique constraint is doing
    this work. Replacement holds because the row is KEYED on `user_id`,
    so a second connect updates that row rather than inserting a
    sibling. If someone later "fixes" the model by giving it its own
    surrogate id, this test fails at step 4 while everything else keeps
    passing — which is exactly the point.
    """
    token, user_id = _new_user(client)

    # 1. PUT username A -> 201, github_user_id A
    _use_client(FakeGitHubClient(user=_github_user(id=111, login="account-a", public_repos=3)))
    first = _connect(client, token, "account-a")
    assert first.status_code == 201
    assert first.json()["github_user_id"] == 111
    assert first.json()["username"] == "account-a"

    # 2. PUT username B -> 200, github_user_id B
    _use_client(FakeGitHubClient(user=_github_user(id=222, login="account-b", public_repos=9)))
    second = _connect(client, token, "account-b")
    assert second.status_code == 200
    assert second.json()["github_user_id"] == 222
    assert second.json()["username"] == "account-b"

    # 3. GET -> only B is present, with B's data throughout.
    current = client.get(_BASE, headers=_headers(token)).json()
    assert current["github_user_id"] == 222
    assert current["username"] == "account-b"
    assert current["public_repo_count"] == 9

    # 4. Exactly one row exists for this user — no orphaned A row.
    assert _connection_row_count(user_id) == 1

    # created_at survived the replacement (the row was updated, not
    # deleted and re-inserted), while updated_at moved.
    assert current["created_at"] == first.json()["created_at"]
    assert current["updated_at"] >= first.json()["updated_at"]


def test_two_users_may_connect_the_same_public_account(client: TestClient) -> None:
    """The deliberate absence of global uniqueness.

    Prompt 3.1 verifies that an account exists, never that the caller
    owns it — so a global unique on `github_user_id` would let whoever
    connects a username first permanently lock out its real owner. See
    app/models/github_connection.py.
    """
    _use_client(FakeGitHubClient(user=_github_user(id=999, login="shared-account")))

    token_a, user_a = _new_user(client)
    token_b, user_b = _new_user(client)

    assert _connect(client, token_a, "shared-account").status_code == 201
    assert _connect(client, token_b, "shared-account").status_code == 201

    assert _connection_row_count(user_a) == 1
    assert _connection_row_count(user_b) == 1


# --------------------------------------------------------------------
# Reading and disconnecting
# --------------------------------------------------------------------


def test_get_returns_null_when_not_connected(client: TestClient) -> None:
    """ "Not connected" is a normal state, not an error — the UI's most
    common case has no error branch to handle."""
    token, _ = _new_user(client)

    response = client.get(_BASE, headers=_headers(token))

    assert response.status_code == 200
    assert response.json() is None


def test_disconnect_removes_the_connection(client: TestClient) -> None:
    _use_client(FakeGitHubClient(user=_github_user()))
    token, user_id = _new_user(client)
    _connect(client, token, "octocat")

    assert client.delete(_BASE, headers=_headers(token)).status_code == 204
    assert client.get(_BASE, headers=_headers(token)).json() is None
    # A hard delete, not a tombstone: nothing re-creates a connection
    # automatically, so there is nothing for a tombstone to suppress.
    assert _connection_row_count(user_id) == 0


def test_disconnect_is_idempotent(client: TestClient) -> None:
    token, _ = _new_user(client)

    assert client.delete(_BASE, headers=_headers(token)).status_code == 204
    assert client.delete(_BASE, headers=_headers(token)).status_code == 204


def _seed_github_derived_skills(user_id: str) -> None:
    """Put a user in the state a completed import + Prompt 3.3 extraction
    would leave them in: one repository, and the candidate skills and
    evidence derived from it."""

    async def _seed(session: AsyncSession) -> None:
        await seed_skill_taxonomy(session)
        repository = GitHubRepository(
            id=uuid.uuid4(),
            user_id=uuid.UUID(user_id),
            github_repo_id=987_654,
            name="toolkit",
            full_name="octocat/toolkit",
            primary_language="Python",
        )
        session.add(repository)
        await session.flush()
        session.add(
            GitHubRepositoryLanguage(repository_id=repository.id, language="Java", byte_count=120)
        )
        await session.commit()
        await extract_github_skill_evidence(session, uuid.UUID(user_id))

    _run(_seed)


def _evidence_source_types(user_id: str) -> list[str]:
    async def _read(session: AsyncSession) -> list[str]:
        rows = (
            await session.scalars(
                select(SkillEvidence.source_type)
                .join(CandidateSkill, CandidateSkill.id == SkillEvidence.candidate_skill_id)
                .where(CandidateSkill.user_id == uuid.UUID(user_id))
            )
        ).all()
        return sorted(rows)

    return _run(_read)


def _candidate_skill_statuses(user_id: str) -> list[str]:
    async def _read(session: AsyncSession) -> list[str]:
        rows = (
            await session.scalars(
                select(CandidateSkill.status).where(CandidateSkill.user_id == uuid.UUID(user_id))
            )
        ).all()
        return sorted(rows)

    return _run(_read)


def test_disconnect_purges_github_derived_skill_evidence(client: TestClient) -> None:
    """Prompt 3.3 answers what 3.2 deferred. Unlike a deleted resume —
    whose evidence stays reconcilable because the extractor may run again
    — nothing will ever reconcile GitHub evidence after a disconnect, so
    leaving it would strand the user with skills citing repositories that
    no longer exist and no way to remove them."""
    _use_client(FakeGitHubClient(user=_github_user()))
    token, user_id = _new_user(client)
    _connect(client, token, "octocat")
    _seed_github_derived_skills(user_id)
    assert _evidence_source_types(user_id) == ["github", "github"]

    assert client.delete(_BASE, headers=_headers(token)).status_code == 204

    assert _evidence_source_types(user_id) == []
    # Both were unreviewed suggestions with no other support, so they go.
    assert _candidate_skill_statuses(user_id) == []


def test_disconnect_preserves_reviewed_skills_and_non_github_evidence(
    client: TestClient,
) -> None:
    _use_client(FakeGitHubClient(user=_github_user()))
    token, user_id = _new_user(client)
    _connect(client, token, "octocat")
    _seed_github_derived_skills(user_id)

    async def _review_and_add_manual(session: AsyncSession) -> None:
        rows = (
            await session.scalars(
                select(CandidateSkill).where(CandidateSkill.user_id == uuid.UUID(user_id))
            )
        ).all()
        # Confirm one of the GitHub-derived skills...
        rows[0].status = "confirmed"
        # ...and give another an independent manual assertion.
        session.add(
            SkillEvidence(
                candidate_skill_id=rows[1].id,
                source_type="manual",
                source_identifier=user_id,
                excerpt=None,
                extraction_method="manual_entry",
                confidence=Decimal("1.00"),
            )
        )
        await session.commit()

    _run(_review_and_add_manual)

    assert client.delete(_BASE, headers=_headers(token)).status_code == 204

    # The manual assertion survives; every github row is gone.
    assert _evidence_source_types(user_id) == ["manual"]
    # The confirmed decision survives even with no evidence left, and the
    # manually supported skill survives because it still has evidence.
    assert _candidate_skill_statuses(user_id) == ["confirmed", "suggested"]


# --------------------------------------------------------------------
# Upstream failures -> user-visible answers
# --------------------------------------------------------------------


def test_nonexistent_github_user_returns_404(client: TestClient) -> None:
    _use_client(FakeGitHubClient(error=GitHubUserNotFound("nope")))
    token, user_id = _new_user(client)

    response = _connect(client, token, "definitely-not-a-real-account")

    assert response.status_code == 404
    assert "no public GitHub account found" in response.json()["detail"]
    assert _connection_row_count(user_id) == 0


def test_timeout_returns_504(client: TestClient) -> None:
    _use_client(FakeGitHubClient(error=GitHubTimeout("timed out")))
    token, user_id = _new_user(client)

    response = _connect(client, token, "octocat")

    assert response.status_code == 504
    assert response.json()["detail"] == "GitHub did not respond in time — try again in a moment"
    assert _connection_row_count(user_id) == 0


def test_rate_limit_returns_503_with_reset_time_and_retry_after(client: TestClient) -> None:
    """503 rather than 429: GitHub is rate-limiting *us*, so this is a
    dependency being unavailable — a 429 would tell the user they did
    something too often, which is wrong and unactionable."""
    reset = datetime.now(UTC) + timedelta(minutes=12)
    _use_client(FakeGitHubClient(error=GitHubRateLimited(reset)))
    token, user_id = _new_user(client)

    response = _connect(client, token, "octocat")

    assert response.status_code == 503
    assert "rate limit was reached" in response.json()["detail"]
    assert reset.strftime("%H:%M") in response.json()["detail"]
    assert 0 < int(response.headers["Retry-After"]) <= 12 * 60
    assert _connection_row_count(user_id) == 0


def test_rate_limit_without_a_reset_time_still_returns_503(client: TestClient) -> None:
    _use_client(FakeGitHubClient(error=GitHubRateLimited(None)))
    token, _ = _new_user(client)

    response = _connect(client, token, "octocat")

    assert response.status_code == 503
    assert "rate limit was reached" in response.json()["detail"]
    assert "Retry-After" not in response.headers


def test_malformed_github_response_returns_503(client: TestClient) -> None:
    """From the user's side, "GitHub sent nonsense" and "GitHub is down"
    are the same event, and nothing different is available to do about
    either."""
    _use_client(FakeGitHubClient(error=GitHubUnavailable("unexpected payload")))
    token, user_id = _new_user(client)

    response = _connect(client, token, "octocat")

    assert response.status_code == 503
    assert response.json()["detail"] == "GitHub is unavailable right now — try again in a moment"
    assert _connection_row_count(user_id) == 0


def test_organization_account_is_rejected(client: TestClient) -> None:
    _use_client(FakeGitHubClient(user=_github_user(login="example-org", type="Organization")))
    token, user_id = _new_user(client)

    response = _connect(client, token, "example-org")

    assert response.status_code == 422
    assert "organization" in response.json()["detail"]
    assert _connection_row_count(user_id) == 0


def test_failure_messages_never_leak_upstream_internals(client: TestClient) -> None:
    """Curated, safe messages only — the same rule app/worker.py applies
    to `resumes.error_message`."""
    for error in (
        GitHubTimeout("ReadTimeout at https://api.github.com/users/x"),
        GitHubUnavailable("github returned status 502"),
        GitHubRateLimited(None),
    ):
        _use_client(FakeGitHubClient(error=error))
        token, _ = _new_user(client)
        detail = _connect(client, token, "octocat").json()["detail"]
        assert "api.github.com" not in detail
        assert "502" not in detail
        assert "Timeout" not in detail


# --------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "username", ["", "   ", "-bad", "bad-", "b--ad", "under_score", "a" * 40, "octo cat"]
)
def test_invalid_username_is_rejected_without_calling_github(
    client: TestClient, username: str
) -> None:
    """Validation happens before the network call, so a typo never spends
    the unauthenticated rate-limit budget (60/hour per IP)."""
    fake = FakeGitHubClient(user=_github_user())
    _use_client(fake)
    token, _ = _new_user(client)

    assert _connect(client, token, username).status_code == 422
    assert fake.calls == []


def test_full_url_is_rejected_with_an_actionable_message(client: TestClient) -> None:
    fake = FakeGitHubClient(user=_github_user())
    _use_client(fake)
    token, _ = _new_user(client)

    response = _connect(client, token, "https://github.com/octocat")

    assert response.status_code == 422
    assert "not the full GitHub URL" in str(response.json()["detail"])
    assert fake.calls == []


def test_a_request_carrying_a_password_is_rejected_outright(client: TestClient) -> None:
    """This product never asks for a GitHub password, and a 422 says so
    far more clearly than silently ignoring the field would. Nothing is
    stored, and no GitHub call is made."""
    fake = FakeGitHubClient(user=_github_user())
    _use_client(fake)
    token, user_id = _new_user(client)

    response = client.put(
        _BASE,
        headers=_headers(token),
        json={"username": "octocat", "password": "hunter2"},
    )

    assert response.status_code == 422
    assert fake.calls == []
    assert _connection_row_count(user_id) == 0
    # The error names the offending FIELD, so a client can see what to
    # remove.
    assert "password" in response.text

    # KNOWN LIMITATION, asserted here so it stays visible rather than
    # being discovered later: FastAPI's default 422 body for a forbidden
    # extra field includes an `input` key echoing the rejected VALUE back
    # to the sender. It is reflected only to the client that supplied it,
    # never stored and never written to a server log — but it would reach
    # a browser-side error reporter that captures response bodies.
    # Scrubbing `input` needs an app-wide RequestValidationError handler,
    # which is outside this prompt's scope; see docs/decisions.md.
    assert '"input"' in response.text


@pytest.mark.parametrize("field", ["token", "access_token", "personal_access_token"])
def test_credential_shaped_fields_are_all_rejected(client: TestClient, field: str) -> None:
    _use_client(FakeGitHubClient(user=_github_user()))
    token, user_id = _new_user(client)

    response = client.put(
        _BASE, headers=_headers(token), json={"username": "octocat", field: "secret-value"}
    )

    assert response.status_code == 422
    assert field in response.text
    # Nothing credential-shaped is ever read, stored, or forwarded to
    # GitHub — see the note on echoing in the password test above.
    assert _connection_row_count(user_id) == 0


# --------------------------------------------------------------------
# Authentication and cross-user isolation
# --------------------------------------------------------------------


def test_every_route_requires_authentication(client: TestClient) -> None:
    _use_client(FakeGitHubClient(user=_github_user()))

    assert client.get(_BASE).status_code == 401
    assert client.put(_BASE, json={"username": "octocat"}).status_code == 401
    assert client.delete(_BASE).status_code == 401


def test_one_users_connection_is_invisible_to_another(client: TestClient) -> None:
    """Cross-user isolation is STRUCTURAL here, not enforced by a check.

    There is no path parameter, body field or query parameter naming an
    owner — the row is keyed on `user_id` and `user_id` comes only from
    the JWT — so B cannot address A's connection even to be refused.
    That is why this asserts invisibility rather than a 403: there is no
    reachable 403 to assert.
    """
    _use_client(FakeGitHubClient(user=_github_user(id=111, login="account-a")))
    token_a, user_a = _new_user(client)
    assert _connect(client, token_a, "account-a").status_code == 201

    token_b, user_b = _new_user(client)

    # B sees nothing of A's.
    assert client.get(_BASE, headers=_headers(token_b)).json() is None
    # B's delete is a no-op on their own (absent) connection...
    assert client.delete(_BASE, headers=_headers(token_b)).status_code == 204
    # ...and leaves A's intact.
    assert client.get(_BASE, headers=_headers(token_a)).json()["github_user_id"] == 111
    assert _connection_row_count(user_a) == 1
    assert _connection_row_count(user_b) == 0


def test_connecting_as_one_user_never_writes_to_another(client: TestClient) -> None:
    _use_client(FakeGitHubClient(user=_github_user(id=333, login="account-c")))
    token_a, user_a = _new_user(client)
    _token_b, user_b = _new_user(client)

    assert _connect(client, token_a, "account-c").status_code == 201

    assert _connection_row_count(user_a) == 1
    assert _connection_row_count(user_b) == 0
