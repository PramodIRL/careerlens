"""Tests the WIRING between GitHub ingestion and GitHub skill extraction
(app/worker.py, Prompt 3.3).

tests/test_github_skill_evidence.py covers what the extraction writes;
this file covers only *when it is invoked*, which is the part the split
between the two responsibilities actually rests on:

  * a SUCCEEDED run derives skills
  * a run PAUSED by rate limiting does not (it is waiting, not finished)
  * a FAILED run does not
  * a failure while deriving never flips a succeeded import to failed

Runs the real Celery task in eager mode, the same approach and the same
`task_eager_propagates` caveat as tests/test_extraction.py.
"""

import asyncio
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.github.base import (
    GitHubRateLimited,
    GitHubReadme,
    GitHubUnavailable,
    GitHubUser,
    RepositoryListing,
)
from app.github.base import (
    GitHubRepository as UpstreamRepository,
)
from app.models.candidate_skill import CandidateSkill
from app.models.github_connection import GitHubConnection
from app.models.github_ingestion_run import GitHubIngestionRun
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.schemas.github_ingestion import IngestionStatus
from app.settings import get_settings
from app.worker import celery_app, ingest_github_repositories
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}


def _isolated_session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _celery_eager() -> Generator[None, None, None]:
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture(autouse=True)
def _use_isolated_worker_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.worker.get_worker_session_factory", _isolated_session_factory)


class _FakeClient:
    """Answers a one-repository listing whose README, topics and language
    all name skills in the curated taxonomy — so if extraction runs at
    all, it cannot write zero."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error

    async def get_user(self, username: str) -> GitHubUser:
        return GitHubUser(id=1, login="octocat", type="User", public_repos=1)

    async def list_repositories(self, username: str) -> RepositoryListing:
        if self._error is not None:
            raise self._error
        return RepositoryListing(
            repositories=(
                UpstreamRepository(
                    id=42,
                    name="toolkit",
                    full_name="octocat/toolkit",
                    description="A Django toolkit.",
                    language="Python",
                    topics=["docker"],
                    pushed_at=datetime.now(UTC),
                ),
            ),
            complete=True,
        )

    async def get_languages(self, full_name: str) -> dict[str, int]:
        return {"Python": 900}

    async def get_readme(self, full_name: str) -> GitHubReadme | None:
        return GitHubReadme(text="# Toolkit\n\nTested with pytest.\n", sha="abc123", size_bytes=32)


def _run[T](coro: object) -> T:
    async def _inner() -> T:
        engine = create_async_engine(
            get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
        )
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                return await coro(session)  # type: ignore[operator]
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def _seed_connected_user() -> tuple[uuid.UUID, uuid.UUID]:
    async def _seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
        await seed_skill_taxonomy(session)
        user_id = uuid.uuid4()
        session.add(
            User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash")
        )
        await session.flush()
        session.add(
            GitHubConnection(
                user_id=user_id,
                github_user_id=1,
                username="octocat",
                public_repo_count=1,
                last_verified_at=datetime.now(UTC),
            )
        )
        run = GitHubIngestionRun(
            id=uuid.uuid4(), user_id=user_id, status=IngestionStatus.QUEUED.value
        )
        session.add(run)
        await session.commit()
        return user_id, run.id

    return _run(_seed)


def _skill_names(user_id: uuid.UUID) -> set[str]:
    async def _read(session: AsyncSession) -> set[str]:
        rows = (
            await session.scalars(
                select(Skill.name)
                .join(CandidateSkill, CandidateSkill.skill_id == Skill.id)
                .where(CandidateSkill.user_id == user_id)
            )
        ).all()
        return set(rows)

    return _run(_read)


def _run_status(run_id: uuid.UUID) -> str:
    async def _read(session: AsyncSession) -> str:
        run = await session.get(GitHubIngestionRun, run_id)
        assert run is not None
        return run.status

    return _run(_read)


def test_a_succeeded_import_derives_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, run_id = _seed_connected_user()
    monkeypatch.setattr("app.worker.get_github_client", _FakeClient)

    ingest_github_repositories.delay(str(run_id))

    assert _run_status(run_id) == IngestionStatus.SUCCEEDED.value
    # README (pytest), description (Django), topic (docker), language (Python).
    assert {"pytest", "Django", "Docker", "Python"} <= _skill_names(user_id)


def test_a_rate_limited_pause_does_not_derive_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paused run is waiting, not finished. Deriving skills from a
    half-imported account would reconcile against a partial repository
    set for no benefit — the run resumes on its own."""
    user_id, run_id = _seed_connected_user()
    reset_at = datetime.now(UTC) + timedelta(seconds=30)
    monkeypatch.setattr(
        "app.worker.get_github_client", lambda: _FakeClient(error=GitHubRateLimited(reset_at))
    )

    ingest_github_repositories.delay(str(run_id))

    assert _run_status(run_id) == IngestionStatus.PROCESSING.value
    assert _skill_names(user_id) == set()


def test_a_failed_import_derives_no_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, run_id = _seed_connected_user()
    monkeypatch.setattr(
        "app.worker.get_github_client", lambda: _FakeClient(error=GitHubUnavailable("down"))
    )

    ingest_github_repositories.delay(str(run_id))

    assert _run_status(run_id) == IngestionStatus.FAILED.value
    assert _skill_names(user_id) == set()


def test_a_skill_extraction_failure_never_fails_the_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repositories genuinely WERE imported, so a failure deriving
    skills from them must not flip the run to "failed" or trigger a
    Celery retry of GitHub work that is already done — that would
    re-spend a 60-request/hour budget for nothing."""
    user_id, run_id = _seed_connected_user()
    monkeypatch.setattr("app.worker.get_github_client", _FakeClient)

    async def _boom(db: AsyncSession, user: uuid.UUID) -> None:
        raise RuntimeError("taxonomy exploded")

    monkeypatch.setattr("app.worker.extract_github_skill_evidence", _boom)

    ingest_github_repositories.delay(str(run_id))

    assert _run_status(run_id) == IngestionStatus.SUCCEEDED.value
    assert _skill_names(user_id) == set()

    # And the import's own data survived the failed derivation, so the
    # next run re-derives from it rather than from nothing.
    async def _count_repositories(session: AsyncSession) -> int:
        from app.models.github_repository import GitHubRepository

        return (
            await session.scalar(
                select(func.count())
                .select_from(GitHubRepository)
                .where(GitHubRepository.user_id == user_id)
            )
        ) or 0

    assert _run(_count_repositories) == 1


def test_no_evidence_is_written_for_a_user_without_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _seed(session: AsyncSession) -> uuid.UUID:
        await seed_skill_taxonomy(session)
        user_id = uuid.uuid4()
        session.add(
            User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash")
        )
        await session.flush()
        run = GitHubIngestionRun(
            id=uuid.uuid4(), user_id=user_id, status=IngestionStatus.QUEUED.value
        )
        session.add(run)
        await session.commit()
        return run.id

    run_id = _run(_seed)
    monkeypatch.setattr("app.worker.get_github_client", _FakeClient)

    ingest_github_repositories.delay(str(run_id))

    assert _run_status(run_id) == IngestionStatus.FAILED.value

    async def _count_evidence(session: AsyncSession) -> int:
        return (await session.scalar(select(func.count()).select_from(SkillEvidence))) or 0

    assert _run(_count_evidence) == 0
