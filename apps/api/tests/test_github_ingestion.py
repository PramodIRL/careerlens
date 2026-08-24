"""Tests for GitHub ingestion (app/github/ingestion.py, Prompt 3.2).

Every GitHub response is scripted by a fake client. NO NETWORK, and no
dependence on any real account existing. The real client's own
pagination/timeout/rate-limit/parsing behaviour is tested separately,
against httpx.MockTransport, in tests/test_github_client.py.

The load-bearing tests here are the four invariants app/github/ingestion.py
exists to uphold, and they should not be softened:

  * a byte-identical rerun changes nothing, and a RENAME updates the
    existing row instead of creating a second one
  * rate limiting PAUSES the run and never counts as a repository
    failure, preserving everything already committed
  * an INCOMPLETE listing never soft-deletes anything
  * no candidate skills and no evidence are ever written

Test functions are `async def` with an anyio backend: run_ingestion is
async, and unlike the Celery-driven resume tests there is no
`asyncio.run()` bridge in the way.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.github.base import (
    GitHubRateLimited,
    GitHubReadme,
    GitHubRepositoryNotFound,
    GitHubTimeout,
    GitHubUnavailable,
    GitHubUser,
    GitHubUserNotFound,
    RepositoryListing,
)
from app.github.base import (
    GitHubRepository as UpstreamRepository,
)
from app.github.ingestion import run_ingestion
from app.models.candidate_skill import CandidateSkill
from app.models.github_connection import GitHubConnection
from app.models.github_ingestion_run import GitHubIngestionRun
from app.models.github_repository import (
    GitHubRepository,
    GitHubRepositoryLanguage,
    GitHubRepositoryTopic,
)
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.schemas.github_ingestion import IngestionStatus
from app.settings import get_settings
from tests.conftest import TEST_SCHEMA

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


# --------------------------------------------------------------------
# Fake client
# --------------------------------------------------------------------


class FakeGitHubClient:
    """Answers from a script instead of the network.

    Per-call error injection is keyed by repository full_name so a test
    can fail exactly one repository's languages or README while the rest
    of the run proceeds — which is the whole point of the partial-failure
    behaviour being tested.
    """

    def __init__(
        self,
        *,
        user: GitHubUser | None = None,
        listing: RepositoryListing | None = None,
        languages: dict[str, dict[str, int]] | None = None,
        readmes: dict[str, GitHubReadme | None] | None = None,
        user_error: Exception | None = None,
        listing_error: Exception | None = None,
        language_errors: dict[str, Exception] | None = None,
        readme_errors: dict[str, Exception] | None = None,
    ) -> None:
        self._user = user or _github_user()
        self._listing = listing or RepositoryListing(repositories=(), complete=True)
        self._languages = languages or {}
        self._readmes = readmes or {}
        self._user_error = user_error
        self._listing_error = listing_error
        self._language_errors = language_errors or {}
        self._readme_errors = readme_errors or {}
        self.language_calls: list[str] = []
        self.readme_calls: list[str] = []

    async def get_user(self, username: str) -> GitHubUser:
        if self._user_error is not None:
            raise self._user_error
        return self._user

    async def list_repositories(self, username: str) -> RepositoryListing:
        if self._listing_error is not None:
            raise self._listing_error
        return self._listing

    async def get_languages(self, full_name: str) -> dict[str, int]:
        self.language_calls.append(full_name)
        error = self._language_errors.get(full_name)
        if error is not None:
            raise error
        return self._languages.get(full_name, {})

    async def get_readme(self, full_name: str) -> GitHubReadme | None:
        self.readme_calls.append(full_name)
        error = self._readme_errors.get(full_name)
        if error is not None:
            raise error
        return self._readmes.get(full_name)


def _github_user(*, login: str = "octocat", public_repos: int = 3) -> GitHubUser:
    return GitHubUser(id=583231, login=login, type="User", public_repos=public_repos)


def _repo(
    *,
    id: int,
    name: str = "repo",
    owner: str = "octocat",
    fork: bool = False,
    topics: list[str] | None = None,
    pushed_days_ago: int = 1,
    description: str | None = "a repository",
    stars: int = 0,
) -> UpstreamRepository:
    return UpstreamRepository(
        id=id,
        name=name,
        full_name=f"{owner}/{name}",
        private=False,
        fork=fork,
        archived=False,
        description=description,
        language="Python",
        stargazers_count=stars,
        forks_count=0,
        topics=topics or [],
        pushed_at=datetime.now(UTC) - timedelta(days=pushed_days_ago),
        created_at=datetime.now(UTC) - timedelta(days=365),
        updated_at=datetime.now(UTC) - timedelta(days=pushed_days_ago),
    )


def _listing(*repos: UpstreamRepository, complete: bool = True) -> RepositoryListing:
    return RepositoryListing(repositories=tuple(repos), complete=complete)


def _readme(text: str = "# Hello\n\nBuilt with Python.\n", sha: str = "sha-1") -> GitHubReadme:
    return GitHubReadme(text=text, sha=sha, size_bytes=len(text.encode()))


# --------------------------------------------------------------------
# Fixtures for a connected user
# --------------------------------------------------------------------


async def _connected_user(db: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash"))
    await db.commit()
    db.add(
        GitHubConnection(
            user_id=user_id,
            github_user_id=583231,
            username="octocat",
            public_repo_count=3,
            last_verified_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return user_id


async def _queue_run(db: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    run = GitHubIngestionRun(user_id=user_id, status=IngestionStatus.QUEUED.value)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run.id


async def _repositories(db: AsyncSession, user_id: uuid.UUID) -> list[GitHubRepository]:
    rows = await db.scalars(
        select(GitHubRepository)
        .where(GitHubRepository.user_id == user_id)
        .order_by(GitHubRepository.github_repo_id)
    )
    return list(rows.all())


async def _run_row(db: AsyncSession, run_id: uuid.UUID) -> GitHubIngestionRun:
    run = await db.get(GitHubIngestionRun, run_id)
    assert run is not None
    await db.refresh(run)
    return run


# --------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_ingests_repositories_languages_topics_and_readme(db: AsyncSession) -> None:
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)
    client = FakeGitHubClient(
        listing=_listing(_repo(id=1, name="alpha", topics=["cli", "python"])),
        languages={"octocat/alpha": {"Python": 900, "HTML": 100}},
        readmes={"octocat/alpha": _readme()},
    )

    outcome = await run_ingestion(db, client, run_id, attempt=1, max_attempts=3)

    assert outcome.should_retry is False
    run = await _run_row(db, run_id)
    assert run.status == IngestionStatus.SUCCEEDED.value
    assert run.repositories_available == 1
    assert run.repositories_forks_excluded == 0
    assert run.repositories_total == 1
    assert run.repositories_completed == 1
    assert run.repositories_failed == 0
    assert run.finished_at is not None

    repos = await _repositories(db, user_id)
    assert len(repos) == 1
    assert repos[0].full_name == "octocat/alpha"
    assert repos[0].primary_language == "Python"
    assert repos[0].readme_text == "# Hello\n\nBuilt with Python.\n"
    assert repos[0].readme_sha == "sha-1"
    assert repos[0].readme_truncated is False
    assert repos[0].detail_fetched_at is not None
    assert repos[0].deleted_at is None

    languages = (
        await db.scalars(
            select(GitHubRepositoryLanguage.language).where(
                GitHubRepositoryLanguage.repository_id == repos[0].id
            )
        )
    ).all()
    assert set(languages) == {"Python", "HTML"}

    topics = (
        await db.scalars(
            select(GitHubRepositoryTopic.topic).where(
                GitHubRepositoryTopic.repository_id == repos[0].id
            )
        )
    ).all()
    assert set(topics) == {"cli", "python"}


@pytest.mark.anyio
async def test_ingestion_writes_no_candidate_skills_or_evidence(db: AsyncSession) -> None:
    """Prompt 3.2 gathers signals; inferring skills from them is 3.3.
    Asserted rather than assumed, because it would be very easy to add
    'just a little' skill matching here and quietly move the boundary."""
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)
    client = FakeGitHubClient(
        listing=_listing(_repo(id=1, name="alpha", topics=["python"])),
        languages={"octocat/alpha": {"Python": 900}},
        readmes={"octocat/alpha": _readme("# Python and FastAPI and Docker\n")},
    )

    await run_ingestion(db, client, run_id, attempt=1, max_attempts=3)

    assert await db.scalar(select(func.count()).select_from(CandidateSkill)) == 0
    assert await db.scalar(select(func.count()).select_from(SkillEvidence)) == 0


@pytest.mark.anyio
async def test_profile_refresh_updates_only_the_three_stored_fields(db: AsyncSession) -> None:
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)
    client = FakeGitHubClient(user=_github_user(login="OctoCat", public_repos=9))

    await run_ingestion(db, client, run_id, attempt=1, max_attempts=3)

    connection = await db.get(GitHubConnection, user_id)
    assert connection is not None
    await db.refresh(connection)
    assert connection.username == "OctoCat"
    assert connection.public_repo_count == 9


# --------------------------------------------------------------------
# Idempotency: reruns, renames, no duplicates
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_rerun_over_unchanged_data_changes_nothing(db: AsyncSession) -> None:
    """The idempotency guarantee, asserted on ids and timestamps rather
    than on row counts alone — a rerun that deleted and re-inserted the
    same rows would pass a count check while churning every id."""
    user_id = await _connected_user(db)
    client = FakeGitHubClient(
        listing=_listing(_repo(id=1, name="alpha", topics=["cli"])),
        languages={"octocat/alpha": {"Python": 900}},
        readmes={"octocat/alpha": _readme()},
    )

    first_run = await _queue_run(db, user_id)
    await run_ingestion(db, client, first_run, attempt=1, max_attempts=3)
    before = (await _repositories(db, user_id))[0]
    before_id, before_created = before.id, before.created_at
    before_language_created = await db.scalar(
        select(GitHubRepositoryLanguage.created_at).where(
            GitHubRepositoryLanguage.repository_id == before_id
        )
    )

    second_run = await _queue_run(db, user_id)
    await run_ingestion(db, client, second_run, attempt=1, max_attempts=3)

    repos = await _repositories(db, user_id)
    assert len(repos) == 1
    assert repos[0].id == before_id
    assert repos[0].created_at == before_created
    # Reconciled by difference, so an unchanged language row keeps its
    # original created_at rather than being deleted and re-inserted.
    after_language_created = await db.scalar(
        select(GitHubRepositoryLanguage.created_at).where(
            GitHubRepositoryLanguage.repository_id == before_id
        )
    )
    assert after_language_created == before_language_created


@pytest.mark.anyio
async def test_rename_updates_the_existing_row_rather_than_duplicating(db: AsyncSession) -> None:
    """The reason identity is github_repo_id and never full_name. If this
    regresses, a candidate's evidence silently doubles in Prompt 3.3."""
    user_id = await _connected_user(db)

    first_run = await _queue_run(db, user_id)
    await run_ingestion(
        db,
        FakeGitHubClient(listing=_listing(_repo(id=1, name="old-name"))),
        first_run,
        attempt=1,
        max_attempts=3,
    )
    original_id = (await _repositories(db, user_id))[0].id

    second_run = await _queue_run(db, user_id)
    await run_ingestion(
        db,
        FakeGitHubClient(listing=_listing(_repo(id=1, name="new-name"))),
        second_run,
        attempt=1,
        max_attempts=3,
    )

    repos = await _repositories(db, user_id)
    assert len(repos) == 1
    assert repos[0].id == original_id
    assert repos[0].full_name == "octocat/new-name"


