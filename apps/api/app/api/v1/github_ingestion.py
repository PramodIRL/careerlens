"""GitHub ingestion endpoints (Prompt 3.2): start an import, check its
progress, and list what was imported.

    POST /api/v1/github-connection/ingestions         -> 202, the new run
    GET  /api/v1/github-connection/ingestions/latest  -> the run, or null
    GET  /api/v1/github-connection/repositories       -> imported repos

Ownership is STRUCTURAL, exactly as in Prompt 3.1: no path parameter,
body field or query parameter names an owner. Runs and repositories are
keyed on `user_id`, and `user_id` comes only from the JWT — so one user
cannot address another's import even to be refused. `/ingestions/latest`
deliberately has no id in it for the same reason.

PUBLIC DATA ONLY. Nothing here accepts, stores or returns a credential,
and nothing reads a private repository — the ingestion these routes
start uses the same unauthenticated client Prompt 3.1 introduced.

This module writes NO candidate skills and NO evidence. Turning
repository signals into skills is Prompt 3.3.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.db import get_db
from app.models.github_connection import GitHubConnection
from app.models.github_ingestion_run import GitHubIngestionRun
from app.models.github_repository import (
    GitHubRepository,
    GitHubRepositoryLanguage,
    GitHubRepositoryTopic,
)
from app.models.user import User
from app.schemas.github_ingestion import (
    GitHubIngestionRunResponse,
    GitHubRepositoryLanguageResponse,
    GitHubRepositoryResponse,
    IngestionStatus,
)
from app.worker import enqueue_github_ingestion

router = APIRouter()

_NO_CONNECTION = "connect a public GitHub account before importing repositories"
_ALREADY_RUNNING = "an import is already in progress"

_ACTIVE_STATUSES = (IngestionStatus.QUEUED.value, IngestionStatus.PROCESSING.value)


async def _latest_run(db: AsyncSession, user_id: uuid.UUID) -> GitHubIngestionRun | None:
    rows = await db.scalars(
        select(GitHubIngestionRun)
        .where(GitHubIngestionRun.user_id == user_id)
        .order_by(GitHubIngestionRun.created_at.desc())
        .limit(1)
    )
    return rows.first()


@router.post("/ingestions", status_code=status.HTTP_202_ACCEPTED)
async def start_ingestion(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubIngestionRunResponse:
    """Queue an import of the caller's public repositories.

    202, not 201: the work has been accepted, not completed. The client
    polls `/ingestions/latest` for progress, the same way the dashboard
    polls a resume while its text is being extracted.

    409 when an import is already running. That is enforced by a PARTIAL
    UNIQUE INDEX on (user_id) WHERE status IN ('queued','processing'),
    not by the check below alone — the check gives a clean message, and
    the constraint is what actually holds when two requests race (a
    double-clicked button, two tabs, a retried POST). Catching the
    IntegrityError is the race-losing path, and it returns the same 409.
    """
    connection = await db.get(GitHubConnection, current_user.id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NO_CONNECTION)

    active = await db.scalar(
        select(GitHubIngestionRun).where(
            GitHubIngestionRun.user_id == current_user.id,
            GitHubIngestionRun.status.in_(_ACTIVE_STATUSES),
        )
    )
    if active is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_ALREADY_RUNNING)

    run = GitHubIngestionRun(user_id=current_user.id, status=IngestionStatus.QUEUED.value)
    db.add(run)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Lost the race against a concurrent request. The other run is
        # already queued, so this is the same answer as the check above.
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_ALREADY_RUNNING) from exc
    await db.refresh(run)

    # Best-effort, exactly like resume upload's enqueue: the run row is
    # committed either way, so a Redis outage leaves it "queued" with
    # nothing processing it rather than failing this request.
    enqueue_github_ingestion(run.id)

    return GitHubIngestionRunResponse.model_validate(run)


@router.get("/ingestions/latest", response_model=GitHubIngestionRunResponse | None)
async def read_latest_ingestion(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubIngestionRun | None:
    """The caller's most recent run, or null if they've never imported.

    Null rather than 404 for the same reason Prompt 3.1's GET returns
    null: "never imported" is a normal state, and the UI should not need
    an error branch for its most common case.
    """
    return await _latest_run(db, current_user.id)


@router.get("/repositories", response_model=list[GitHubRepositoryResponse])
async def list_repositories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[GitHubRepositoryResponse]:
    """Every repository imported for the caller, most recently pushed
    first. Soft-deleted repositories are excluded — the row is kept so
    Prompt 3.3's evidence stays explicable, but a repository that is no
    longer on GitHub is not something to show as current.

    `readme_text` is deliberately not in the response (see
    app/schemas/github_ingestion.py): it exists for server-side excerpt
    extraction, not for shipping to a list view.
    """
    rows = (
        await db.scalars(
            select(GitHubRepository)
            .where(
                GitHubRepository.user_id == current_user.id,
                GitHubRepository.deleted_at.is_(None),
            )
            .order_by(GitHubRepository.pushed_at.desc().nullslast())
        )
    ).all()
    if not rows:
        return []

    repository_ids = [row.id for row in rows]
    language_rows = (
        await db.execute(
            select(
                GitHubRepositoryLanguage.repository_id,
                GitHubRepositoryLanguage.language,
                GitHubRepositoryLanguage.byte_count,
            )
            .where(GitHubRepositoryLanguage.repository_id.in_(repository_ids))
            .order_by(GitHubRepositoryLanguage.byte_count.desc())
        )
    ).all()
    topic_rows = (
        await db.execute(
            select(GitHubRepositoryTopic.repository_id, GitHubRepositoryTopic.topic)
            .where(GitHubRepositoryTopic.repository_id.in_(repository_ids))
            .order_by(GitHubRepositoryTopic.topic)
        )
    ).all()

    languages: dict[uuid.UUID, list[GitHubRepositoryLanguageResponse]] = {}
    for repository_id, language, byte_count in language_rows:
        languages.setdefault(repository_id, []).append(
            GitHubRepositoryLanguageResponse(language=language, byte_count=byte_count)
        )
    topics: dict[uuid.UUID, list[str]] = {}
    for repository_id, topic in topic_rows:
        topics.setdefault(repository_id, []).append(topic)

    return [
        GitHubRepositoryResponse(
            id=row.id,
            github_repo_id=row.github_repo_id,
            name=row.name,
            full_name=row.full_name,
            description=row.description,
            is_fork=row.is_fork,
            is_archived=row.is_archived,
            primary_language=row.primary_language,
            stargazers_count=row.stargazers_count,
            forks_count=row.forks_count,
            pushed_at=row.pushed_at,
            languages=languages.get(row.id, []),
            topics=topics.get(row.id, []),
            has_readme=row.readme_text is not None,
            detail_fetched=row.detail_fetched_at is not None,
            updated_at=row.updated_at,
        )
        for row in rows
    ]
