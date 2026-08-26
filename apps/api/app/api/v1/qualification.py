"""Candidate qualification endpoints (Prompt 5.1a): read and declare the
academic and experience facts job postings set bars against.

    GET   /api/v1/qualifications  -> everything declared, null if unknown
    PATCH /api/v1/qualifications  -> partial update

OWNERSHIP IS STRUCTURAL. No user id appears in the path, body or query
string — `user_id` comes only from the JWT, so one candidate cannot
address another's academic record even to be refused. This is the same
shape as `/candidate-skills` and the GitHub routes, and deliberately NOT
the id-addressable shape `/profiles/{user_id}` uses: these are exam
results and graduation dates, and the safest surface for them is one
with nothing to enumerate.

THIS DATA IS SENSITIVE, and the module treats it that way. Nothing here
is logged, nothing is returned to any other user, and no other endpoint
in this application exposes it. `GET /api/v1/saved-jobs/{id}/eligibility`
reports how the caller's OWN facts compare against a job — never the
facts of anyone else.

RESUME-FIRST SINCE PROMPT 5.1b. Most facts arrive automatically from
app/qualifications/extract.py as `suggested`, carrying the resume line
they were read from. These routes are for CORRECTION: a PATCH stamps
`confirmed`, which is what protects the value from the next extraction
run, and DELETE records a `rejected` tombstone that re-extraction must
never resurrect. Nothing here infers a value — the person typing is the
person the fact is about.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.db import get_db
from app.models.user import User
from app.qualifications.store import (
    QualificationConflictError,
    apply_update,
    load_rows,
    reject_fact,
    to_response,
)
from app.schemas.qualification import (
    QualificationFactType,
    QualificationsResponse,
    QualificationsUpdateRequest,
)

router = APIRouter()


@router.get("", response_model=QualificationsResponse)
async def read_qualifications(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QualificationsResponse:
    """The caller's own declared facts.

    An all-null response rather than a 404 when nothing is declared:
    "I have not filled this in yet" is the normal starting state, and
    the UI should not need an error branch for its most common case —
    the same choice the resume list and GitHub connection endpoints
    make.
    """
    return to_response(await load_rows(db, current_user.id))


@router.patch("", response_model=QualificationsResponse)
async def update_qualifications(
    body: QualificationsUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QualificationsResponse:
    """Partial update.

    An omitted field is left unchanged; an explicit `null` clears it
    back to unknown. Clearing is a first-class operation here, not an
    afterthought: a candidate who mistyped their CGPA must be able to
    remove it entirely rather than being stuck with a wrong number that
    a job could report them as failing.
    """
    rows = await load_rows(db, current_user.id)
    try:
        await apply_update(db, current_user.id, body, rows)
    except QualificationConflictError as exc:
        # 422, matching the schema-level validation errors this endpoint
        # already returns — the request was well-formed but describes a
        # state that cannot exist.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    await db.commit()
    return to_response(await load_rows(db, current_user.id))


@router.delete("/{fact_type}", response_model=QualificationsResponse)
async def reject_qualification(
    fact_type: QualificationFactType,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QualificationsResponse:
    """Mark one extracted fact as not the candidate's.

    A TOMBSTONE, NOT A DELETE, exactly as rejecting a candidate skill
    is: if the row disappeared, the next resume run would faithfully
    re-suggest the same value and silently discard the decision. The
    fact reads as unknown everywhere afterwards — including in
    eligibility, where it must never count as a value that still
    stands — while the row keeps the rejection.

    404 when there is no such fact to reject. Ownership is structural:
    `fact_type` names a KIND of fact, never a row belonging to somebody
    else, and `user_id` still comes only from the JWT.
    """
    row = await reject_fact(db, current_user.id, fact_type.value)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no such qualification to reject",
        )
    await db.commit()
    return to_response(await load_rows(db, current_user.id))