@pytest.mark.anyio
async def test_unchanged_readme_sha_leaves_the_snapshot_untouched(db: AsyncSession) -> None:
    user_id = await _connected_user(db)
    client = FakeGitHubClient(
        listing=_listing(_repo(id=1, name="alpha")),
        readmes={"octocat/alpha": _readme("# First\n", sha="sha-1")},
    )
    first_run = await _queue_run(db, user_id)
    await run_ingestion(db, client, first_run, attempt=1, max_attempts=3)

    # Same SHA, different text — the SHA is what decides, so the stored
    # text must not change.
    client._readmes = {"octocat/alpha": _readme("# Different body\n", sha="sha-1")}
    second_run = await _queue_run(db, user_id)
    await run_ingestion(db, client, second_run, attempt=1, max_attempts=3)

    assert (await _repositories(db, user_id))[0].readme_text == "# First\n"


@pytest.mark.anyio
async def test_changed_readme_sha_replaces_the_snapshot(db: AsyncSession) -> None:
    user_id = await _connected_user(db)
    client = FakeGitHubClient(
        listing=_listing(_repo(id=1, name="alpha")),
        readmes={"octocat/alpha": _readme("# First\n", sha="sha-1")},
    )
    await run_ingestion(db, client, await _queue_run(db, user_id), attempt=1, max_attempts=3)

    client._readmes = {"octocat/alpha": _readme("# Second\n", sha="sha-2")}
    await run_ingestion(db, client, await _queue_run(db, user_id), attempt=1, max_attempts=3)

    repo = (await _repositories(db, user_id))[0]
    assert repo.readme_text == "# Second\n"
    assert repo.readme_sha == "sha-2"


