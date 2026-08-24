"""API tests for the GitHub ingestion endpoints (Prompt 3.2).

The Celery enqueue is stubbed to a recorder: these tests are about the
routes — status codes, the one-active-run rule, what the response
exposes, and ownership — not about the ingestion itself, which is
covered against a fake client in tests/test_github_ingestion.py. No
network anywhere.

The security assertions here are that one user's import is invisible and
untouchable from another user's session, and that disconnecting a GitHub
account really does remove the imported data rather than orphaning it.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.main import app
from app.models.github_connection import GitHubConnection
from app.models.github_ingestion_run import GitHubIngestionRun
from app.models.github_repository import GitHubRepository, GitHubRepositoryTopic
from app.rate_limit import _request_log
from app.schemas.github_ingestion import IngestionStatus
from app.settings import get_settings
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_CONNECTION = "/api/v1/github-connection"
_INGESTIONS = f"{_CONNECTION}/ingestions"
_LATEST = f"{_INGESTIONS}/latest"
_REPOSITORIES = f"{_CONNECTION}/repositories"


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    _request_log.clear()


@pytest.fixture(autouse=True)
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records what the route asked to have ingested, without touching
    Redis. The ingestion itself is tested elsewhere."""
    recorded: list[str] = []
    monkeypatch.setattr(
        "app.api.v1.github_ingestion.enqueue_github_ingestion",
        lambda run_id: recorded.append(str(run_id)),
    )
    return recorded


def _run[T](coro_factory: Callable[[AsyncSession], Awaitable[T]]) -> T:
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


