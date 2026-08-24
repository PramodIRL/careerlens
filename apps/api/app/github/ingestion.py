"""Idempotent ingestion of public GitHub repository data (Prompt 3.2).

Splits from app/github/http.py the same way app/skill_extraction.py
splits from app/skill_matching.py: that module decides what GitHub SAID,
this one decides what to WRITE. Everything here is about idempotency,
partial failure, and not trampling data we already hold.

FOUR INVARIANTS THIS MODULE EXISTS TO UPHOLD:

1. IDENTITY IS `github_repo_id`. Repositories are upserted on
   (user_id, github_repo_id), never on "owner/repo" — a rename would
   otherwise create a second row on the next run and silently double a
   candidate's evidence in Prompt 3.3.

2. RATE LIMITING PAUSES THE RUN; IT IS NEVER A REPOSITORY FAILURE.
   GitHubRateLimited is caught at the loop level, aborts further work
   for this attempt, and asks the caller to retry from GitHub's own
   reset time. Every repository already committed stays committed, and
   `repositories_failed` is untouched. Only timeouts, upstream errors,
   and repositories that vanished between listing and detail count as
   per-repository failures.

3. DELETIONS ARE RECONCILED ONLY FROM A COMPLETE LISTING. If pagination
   stopped early, the repositories we did not see are UNKNOWN, not
   absent. Marking them deleted would let one timeout erase a user's
   entire history — and it would look like correct cleanup code.

4. NO RAW GITHUB JSON IS EVER STORED. The README (truncated text + SHA +
   original byte size + a truncation flag) is the only retained source
   snapshot, because Prompt 3.3 must quote it verbatim as evidence and a
   hash cannot be quoted.

This module writes NO candidate skills and NO evidence. Prompt 3.2
gathers signals; inferring skills from them is Prompt 3.3.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.github.base import (
    GitHubClient,
    GitHubRateLimited,
    GitHubReadme,
    GitHubRepositoryNotFound,
    GitHubTimeout,
    GitHubUnavailable,
    GitHubUserNotFound,
    RepositoryListing,
)
from app.github.base import GitHubRepository as UpstreamRepository
from app.models.github_connection import GitHubConnection
from app.models.github_ingestion_run import GitHubIngestionRun
from app.models.github_repository import (
    GitHubRepository,
    GitHubRepositoryLanguage,
    GitHubRepositoryTopic,
)
from app.schemas.github_ingestion import IngestionStatus
from app.settings import get_settings

logger = logging.getLogger(__name__)

# Curated, safe messages only — never an upstream status code, response
# body or exception text. Same rule app/worker.py applies to
# `resumes.error_message`.
NO_CONNECTION = "no GitHub account is connected"
ACCOUNT_UNAVAILABLE = (
    "that GitHub account is no longer available — reconnect a public account to try again"
)
GITHUB_UNAVAILABLE = "GitHub could not be reached — try importing again in a moment"

# How long to wait when GitHub gave us no usable reset time. Short
# enough to recover quickly, long enough not to spend another request
# immediately into the same closed window.
_RATE_LIMIT_FALLBACK_SECONDS = 300


@dataclass(frozen=True)
class IngestionOutcome:
    """What the Celery task should do when this returns.

    `retry_after_seconds` is not None only for a pause that should be
    resumed — today, exclusively rate limiting. A terminal run (succeeded
    or failed) returns None, and so does a transient failure that has
    already exhausted its attempts: in both cases the run row already
    records the outcome and there is nothing left for the caller to do.
    """

    retry_after_seconds: int | None = None

    @property
    def should_retry(self) -> bool:
        return self.retry_after_seconds is not None


def _truncate_readme(readme: GitHubReadme) -> tuple[str, bool]:
    """Return (stored_text, was_truncated).

    A README is third-party content with no natural size bound, so it is
    clipped to a configured character limit. `was_truncated` is stored
    alongside so a record is never silently lossy — and
    `readme_byte_size` keeps the ORIGINAL size, so the row still says
    how much there really was.
    """
    limit = get_settings().github_readme_max_chars
    if len(readme.text) <= limit:
        return readme.text, False
    return readme.text[:limit], True


async def _claim_run(db: AsyncSession, run_id: uuid.UUID, attempt: int) -> bool:
    """Atomically transition queued -> processing.

    Identical in shape and reasoning to app/worker.py's `_claim_resume`:
    only ever called on a task's first try, and a False return means
    some other execution already claimed this run — a no-op, not an
    error.
    """
    result = cast(
        "CursorResult[Any]",
        await db.execute(
            update(GitHubIngestionRun)
            .where(
                GitHubIngestionRun.id == run_id,
                GitHubIngestionRun.status == IngestionStatus.QUEUED.value,
            )
            .values(
                status=IngestionStatus.PROCESSING.value,
                attempt_count=attempt,
                started_at=datetime.now(UTC),
            )
        ),
    )
    await db.commit()
    return result.rowcount > 0


async def _fail_run(db: AsyncSession, run: GitHubIngestionRun, message: str, attempt: int) -> None:
    run.status = IngestionStatus.FAILED.value
    run.error_message = message
    run.attempt_count = attempt
    run.finished_at = datetime.now(UTC)
    await db.commit()


async def _upsert_repository(
    db: AsyncSession, user_id: uuid.UUID, upstream: UpstreamRepository, seen_at: datetime
) -> uuid.UUID:
    """Insert or update one repository's BASE row from the listing
    payload, returning its local id.

    Base rows are written for every listed repository, forks included:
    all of this data is already in the listing response, so it costs no
    extra request against the rate-limit budget, and it keeps the
    deletion diff working over the user's complete set rather than only
    the capped subset.

    `deleted_at` is cleared here — a repository present in a listing is,
    by definition, not deleted, so this is also how a repository that
    was made private and then public again comes back.
    """
    values = {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "github_repo_id": upstream.id,
        "name": upstream.name,
        "full_name": upstream.full_name,
        "description": upstream.description,
        "is_fork": upstream.fork,
        "is_archived": upstream.archived,
        "primary_language": upstream.language,
        "stargazers_count": upstream.stargazers_count,
        "forks_count": upstream.forks_count,
        "pushed_at": upstream.pushed_at,
        "github_created_at": upstream.created_at,
        "github_updated_at": upstream.updated_at,
        "last_seen_at": seen_at,
        "deleted_at": None,
    }
    insert_statement = pg_insert(GitHubRepository).values(**values)
    statement = insert_statement.on_conflict_do_update(
        constraint="uq_github_repositories_user_repo",
        set_={
            # Everything except the readme_* columns and
            # detail_fetched_at, which only the detail step owns — a
            # base upsert must never wipe a README fetched earlier.
            "name": insert_statement.excluded.name,
            "full_name": insert_statement.excluded.full_name,
            "description": insert_statement.excluded.description,
            "is_fork": insert_statement.excluded.is_fork,
            "is_archived": insert_statement.excluded.is_archived,
            "primary_language": insert_statement.excluded.primary_language,
            "stargazers_count": insert_statement.excluded.stargazers_count,
            "forks_count": insert_statement.excluded.forks_count,
            "pushed_at": insert_statement.excluded.pushed_at,
            "github_created_at": insert_statement.excluded.github_created_at,
            "github_updated_at": insert_statement.excluded.github_updated_at,
            "last_seen_at": insert_statement.excluded.last_seen_at,
            "deleted_at": None,
            "updated_at": datetime.now(UTC),
        },
    ).returning(GitHubRepository.id)
    repository_id = await db.scalar(statement)
    assert repository_id is not None, "upsert returned no repository id"
    return repository_id


async def _sync_topics(db: AsyncSession, repository_id: uuid.UUID, topics: list[str]) -> None:
    """Reconcile topics by difference — delete only what GitHub no
    longer reports, insert only what is missing — so unchanged rows keep
    their original created_at. Same approach as scripts/seed_skills.py.
    """
    desired = {topic.strip() for topic in topics if topic.strip()}
    existing = set(
        (
            await db.scalars(
                select(GitHubRepositoryTopic.topic).where(
                    GitHubRepositoryTopic.repository_id == repository_id
                )
            )
        ).all()
    )
    removed = existing - desired
    if removed:
        await db.execute(
            delete(GitHubRepositoryTopic).where(
                GitHubRepositoryTopic.repository_id == repository_id,
                GitHubRepositoryTopic.topic.in_(removed),
            )
        )
    added = desired - existing
    if added:
        await db.execute(
            pg_insert(GitHubRepositoryTopic)
            .values([{"repository_id": repository_id, "topic": topic} for topic in sorted(added)])
            .on_conflict_do_nothing()
        )


async def _sync_languages(
    db: AsyncSession, repository_id: uuid.UUID, languages: dict[str, int]
) -> None:
    """Reconcile languages by difference, updating a byte count only
    when it actually changed."""
    existing_rows = (
        await db.execute(
            select(GitHubRepositoryLanguage.language, GitHubRepositoryLanguage.byte_count).where(
                GitHubRepositoryLanguage.repository_id == repository_id
            )
        )
    ).all()
    existing = {language: byte_count for language, byte_count in existing_rows}

    removed = set(existing) - set(languages)
    if removed:
        await db.execute(
            delete(GitHubRepositoryLanguage).where(
                GitHubRepositoryLanguage.repository_id == repository_id,
                GitHubRepositoryLanguage.language.in_(removed),
            )
        )
    for language, byte_count in sorted(languages.items()):
        if existing.get(language) == byte_count:
            continue
        await db.execute(
            pg_insert(GitHubRepositoryLanguage)
            .values(repository_id=repository_id, language=language, byte_count=byte_count)
            .on_conflict_do_update(
                index_elements=["repository_id", "language"], set_={"byte_count": byte_count}
            )
        )


async def _store_readme(
    db: AsyncSession, repository_id: uuid.UUID, readme: GitHubReadme | None
) -> None:
    """Persist the README snapshot, or clear it when the repository no
    longer has one.

    Skips the write entirely when GitHub's blob SHA is unchanged, so a
    rerun over untouched content is a true no-op. Note the SHA saves the
    WRITE, not the request: reading it requires fetching the README, and
    conditional requests (ETag) are deliberately out of scope for 3.2.
    """
    current = await db.get(GitHubRepository, repository_id)
    if current is None:  # pragma: no cover - defensive
        return

    if readme is None:
        if current.readme_sha is None and current.readme_text is None:
            return
        current.readme_text = None
        current.readme_sha = None
        current.readme_byte_size = None
        current.readme_truncated = False
        return

    if current.readme_sha == readme.sha:
        return

    text, truncated = _truncate_readme(readme)
    current.readme_text = text
    current.readme_sha = readme.sha
    current.readme_byte_size = readme.size_bytes
    current.readme_truncated = truncated


async def _reconcile_deletions(
    db: AsyncSession, user_id: uuid.UUID, seen_repo_ids: set[int], seen_at: datetime
) -> int:
    """Soft-delete repositories that a COMPLETE listing no longer
    contains, and return how many were marked.

    The caller must only invoke this when `RepositoryListing.complete`
    is True — see invariant 3 in the module docstring. Soft delete, never
    removal: Prompt 3.3 will cite these repositories, and deleting the
    row would leave a skill with unexplained missing support.
    """
    statement = (
        update(GitHubRepository)
        .where(
            GitHubRepository.user_id == user_id,
            GitHubRepository.deleted_at.is_(None),
        )
        .values(deleted_at=seen_at)
    )
    if seen_repo_ids:
        statement = statement.where(GitHubRepository.github_repo_id.notin_(seen_repo_ids))
    result = cast("CursorResult[Any]", await db.execute(statement))
    return result.rowcount or 0


def _select_for_detail(listing: RepositoryListing) -> list[UpstreamRepository]:
    """The repositories whose languages and README will be fetched.

    Forks are excluded before the cap applies, so the cap is spent on
    the candidate's own work rather than on copies of other people's.
    The listing already arrives most-recently-pushed first; sorting here
    again makes the selection independent of that server-side ordering
    rather than quietly dependent on it.
    """
    settings = get_settings()
    own = [repo for repo in listing.repositories if not repo.fork and not repo.private]
    own.sort(key=lambda repo: (repo.pushed_at is not None, repo.pushed_at), reverse=True)
    return own[: settings.github_max_repositories]


async def _ingest_repository_detail(
    db: AsyncSession,
    client: GitHubClient,
    repository_id: uuid.UUID,
    upstream: UpstreamRepository,
) -> None:
    """Fetch and persist one repository's languages and README.

    Raises GitHubRateLimited straight through to the caller — that is a
    run-level pause, not a per-repository failure, and this function
    must not absorb it.
    """
    languages = await client.get_languages(upstream.full_name)
    readme = await client.get_readme(upstream.full_name)

    await _sync_languages(db, repository_id, languages)
    await _store_readme(db, repository_id, readme)


async def run_ingestion(
    db: AsyncSession,
    client: GitHubClient,
    run_id: uuid.UUID,
    attempt: int,
    max_attempts: int,
) -> IngestionOutcome:
    """Execute one attempt of an ingestion run.

    Commits per repository so progress is visible while the run is still
    going, and so a paused run resumes without redoing work. Returns an
    IngestionOutcome telling the caller whether to retry.
    """
    run = await db.get(GitHubIngestionRun, run_id)
    if run is None:
        logger.warning("ingestion skipped: run %s no longer exists", run_id)
        return IngestionOutcome()

    if attempt == 1:
        if not await _claim_run(db, run_id, attempt):
            logger.info("ingestion skipped: run %s already claimed", run_id)
            return IngestionOutcome()
        await db.refresh(run)
    else:
        run.attempt_count = attempt
        await db.commit()

    connection = await db.get(GitHubConnection, run.user_id)
    if connection is None:
        await _fail_run(db, run, NO_CONNECTION, attempt)
        return IngestionOutcome()

    username = connection.username
    started_at = run.started_at or datetime.now(UTC)

    # --- Profile refresh + full listing ---------------------------------
    try:
        profile = await client.get_user(username)
        listing = await client.list_repositories(username)
    except GitHubRateLimited as exc:
        return _pause_for_rate_limit(exc, run_id)
    except GitHubUserNotFound:
        # Permanent: the account was deleted or renamed. Retrying cannot
        # help, so it fails immediately rather than after three waits.
        await _fail_run(db, run, ACCOUNT_UNAVAILABLE, attempt)
        return IngestionOutcome()
    except (GitHubTimeout, GitHubUnavailable):
        logger.warning("ingestion listing failed for run %s (attempt %s)", run_id, attempt)
        if attempt >= max_attempts:
            await _fail_run(db, run, GITHUB_UNAVAILABLE, attempt)
            return IngestionOutcome()
        return IngestionOutcome(retry_after_seconds=0)

    # Refresh the three fields Prompt 3.1 already stores. No new profile
    # data is captured — no bio, no name, no company, no avatar.
    connection.username = profile.login
    connection.public_repo_count = profile.public_repos
    connection.last_verified_at = datetime.now(UTC)

    to_detail = _select_for_detail(listing)
    forks = sum(1 for repo in listing.repositories if repo.fork)

    run.repositories_available = len(listing.repositories)
    run.repositories_forks_excluded = forks
    run.repositories_total = len(to_detail)
    await db.commit()

    # --- Base rows for every listed repository --------------------------
    seen_repo_ids: set[int] = set()
    repository_ids: dict[int, uuid.UUID] = {}
    for upstream in listing.repositories:
        if upstream.private:
            # Unreachable on an unauthenticated listing. Fail closed
            # rather than store something we should never have seen.
            logger.warning("skipping unexpectedly private repository in listing for %s", username)
            continue
        repository_id = await _upsert_repository(db, run.user_id, upstream, started_at)
        repository_ids[upstream.id] = repository_id
        seen_repo_ids.add(upstream.id)
        await _sync_topics(db, repository_id, upstream.topics)
    await db.commit()

    # --- Detail for the capped, fork-free subset ------------------------
    for upstream in to_detail:
        detail_repository_id = repository_ids.get(upstream.id)
        if detail_repository_id is None:  # pragma: no cover - defensive
            continue

        existing = await db.get(GitHubRepository, detail_repository_id)
        if (
            existing is not None
            and existing.detail_fetched_at is not None
            and existing.detail_fetched_at >= started_at
        ):
            # Already handled earlier in this same run (a previous
            # attempt that paused). Skipping is what stops a resumed run
            # from re-spending requests it already spent.
            continue

        try:
            await _ingest_repository_detail(db, client, detail_repository_id, upstream)
        except GitHubRateLimited as exc:
            # Nothing is written for the in-flight repository, and
            # nothing already committed is rolled back. The retry picks
            # up exactly here.
            await db.rollback()
            return _pause_for_rate_limit(exc, run_id)
        except (GitHubTimeout, GitHubUnavailable, GitHubRepositoryNotFound):
            # An ordinary per-repository failure. The base row from the
            # listing is still saved — this never means "nothing stored"
            # — and the run continues. A repository that 404s here is
            # NOT soft-deleted: that is a race with the listing, not the
            # authoritative absence that deletion requires.
            logger.warning("detail fetch failed for repository %s", upstream.full_name)
            run.repositories_failed += 1
        else:
            run.repositories_completed += 1

        if existing is not None:
            # Set on failure too: the attempt happened and was counted,
            # and an ordinary failure is not auto-retried within the
            # same run.
            existing.detail_fetched_at = datetime.now(UTC)
        # One commit per repository — progress is visible while the run
        # is still going, and a crash loses one repository rather than
        # all of them.
        await db.commit()

    # --- Deletions, only from a complete listing ------------------------
    if listing.complete:
        removed = await _reconcile_deletions(db, run.user_id, seen_repo_ids, started_at)
        if removed:
            logger.info("soft-deleted %s repositories no longer on github", removed)
    else:
        logger.warning(
            "listing for run %s was incomplete — skipping deletion reconciliation", run_id
        )

    run.status = IngestionStatus.SUCCEEDED.value
    run.error_message = None
    run.finished_at = datetime.now(UTC)
    await db.commit()
    return IngestionOutcome()


def _pause_for_rate_limit(exc: GitHubRateLimited, run_id: uuid.UUID) -> IngestionOutcome:
    """Turn GitHub's own reset time into a retry delay.

    The run stays `processing` — it is paused, not failed, and every
    repository already committed stays committed. Waiting until the
    window actually resets beats a blind backoff that would spend
    another request into the same closed window.
    """
    if exc.reset_at is not None:
        seconds = int((exc.reset_at - datetime.now(UTC)).total_seconds())
        delay = max(seconds, 1)
    else:
        delay = _RATE_LIMIT_FALLBACK_SECONDS
    logger.info("ingestion run %s paused for %ss by github rate limiting", run_id, delay)
    return IngestionOutcome(retry_after_seconds=delay)