@pytest.mark.anyio
async def test_readme_is_truncated_and_flagged(db: AsyncSession, monkeypatch) -> None:
    """Truncation must be visible, not silent: the flag says it happened
    and readme_byte_size still reports the ORIGINAL size."""
    monkeypatch.setattr(get_settings(), "github_readme_max_chars", 20)
    user_id = await _connected_user(db)
    long_text = "x" * 500

    await run_ingestion(
        db,
        FakeGitHubClient(
            listing=_listing(_repo(id=1, name="alpha")),
            readmes={"octocat/alpha": _readme(long_text, sha="sha-long")},
        ),
        await _queue_run(db, user_id),
        attempt=1,
        max_attempts=3,
    )

    repo = (await _repositories(db, user_id))[0]
    assert repo.readme_text == "x" * 20
    assert repo.readme_truncated is True
    assert repo.readme_byte_size == 500


@pytest.mark.anyio
async def test_topics_and_languages_are_reconciled_by_difference(db: AsyncSession) -> None:
    user_id = await _connected_user(db)
    client = FakeGitHubClient(
        listing=_listing(_repo(id=1, name="alpha", topics=["cli", "python"])),
        languages={"octocat/alpha": {"Python": 900, "HTML": 100}},
    )
    await run_ingestion(db, client, await _queue_run(db, user_id), attempt=1, max_attempts=3)

    client._listing = _listing(_repo(id=1, name="alpha", topics=["python", "fastapi"]))
    client._languages = {"octocat/alpha": {"Python": 950}}
    await run_ingestion(db, client, await _queue_run(db, user_id), attempt=1, max_attempts=3)

    repo = (await _repositories(db, user_id))[0]
    topics = (
        await db.scalars(
            select(GitHubRepositoryTopic.topic).where(
                GitHubRepositoryTopic.repository_id == repo.id
            )
        )
    ).all()
    assert set(topics) == {"python", "fastapi"}

    languages = (
        await db.execute(
            select(GitHubRepositoryLanguage.language, GitHubRepositoryLanguage.byte_count).where(
                GitHubRepositoryLanguage.repository_id == repo.id
            )
        )
    ).all()
    assert dict(languages) == {"Python": 950}


