"""Saved job description endpoints (Prompt 4.1): save a posting, list
them, read one, edit it, remove it.

    POST   /api/v1/saved-jobs        -> 201, the new job
    GET    /api/v1/saved-jobs        -> the caller's jobs, newest first
    GET    /api/v1/saved-jobs/{id}   -> one job
    PATCH  /api/v1/saved-jobs/{id}   -> partial update
    DELETE /api/v1/saved-jobs/{id}   -> 204

OWNERSHIP IS STRUCTURAL. No path parameter, body field or query string
names an owner: `user_id` comes only from the JWT on create, and every
read/update/delete loads the row and compares against `current_user.id`.
`extra="forbid"` on both request models means a client that tries to
send `user_id` gets a 422 rather than having it silently dropped. Same
shape as app/api/v1/resume.py's `_get_owned_resume`, and the same
404-if-missing / 403-if-someone-else's split.

THIS MODULE NEVER FETCHES `source_url`. It is stored metadata, and the
value is arbitrary user input — requesting it server-side would make
this endpoint a server-side request forgery vector against whatever the
API host can reach. Validation restricts the scheme to http/https for
the sake of the user's own browser; it does not license a fetch here.
See app/models/saved_job.py.

NO EXTRACTION, NO SCORING. A saved job is inert in Prompt 4.1: nothing
here parses the description into skills (Prompt 4.2) or ranks it against
a candidate (Prompt 4.3). Nothing here writes candidate skills or skill
evidence, and it must stay that way — it would be very easy to add
"just a little" matching and quietly move the boundary.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.db import get_db
from app.models.saved_job import SavedJob
from app.models.user import User
from app.schemas.saved_job import (
    SavedJobCreateRequest,
    SavedJobResponse,
    SavedJobUpdateRequest,
)

router = APIRouter()

_NOT_FOUND = "saved job not found"
_NOT_YOURS = "not authorized to access this saved job"


async def _get_owned_job(db: AsyncSession, saved_job_id: uuid.UUID, current_user: User) -> SavedJob:
    """Load a job and prove the caller owns it.

    404 when the id does not exist, 403 when it exists but belongs to
    someone else. A specific 403 is not a credential-guessing surface
    here: saved-job ids are random UUIDs, not enumerable — the same
    reasoning as the resume and candidate-skill ownership checks.
    """
    saved_job = await db.get(SavedJob, saved_job_id)
    if saved_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if saved_job.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_NOT_YOURS)
    return saved_job


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SavedJobResponse)
async def create_saved_job(
    body: SavedJobCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SavedJob:
    """Save a posting. The owner is taken from the access token, never
    from the request."""
    saved_job = SavedJob(
        user_id=current_user.id,
        company=body.company,
        title=body.title,
        description=body.description,
        location=body.location,
        employment_type=body.employment_type.value if body.employment_type else None,
        source_url=body.source_url,
    )
    db.add(saved_job)
    await db.commit()
    await db.refresh(saved_job)
    return saved_job


@router.get("", response_model=list[SavedJobResponse])
async def list_saved_jobs(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SavedJob]:
    """The caller's saved jobs, most recently saved first.

    An empty list rather than a 404 when nothing is saved: "I have not
    saved any jobs yet" is a normal state, and the UI should not need an
    error branch for its most common case — the same choice as the
    resume list and the GitHub connection endpoint.

    Deliberately unpaginated, matching every other list endpoint in this
    application. A personal collection of saved jobs is small; adding
    pagination to this one alone would make it the odd one out.
    """
    rows = await db.scalars(
        select(SavedJob)
        .where(SavedJob.user_id == current_user.id)
        .order_by(SavedJob.created_at.desc())
    )
    return list(rows.all())


@router.get("/{saved_job_id}", response_model=SavedJobResponse)
async def read_saved_job(
    saved_job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SavedJob:
    return await _get_owned_job(db, saved_job_id, current_user)


@router.patch("/{saved_job_id}", response_model=SavedJobResponse)
async def update_saved_job(
    saved_job_id: uuid.UUID,
    body: SavedJobUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SavedJob:
    """Partial update.

    `exclude_unset` is the whole point of a PATCH: an omitted field
    means "leave unchanged", while an explicit `null` clears an optional
    scalar. The three required fields reject `null` in the schema, so
    there is no request shape that can blank a row's identity. Same
    contract as app/api/v1/profile.py's PATCH.
    """
    saved_job = await _get_owned_job(db, saved_job_id, current_user)

    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        if field == "employment_type" and value is not None:
            value = value.value if hasattr(value, "value") else value
        setattr(saved_job, field, value)

    await db.commit()
    await db.refresh(saved_job)
    return saved_job


@router.delete("/{saved_job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_job(
    saved_job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a saved job.

    A hard delete, not a tombstone. Nothing re-creates a saved job —
    only the user saving it again — so there is nothing for a tombstone
    to suppress, unlike a rejected candidate skill that an extraction
    re-run would otherwise resurrect (app/schemas/skill.py).
    """
    saved_job = await _get_owned_job(db, saved_job_id, current_user)
    await db.delete(saved_job)
    await db.commit()