def _register_and_login(client: TestClient) -> tuple[str, str]:
    email = f"{uuid.uuid4()}@example.com"
    register = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    assert register.status_code == 201
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200
    return login.json()["access_token"], register.json()["id"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _connect(user_id: str, username: str = "octocat") -> None:
    async def _insert(session: AsyncSession) -> None:
        session.add(
            GitHubConnection(
                user_id=uuid.UUID(user_id),
                github_user_id=583231,
                username=username,
                public_repo_count=3,
                last_verified_at=datetime.now(UTC),
            )
        )
        await session.commit()

    _run(_insert)


def _add_repository(user_id: str, *, repo_id: int, name: str, topics: list[str]) -> uuid.UUID:
    async def _insert(session: AsyncSession) -> uuid.UUID:
        repository = GitHubRepository(
            user_id=uuid.UUID(user_id),
            github_repo_id=repo_id,
            name=name,
            full_name=f"octocat/{name}",
            description="a repository",
            primary_language="Python",
            stargazers_count=5,
            forks_count=1,
            pushed_at=datetime.now(UTC),
            readme_text="# Hello",
            readme_sha="sha-1",
            readme_byte_size=7,
            detail_fetched_at=datetime.now(UTC),
        )
        session.add(repository)
        await session.commit()
        await session.refresh(repository)
        for topic in topics:
            session.add(GitHubRepositoryTopic(repository_id=repository.id, topic=topic))
        await session.commit()
        return repository.id

    return _run(_insert)


def _mark_deleted(repository_id: uuid.UUID) -> None:
    async def _update(session: AsyncSession) -> None:
        repository = await session.get(GitHubRepository, repository_id)
        assert repository is not None
        repository.deleted_at = datetime.now(UTC)
        await session.commit()

    _run(_update)


def _count(model: type, user_id: str) -> int:
    async def _do(session: AsyncSession) -> int:
        result = await session.scalar(
            select(func.count()).select_from(model).where(model.user_id == uuid.UUID(user_id))
        )
        return result or 0

    return _run(_do)


# --------------------------------------------------------------------
# Starting an import
# --------------------------------------------------------------------


def test_start_ingestion_queues_a_run(client: TestClient, enqueued: list[str]) -> None:
    token, user_id = _register_and_login(client)
    _connect(user_id)

    response = client.post(_INGESTIONS, headers=_headers(token))

    # 202: accepted, not completed. The client polls for progress.
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == IngestionStatus.QUEUED.value
    assert body["user_id"] == user_id
    # Null until the listing finishes — that null is the "still working
    # out how much there is" state, not a zero.
    assert body["repositories_available"] is None
    assert body["repositories_total"] is None
    assert body["repositories_completed"] == 0
    assert body["repositories_failed"] == 0
    assert enqueued == [body["id"]]


def test_start_ingestion_without_a_connection_is_409(
    client: TestClient, enqueued: list[str]
) -> None:
    token, _ = _register_and_login(client)

    response = client.post(_INGESTIONS, headers=_headers(token))

    assert response.status_code == 409
    assert "connect a public GitHub account" in response.json()["detail"]
    assert enqueued == []


def test_a_second_import_while_one_is_active_is_409(
    client: TestClient, enqueued: list[str]
) -> None:
    """One active run per user, so a double-clicked button cannot start
    two runs racing to upsert the same repositories."""
    token, user_id = _register_and_login(client)
    _connect(user_id)

    assert client.post(_INGESTIONS, headers=_headers(token)).status_code == 202
    second = client.post(_INGESTIONS, headers=_headers(token))

    assert second.status_code == 409
    assert second.json()["detail"] == "an import is already in progress"
    assert len(enqueued) == 1
    assert _count(GitHubIngestionRun, user_id) == 1


def test_a_new_import_is_allowed_once_the_previous_one_finished(
    client: TestClient, enqueued: list[str]
) -> None:
    """The partial unique index covers queued/processing only — a user
    may have any number of finished runs."""
    token, user_id = _register_and_login(client)
    _connect(user_id)
    first = client.post(_INGESTIONS, headers=_headers(token)).json()

    async def _finish(session: AsyncSession) -> None:
        run = await session.get(GitHubIngestionRun, uuid.UUID(first["id"]))
        assert run is not None
        run.status = IngestionStatus.SUCCEEDED.value
        await session.commit()

    _run(_finish)

    assert client.post(_INGESTIONS, headers=_headers(token)).status_code == 202
    assert _count(GitHubIngestionRun, user_id) == 2


# --------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------


def test_latest_is_null_before_any_import(client: TestClient) -> None:
    token, _ = _register_and_login(client)

    response = client.get(_LATEST, headers=_headers(token))

    assert response.status_code == 200
    assert response.json() is None


def test_latest_returns_the_most_recent_run(client: TestClient) -> None:
    token, user_id = _register_and_login(client)
    _connect(user_id)
    first = client.post(_INGESTIONS, headers=_headers(token)).json()

    async def _finish_and_progress(session: AsyncSession) -> None:
        run = await session.get(GitHubIngestionRun, uuid.UUID(first["id"]))
        assert run is not None
        run.status = IngestionStatus.SUCCEEDED.value
        run.repositories_available = 47
        run.repositories_forks_excluded = 5
        run.repositories_total = 20
        run.repositories_completed = 18
        run.repositories_failed = 2
        await session.commit()

    _run(_finish_and_progress)

    body = client.get(_LATEST, headers=_headers(token)).json()

    # All three counts are distinct and all three are exposed — without
    # them a UI can only say "20 imported", which reads as "you have 20
    # repositories".
    assert body["repositories_available"] == 47
    assert body["repositories_forks_excluded"] == 5
    assert body["repositories_total"] == 20
    assert body["repositories_completed"] == 18
    assert body["repositories_failed"] == 2


# --------------------------------------------------------------------
# Imported repositories
# --------------------------------------------------------------------


def test_repositories_lists_what_was_imported(client: TestClient) -> None:
    token, user_id = _register_and_login(client)
    _connect(user_id)
    _add_repository(user_id, repo_id=1, name="alpha", topics=["cli", "python"])

    body = client.get(_REPOSITORIES, headers=_headers(token)).json()

    assert len(body) == 1
    assert body[0]["full_name"] == "octocat/alpha"
    assert body[0]["topics"] == ["cli", "python"]
    assert body[0]["has_readme"] is True
    assert body[0]["detail_fetched"] is True
    # The README text itself is deliberately NOT shipped to a list view —
    # it exists for server-side excerpt extraction in Prompt 3.3.
    assert "readme_text" not in body[0]


def test_soft_deleted_repositories_are_not_listed(client: TestClient) -> None:
    """The row is kept so Prompt 3.3's evidence stays explicable, but a
    repository that is no longer on GitHub is not shown as current."""
    token, user_id = _register_and_login(client)
    _connect(user_id)
    _add_repository(user_id, repo_id=1, name="alpha", topics=[])
    gone = _add_repository(user_id, repo_id=2, name="beta", topics=[])
    _mark_deleted(gone)

    body = client.get(_REPOSITORIES, headers=_headers(token)).json()

    assert [repo["full_name"] for repo in body] == ["octocat/alpha"]
    # Still in the database, just not surfaced.
    assert _count(GitHubRepository, user_id) == 2


def test_repositories_is_empty_before_any_import(client: TestClient) -> None:
    token, _ = _register_and_login(client)

    assert client.get(_REPOSITORIES, headers=_headers(token)).json() == []


# --------------------------------------------------------------------
# Disconnecting
# --------------------------------------------------------------------


def test_disconnecting_removes_imported_repositories_and_runs(
    client: TestClient, enqueued: list[str]
) -> None:
    """Prompt 3.1 promised that disconnecting means the stored GitHub
    identity is gone. Leaving twenty repositories and their README
    snapshots behind would quietly break that promise."""
    token, user_id = _register_and_login(client)
    _connect(user_id)
    _add_repository(user_id, repo_id=1, name="alpha", topics=["cli"])
    client.post(_INGESTIONS, headers=_headers(token))

    assert client.delete(_CONNECTION, headers=_headers(token)).status_code == 204

    assert _count(GitHubRepository, user_id) == 0
    assert _count(GitHubIngestionRun, user_id) == 0
    assert client.get(_REPOSITORIES, headers=_headers(token)).json() == []
    assert client.get(_LATEST, headers=_headers(token)).json() is None


# --------------------------------------------------------------------
# Authentication and cross-user isolation
# --------------------------------------------------------------------


def test_every_route_requires_authentication(client: TestClient) -> None:
    assert client.post(_INGESTIONS).status_code == 401
    assert client.get(_LATEST).status_code == 401
    assert client.get(_REPOSITORIES).status_code == 401


def test_one_users_import_is_invisible_to_another(client: TestClient) -> None:
    """Isolation is structural: no path parameter, body field or query
    parameter names an owner, so B cannot address A's import even to be
    refused. Hence asserting invisibility rather than a 403."""
    token_a, user_a = _register_and_login(client)
    _connect(user_a)
    _add_repository(user_a, repo_id=1, name="alpha", topics=[])
    client.post(_INGESTIONS, headers=_headers(token_a))

    token_b, user_b = _register_and_login(client)

    assert client.get(_LATEST, headers=_headers(token_b)).json() is None
    assert client.get(_REPOSITORIES, headers=_headers(token_b)).json() == []
    # B starting their own import does not touch A's data...
    _connect(user_b)
    assert client.post(_INGESTIONS, headers=_headers(token_b)).status_code == 202
    assert _count(GitHubRepository, user_a) == 1
    assert _count(GitHubRepository, user_b) == 0
    # ...and A still sees their own.
    assert len(client.get(_REPOSITORIES, headers=_headers(token_a)).json()) == 1


def test_one_users_active_run_does_not_block_another(client: TestClient) -> None:
    """The one-active-run rule is per user, not global."""
    token_a, user_a = _register_and_login(client)
    _connect(user_a)
    assert client.post(_INGESTIONS, headers=_headers(token_a)).status_code == 202

    token_b, user_b = _register_and_login(client)
    _connect(user_b)
    assert client.post(_INGESTIONS, headers=_headers(token_b)).status_code == 202


def test_disconnecting_one_user_leaves_another_users_data_intact(client: TestClient) -> None:
    token_a, user_a = _register_and_login(client)
    _connect(user_a)
    _add_repository(user_a, repo_id=1, name="alpha", topics=[])

    token_b, user_b = _register_and_login(client)
    _connect(user_b)
    _add_repository(user_b, repo_id=2, name="beta", topics=[])

    assert client.delete(_CONNECTION, headers=_headers(token_b)).status_code == 204

    assert _count(GitHubRepository, user_a) == 1
    assert _count(GitHubRepository, user_b) == 0
    assert len(client.get(_REPOSITORIES, headers=_headers(token_a)).json()) == 1