# --------------------------------------------------------------------
# Deletion — only from a COMPLETE listing
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_repository_missing_from_a_complete_listing_is_soft_deleted(
    db: AsyncSession,
) -> None:
    user_id = await _connected_user(db)
    await run_ingestion(
        db,
        FakeGitHubClient(listing=_listing(_repo(id=1, name="alpha"), _repo(id=2, name="beta"))),
        await _queue_run(db, user_id),
        attempt=1,
        max_attempts=3,
    )

    await run_ingestion(
        db,
        FakeGitHubClient(listing=_listing(_repo(id=1, name="alpha"))),
        await _queue_run(db, user_id),
        attempt=1,
        max_attempts=3,
    )

    repos = await _repositories(db, user_id)
    # Soft delete: the row survives so Prompt 3.3's evidence stays
    # explicable rather than silently losing its support.
    assert len(repos) == 2
    assert repos[0].deleted_at is None
    assert repos[1].deleted_at is not None


@pytest.mark.anyio
async def test_incomplete_listing_never_soft_deletes_anything(db: AsyncSession) -> None:
    """THE partial-failure trap of this prompt.

    If pagination stopped early, the repositories we did not see are
    UNKNOWN, not absent. Reconciling here would let one timeout erase a
    user's entire history — and it would look like correct cleanup code.
    """
    user_id = await _connected_user(db)
    await run_ingestion(
        db,
        FakeGitHubClient(listing=_listing(_repo(id=1, name="alpha"), _repo(id=2, name="beta"))),
        await _queue_run(db, user_id),
        attempt=1,
        max_attempts=3,
    )

    await run_ingestion(
        db,
        FakeGitHubClient(listing=_listing(_repo(id=1, name="alpha"), complete=False)),
        await _queue_run(db, user_id),
        attempt=1,
        max_attempts=3,
    )

    repos = await _repositories(db, user_id)
    assert [repo.deleted_at for repo in repos] == [None, None]


