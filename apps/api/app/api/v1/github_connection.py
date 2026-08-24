"""Public GitHub connection endpoints (Prompt 3.1): connect, read and
disconnect a candidate's public GitHub account.

    GET    /api/v1/github-connection  -> the connection, or null
    PUT    /api/v1/github-connection  -> verify and store (201/200)
    DELETE /api/v1/github-connection  -> 204, idempotent

NO CREDENTIAL IS INVOLVED ANYWHERE IN THIS FLOW. A username is submitted,
GitHub's public API is asked whether that account exists, and four public
facts are stored. This product never asks for a GitHub password, never
requests an OAuth scope or personal access token, and never reads a
private repository. `GitHubConnectRequest` forbids extra fields, so a
client that tries to send a password gets a 422 rather than having it
quietly ignored.

Ownership is STRUCTURAL, not checked. Unlike `/profiles/{user_id}`
(Prompt 1.3), which takes an id in the path and must compare it against
the caller, these routes take no owner from the request at all — the row
is keyed on `user_id` and `user_id` comes only from the JWT. There is no
path parameter, body field or query parameter naming an owner, so one
user cannot address another's connection even to be refused. That
follows the newer convention set by Prompt 2.1's resumes and 2.4's
candidate skills; it is strictly safer than defending an id-addressable
route correctly, because there is nothing to defend.

"Not connected" is a normal state, not an error: GET returns 200 with a
null body rather than a 404, so the UI's most common case has no error
branch. The only 404 this router produces means "GitHub has no such
public account" — a statement about GitHub, not about our resource.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
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
from app.github.skill_evidence import purge_github_skill_evidence
from app.models.github_connection import GitHubConnection
from app.models.github_ingestion_run import GitHubIngestionRun
from app.models.github_repository import GitHubRepository
from app.models.user import User
from app.schemas.github import GitHubConnectionResponse, GitHubConnectRequest

router = APIRouter()

# Every message below is written for the person who typed the username.
# None of them contains an upstream status code, response body or
# exception text — the same rule app/worker.py applies to
# `resumes.error_message`: a curated, safe reason only.
_NOT_FOUND = "no public GitHub account found for '{username}'"
_IS_ORGANIZATION = (
    "'{username}' is a GitHub organization, not a personal account — "
    "connect your own public account instead"
)
_TIMED_OUT = "GitHub did not respond in time — try again in a moment"
_UNAVAILABLE = "GitHub is unavailable right now — try again in a moment"
_RATE_LIMITED = "GitHub's public API rate limit was reached. Try again in a few minutes."
_RATE_LIMITED_UNTIL = "GitHub's public API rate limit was reached. Try again after {when} UTC."

_USER_ACCOUNT_TYPE = "User"


def _rate_limit_error(exc: GitHubRateLimited) -> HTTPException:
    """503, not 429.

    GitHub is rate-limiting *us* (60 requests/hour per IP for
    unauthenticated calls), so this is a dependency being temporarily
    unavailable — a 429 would tell the user they did something too
    often, which is both wrong and unactionable for them. `Retry-After`
    is set when GitHub told us when the window resets.
    """
    headers: dict[str, str] = {}
    detail = _RATE_LIMITED
    if exc.reset_at is not None:
        detail = _RATE_LIMITED_UNTIL.format(when=exc.reset_at.strftime("%H:%M"))
        seconds = int((exc.reset_at - datetime.now(UTC)).total_seconds())
        if seconds > 0:
            headers["Retry-After"] = str(seconds)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail, headers=headers or None
    )


async def _verify_public_account(client: GitHubClient, username: str) -> GitHubUser:
    """Ask GitHub whether this public account exists, translating every
    failure into the one status and message the user should see.

    The route never sees an HTTP status code from GitHub — that is the
    whole point of app/github/base.py's error set.
    """
    try:
        github_user = await client.get_user(username)
    except GitHubUserNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_NOT_FOUND.format(username=username),
        ) from exc
    except GitHubTimeout as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=_TIMED_OUT) from exc
    except GitHubRateLimited as exc:
        raise _rate_limit_error(exc) from exc
    except GitHubUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_UNAVAILABLE
        ) from exc

    if github_user.type != _USER_ACCOUNT_TYPE:
        # An organization is public and has repositories, but it is not
        # the candidate's own work — connecting `microsoft` and calling
        # the result personal evidence is exactly the kind of unearned
        # claim docs/project-brief.md's Evidence-First rule rules out.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_IS_ORGANIZATION.format(username=github_user.login),
        )

    return github_user


async def _get_connection(db: AsyncSession, user_id: uuid.UUID) -> GitHubConnection | None:
    return await db.get(GitHubConnection, user_id)


@router.get("", response_model=GitHubConnectionResponse | None)
async def read_github_connection(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubConnection | None:
    """The caller's own connection, or null when there isn't one."""
    return await _get_connection(db, current_user.id)


@router.put("", response_model=GitHubConnectionResponse)
async def connect_github(
    body: GitHubConnectRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    client: GitHubClient = Depends(get_github_client),
) -> GitHubConnection:
    """Verify a public GitHub username and store the connection.

    201 when the user had no connection, 200 when this replaced one —
    the same distinction app/api/v1/candidate_skill.py's POST makes.

    A replacement UPDATES the single row keyed on `user_id` rather than
    inserting a second one, which is what makes "one connection per
    user" a property of the schema rather than of this handler
    remembering to delete first. Re-submitting the same username is a
    re-verification: it refreshes `last_verified_at` and the repository
    count.

    The username is already validated and normalized by the time it
    arrives (app/schemas/github.py), so an obvious typo never becomes an
    outbound request against a 60-per-hour budget.
    """
    github_user = await _verify_public_account(client, body.username)

    connection = await _get_connection(db, current_user.id)
    created = connection is None
    if connection is None:
        connection = GitHubConnection(user_id=current_user.id)
        db.add(connection)

    # Stored from GitHub's answer, never from the request: `login` is
    # the account's canonical casing, and `id` is the immutable key
    # Prompt 3.2 will ingest against.
    connection.github_user_id = github_user.id
    connection.username = github_user.login
    connection.public_repo_count = github_user.public_repos
    connection.last_verified_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(connection)

    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return connection


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_github(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove the caller's connection. Idempotent — 204 whether or not
    there was one, the same reasoning as `POST /auth/logout` with no
    session: the caller's desired end state is already true.

    A hard delete, not a tombstone (see app/models/github_connection.py):
    nothing re-creates a connection automatically, so there is nothing
    for a tombstone to suppress, and "disconnect" should mean the stored
    GitHub identity is gone.

    Prompt 3.2: that promise now has to cover the imported data too.
    Leaving twenty repositories and their README snapshots behind after
    a "disconnect" would quietly break the thing this endpoint claims to
    do, so ingested repositories and ingestion runs go with the
    connection. Languages and topics follow by ON DELETE CASCADE from
    the repository rows.

    Prompt 3.3 answers the question 3.2 deferred: GitHub-DERIVED skill
    evidence goes too. That diverges from the dangling reference
    docs/decisions.md accepts for a deleted resume, and the asymmetry is
    the justification — deleted-resume evidence is still RECONCILABLE,
    because the resume extractor may run again, whereas after a
    disconnect there is no connection and GitHub reconciliation will
    never run again. Leaving it would strand the user with skills citing
    repositories that no longer exist and no mechanism to remove them:
    unrecoverable, not merely dangling.

    WHAT SURVIVES A DISCONNECT: every resume-derived and manually added
    piece of evidence, and every confirmed or rejected candidate skill.
    Only UNREVIEWED suggestions left with no evidence at all are removed.
    A confirmed skill may therefore survive with zero evidence — the
    existing, intended semantics (the user asserted it), identical to
    today's behaviour after a resume is deleted.
    """
    connection = await _get_connection(db, current_user.id)
    if connection is None:
        return

    # Before the repositories go, so the evidence citing them is removed
    # deliberately rather than left pointing at rows that no longer exist.
    await purge_github_skill_evidence(db, current_user.id)
    await db.execute(delete(GitHubRepository).where(GitHubRepository.user_id == current_user.id))
    await db.execute(
        delete(GitHubIngestionRun).where(GitHubIngestionRun.user_id == current_user.id)
    )
    await db.delete(connection)
    await db.commit()