@pytest.mark.anyio
async def test_a_reappearing_repository_is_undeleted(db: AsyncSession) -> None:
    user_id = await _connected_user(db)
    both = FakeGitHubClient(listing=_listing(_repo(id=1, name="alpha"), _repo(id=2, name="beta")))
    await run_ingestion(db, both, await _queue_run(db, user_id), attempt=1, max_attempts=3)

    await run_ingestion(
        db,
        FakeGitHubClient(listing=_listing(_repo(id=1, name="alpha"))),
        await _queue_run(db, user_id),
        attempt=1,
        max_attempts=3,
    )
    assert (await _repositories(db, user_id))[1].deleted_at is not None

    await run_ingestion(db, both, await _queue_run(db, user_id), attempt=1, max_attempts=3)
    assert (await _repositories(db, user_id))[1].deleted_at is None


@pytest.mark.anyio
async def test_a_detail_404_does_not_soft_delete_the_repository(db: AsyncSession) -> None:
    """A 404 on a detail endpoint is a race with the listing, not the
    authoritative absence that deletion requires. It counts as a
    per-repository failure and nothing more."""
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)

    await run_ingestion(
        db,
        FakeGitHubClient(
            listing=_listing(_repo(id=1, name="alpha")),
            language_errors={"octocat/alpha": GitHubRepositoryNotFound("gone")},
        ),
        run_id,
        attempt=1,
        max_attempts=3,
    )

    repos = await _repositories(db, user_id)
    assert repos[0].deleted_at is None
    run = await _run_row(db, run_id)
    assert run.repositories_failed == 1
    assert run.status == IngestionStatus.SUCCEEDED.value


# --------------------------------------------------------------------
# Forks and the repository cap
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_forks_are_stored_but_never_fetched_in_detail(db: AsyncSession) -> None:
    """A fork gets a base row (that data is already in the listing and
    costs no request) but never spends one of the 60/hour budget."""
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)
    client = FakeGitHubClient(
        listing=_listing(_repo(id=1, name="mine"), _repo(id=2, name="theirs", fork=True))
    )

    await run_ingestion(db, client, run_id, attempt=1, max_attempts=3)

    assert client.language_calls == ["octocat/mine"]
    assert client.readme_calls == ["octocat/mine"]

    run = await _run_row(db, run_id)
    assert run.repositories_available == 2
    assert run.repositories_forks_excluded == 1
    assert run.repositories_total == 1

    repos = await _repositories(db, user_id)
    assert len(repos) == 2
    assert repos[1].is_fork is True
    # Base data only — the flag the UI uses to say so.
    assert repos[1].detail_fetched_at is None


@pytest.mark.anyio
async def test_cap_limits_detail_to_the_most_recently_pushed(db: AsyncSession, monkeypatch) -> None:
    """The cap exists because 60 requests/hour is a hard ceiling. It must
    keep the most recent work, and the three counts must stay distinct so
    the UI can say '2 of 5' rather than implying the account has 2."""
    monkeypatch.setattr(get_settings(), "github_max_repositories", 2)
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)
    client = FakeGitHubClient(
        listing=_listing(
            _repo(id=1, name="oldest", pushed_days_ago=100),
            _repo(id=2, name="newest", pushed_days_ago=1),
            _repo(id=3, name="middle", pushed_days_ago=10),
            _repo(id=4, name="a-fork", fork=True, pushed_days_ago=0),
        )
    )

    await run_ingestion(db, client, run_id, attempt=1, max_attempts=3)

    assert client.language_calls == ["octocat/newest", "octocat/middle"]

    run = await _run_row(db, run_id)
    assert run.repositories_available == 4
    assert run.repositories_forks_excluded == 1
    assert run.repositories_total == 2
    assert run.repositories_completed == 2
    # Every listed repository still has a base row, capped or not.
    assert len(await _repositories(db, user_id)) == 4


# --------------------------------------------------------------------
# Failure precedence
# --------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [GitHubTimeout("slow"), GitHubUnavailable("502"), GitHubRepositoryNotFound("gone")]
)
async def test_one_repositorys_detail_failure_does_not_fail_the_run(
    db: AsyncSession, error: Exception
) -> None:
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)
    client = FakeGitHubClient(
        listing=_listing(_repo(id=1, name="good"), _repo(id=2, name="bad")),
        languages={"octocat/good": {"Python": 10}},
        language_errors={"octocat/bad": error},
    )

    await run_ingestion(db, client, run_id, attempt=1, max_attempts=3)

    run = await _run_row(db, run_id)
    assert run.status == IngestionStatus.SUCCEEDED.value
    assert run.repositories_completed == 1
    assert run.repositories_failed == 1

    # The failed repository still has its base row from the listing —
    # "failed" means incomplete detail, never "nothing stored".
    repos = await _repositories(db, user_id)
    assert len(repos) == 2
    assert repos[1].full_name == "octocat/bad"
    assert repos[1].readme_text is None


@pytest.mark.anyio
async def test_missing_readme_is_not_a_failure(db: AsyncSession) -> None:
    """Most repositories have no README. Counting that as a failure would
    make the normal case look broken and inflate repositories_failed."""
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)

    await run_ingestion(
        db,
        FakeGitHubClient(listing=_listing(_repo(id=1, name="alpha")), readmes={}),
        run_id,
        attempt=1,
        max_attempts=3,
    )

    run = await _run_row(db, run_id)
    assert run.repositories_failed == 0
    assert run.repositories_completed == 1
    assert (await _repositories(db, user_id))[0].readme_text is None


@pytest.mark.anyio
async def test_rate_limit_pauses_the_run_and_preserves_progress(db: AsyncSession) -> None:
    """INVARIANT: rate limiting is a run-level pause, never a repository
    failure. Everything already committed survives, repositories_failed
    stays zero, and the run stays PROCESSING rather than failing."""
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)
    reset = datetime.now(UTC) + timedelta(minutes=7)
    client = FakeGitHubClient(
        # Explicit push dates: detail order is pushed_at desc, so this is
        # what makes "first before second" a property of the data rather
        # than of construction order.
        listing=_listing(
            _repo(id=1, name="first", pushed_days_ago=1),
            _repo(id=2, name="second", pushed_days_ago=5),
        ),
        languages={"octocat/first": {"Python": 10}},
        language_errors={"octocat/second": GitHubRateLimited(reset)},
    )

    outcome = await run_ingestion(db, client, run_id, attempt=1, max_attempts=3)

    assert outcome.should_retry is True
    assert outcome.retry_after_seconds is not None
    assert 0 < outcome.retry_after_seconds <= 7 * 60

    run = await _run_row(db, run_id)
    assert run.status == IngestionStatus.PROCESSING.value
    assert run.repositories_completed == 1
    # The whole point: NOT counted as a repository failure.
    assert run.repositories_failed == 0

    repos = await _repositories(db, user_id)
    assert repos[0].detail_fetched_at is not None
    # Nothing was written for the in-flight repository.
    assert repos[1].detail_fetched_at is None


@pytest.mark.anyio
async def test_a_resumed_run_skips_repositories_already_handled(db: AsyncSession) -> None:
    """The other half of the pause guarantee: resuming must not re-spend
    requests on repositories the first attempt already handled."""
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)
    reset = datetime.now(UTC) + timedelta(seconds=30)
    client = FakeGitHubClient(
        listing=_listing(
            _repo(id=1, name="first", pushed_days_ago=1),
            _repo(id=2, name="second", pushed_days_ago=5),
        ),
        languages={"octocat/first": {"Python": 10}, "octocat/second": {"Go": 20}},
        language_errors={"octocat/second": GitHubRateLimited(reset)},
    )
    await run_ingestion(db, client, run_id, attempt=1, max_attempts=3)
    assert client.language_calls == ["octocat/first", "octocat/second"]

    # The limit has lifted; the retry runs.
    client._language_errors = {}
    client.language_calls.clear()
    outcome = await run_ingestion(db, client, run_id, attempt=2, max_attempts=3)

    assert outcome.should_retry is False
    # "first" was already done and is NOT re-fetched.
    assert client.language_calls == ["octocat/second"]
    run = await _run_row(db, run_id)
    assert run.status == IngestionStatus.SUCCEEDED.value
    assert run.repositories_completed == 2


@pytest.mark.anyio
async def test_rate_limit_without_a_reset_time_still_pauses(db: AsyncSession) -> None:
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)

    outcome = await run_ingestion(
        db,
        FakeGitHubClient(listing_error=GitHubRateLimited(None)),
        run_id,
        attempt=1,
        max_attempts=3,
    )

    assert outcome.should_retry is True
    assert outcome.retry_after_seconds == 300
    assert (await _run_row(db, run_id)).status == IngestionStatus.PROCESSING.value


@pytest.mark.anyio
async def test_account_not_found_fails_permanently_without_retrying(db: AsyncSession) -> None:
    """The account was deleted or renamed. Retrying cannot help, so it
    fails immediately rather than after three waits."""
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)

    outcome = await run_ingestion(
        db,
        FakeGitHubClient(user_error=GitHubUserNotFound("gone")),
        run_id,
        attempt=1,
        max_attempts=3,
    )

    assert outcome.should_retry is False
    run = await _run_row(db, run_id)
    assert run.status == IngestionStatus.FAILED.value
    assert "no longer available" in (run.error_message or "")


@pytest.mark.anyio
async def test_transient_listing_failure_retries_then_fails(db: AsyncSession) -> None:
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)
    client = FakeGitHubClient(listing_error=GitHubTimeout("slow"))

    assert (await run_ingestion(db, client, run_id, attempt=1, max_attempts=2)).should_retry is True
    assert (await _run_row(db, run_id)).status == IngestionStatus.PROCESSING.value

    outcome = await run_ingestion(db, client, run_id, attempt=2, max_attempts=2)

    assert outcome.should_retry is False
    run = await _run_row(db, run_id)
    assert run.status == IngestionStatus.FAILED.value
    assert run.error_message is not None
    # Curated, safe text only — no upstream status code or exception text.
    assert "502" not in run.error_message
    assert "Timeout" not in run.error_message


@pytest.mark.anyio
async def test_run_without_a_connection_fails_cleanly(db: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash"))
    await db.commit()
    run_id = await _queue_run(db, user_id)

    await run_ingestion(db, FakeGitHubClient(), run_id, attempt=1, max_attempts=3)

    run = await _run_row(db, run_id)
    assert run.status == IngestionStatus.FAILED.value
    assert run.error_message == "no GitHub account is connected"


@pytest.mark.anyio
async def test_an_already_claimed_run_is_a_no_op(db: AsyncSession) -> None:
    """Guards against a genuinely duplicate execution, the same way
    app/worker.py's _claim_resume does."""
    user_id = await _connected_user(db)
    run_id = await _queue_run(db, user_id)
    run = await db.get(GitHubIngestionRun, run_id)
    assert run is not None
    run.status = IngestionStatus.PROCESSING.value
    await db.commit()

    client = FakeGitHubClient(listing=_listing(_repo(id=1, name="alpha")))
    outcome = await run_ingestion(db, client, run_id, attempt=1, max_attempts=3)

    assert outcome.should_retry is False
    assert client.language_calls == []
    assert await _repositories(db, user_id) == []
