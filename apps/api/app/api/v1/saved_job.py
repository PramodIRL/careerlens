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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.db import get_db
from app.job_requirements.extract import extract_job_requirements, list_job_requirements
from app.matching.score import RequirementInput, compute_score
from app.models.candidate_skill import CandidateSkill
from app.models.saved_job import SavedJob
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.schemas.saved_job import (
    JobMatchResponse,
    JobSkillRequirementResponse,
    LevelBreakdownResponse,
    MatchedEvidenceResponse,
    MatchedSkillResponse,
    MissingSkillResponse,
    RequirementExtractionMethod,
    RequirementLevelSchema,
    SavedJobCreateRequest,
    SavedJobResponse,
    SavedJobUpdateRequest,
)
from app.schemas.skill import CandidateSkillStatus

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
    # flush, not commit: the row needs an id for the requirements to
    # reference, but both must land in ONE transaction so a job can
    # never be stored with missing requirements.
    await db.flush()

    # Prompt 4.2: derive job-side requirements from the description.
    # Synchronous and NOT best-effort — unlike resume/GitHub extraction,
    # where the import genuinely succeeded on its own, here the
    # extraction is part of saving the job. A failure rolls the whole
    # request back rather than storing a job with no requirements.
    await extract_job_requirements(db, saved_job)

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
    # Captured BEFORE the assignment loop so the comparison below sees
    # the old value.
    description_changed = (
        "description" in updates and updates["description"] != saved_job.description
    )
    for field, value in updates.items():
        if field == "employment_type" and value is not None:
            value = value.value if hasattr(value, "value") else value
        setattr(saved_job, field, value)

    # Re-derive ONLY when the text actually changed. Editing the company
    # or the location leaves every requirement untouched — including its
    # id and timestamps — because nothing about the description moved.
    # Re-sending an identical description is also a no-op, so a client
    # that PATCHes the whole object does not churn rows.
    if description_changed:
        await extract_job_requirements(db, saved_job)

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


@router.get("/{saved_job_id}/requirements", response_model=list[JobSkillRequirementResponse])
async def read_saved_job_requirements(
    saved_job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[JobSkillRequirementResponse]:
    """The skills this saved job asks for, strongest first.

    NESTED UNDER THE JOB, AND READ-ONLY, both deliberately.

    Nested, because ownership then costs nothing extra: `_get_owned_job`
    already proves the caller owns the parent, and a requirement has no
    other route to it. There is no `/requirements/{id}` endpoint, so a
    requirement id is never something a client can address — which
    removes an entire class of ownership mistake rather than guarding
    against it.

    Read-only, because requirements are DERIVED. The way to change them
    is to edit the job description; a mutation endpoint would let stored
    requirements drift away from the text that justifies them, which is
    exactly what the Evidence-First rule forbids.
    """
    saved_job = await _get_owned_job(db, saved_job_id, current_user)
    rows = await list_job_requirements(db, saved_job.id)
    if not rows:
        return []

    skills = {
        skill.id: skill
        for skill in (
            await db.scalars(select(Skill).where(Skill.id.in_([row.skill_id for row in rows])))
        ).all()
    }
    return [
        JobSkillRequirementResponse(
            id=row.id,
            skill_id=row.skill_id,
            skill_name=skills[row.skill_id].name,
            skill_category=skills[row.skill_id].category,
            requirement_level=RequirementLevelSchema(row.requirement_level),
            matched_term=row.matched_term,
            excerpt=row.excerpt,
            confidence=float(row.confidence),
            extraction_method=RequirementExtractionMethod(row.extraction_method),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


# Candidate review states that can SATISFY a job requirement.
#
# "confirmed" is the user's own assertion, and counts even with no
# evidence left — the existing, tested semantics after a resume is
# deleted or GitHub is disconnected.
#
# "suggested" counts too: an extractor found real, persisted evidence
# and the user simply has not reviewed it yet. Discarding it would score
# every new candidate at 0% until they clicked through every skill,
# which measures their attention rather than their ability. The
# distinction is surfaced per skill as `candidate_unreviewed` instead.
#
# "rejected" is deliberately absent. It is a persistent tombstone
# (app/schemas/skill.py), and must never satisfy a requirement however
# much stale evidence still hangs off it.
_POSITIVE_STATUSES = frozenset(
    {CandidateSkillStatus.CONFIRMED.value, CandidateSkillStatus.SUGGESTED.value}
)


@router.get("/{saved_job_id}/match", response_model=JobMatchResponse)
async def read_saved_job_match(
    saved_job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobMatchResponse:
    """How well the authenticated candidate matches this saved job.

    STRICTLY READ-ONLY. No INSERT, no UPDATE, no DELETE, no commit. It
    reads five tables and returns a computed view; `candidate_skills`,
    `skill_evidence` and `job_skill_requirements` remain the only
    sources of truth, and the matcher authors no evidence of its own.

    RECOMPUTED EVERY REQUEST, not stored. A persisted score would be a
    cache with no invalidation trigger — confirming a skill, rejecting
    one, or editing the description would each silently stale it — so
    deriving on read is what keeps the number honest without a "rebuild
    score" workflow to forget.

    MATCHING IS ON CANONICAL `skill_id`, never on skill names. Both
    sides resolved aliases to the same curated taxonomy at extraction
    time, so no string comparison and no matcher run happens here.

    OWNERSHIP IS STRUCTURAL. The candidate IS the authenticated user —
    there is no candidate id or user id anywhere in the path, body or
    query — and the job must be theirs, proved by the same
    `_get_owned_job` the rest of this module uses. Comparing yourself
    against somebody else's job is unreachable rather than forbidden.

    FOUR BOUNDED READS, no N+1: the job (ownership), its requirements,
    the caller's candidate skills for exactly those skill ids, and the
    evidence for the ones that matched. Every set is bounded by the
    ~33-entry curated taxonomy.
    """
    saved_job = await _get_owned_job(db, saved_job_id, current_user)
    requirements = await list_job_requirements(db, saved_job.id)

    if not requirements:
        # No recognised requirements: an empty score whose
        # `has_requirements=False` tells the client to say "no skill
        # requirements detected" rather than "0% match".
        empty = compute_score([])
        return _to_match_response(empty, [], [])

    skill_ids = [row.skill_id for row in requirements]
    # One query for the names, one for the candidate's own rows — both
    # restricted to the skills this job actually asks about.
    skills = {
        skill.id: skill
        for skill in (await db.scalars(select(Skill).where(Skill.id.in_(skill_ids)))).all()
    }
    candidate_skills = {
        row.skill_id: row
        for row in (
            await db.scalars(
                select(CandidateSkill).where(
                    CandidateSkill.user_id == current_user.id,
                    CandidateSkill.skill_id.in_(skill_ids),
                )
            )
        ).all()
    }

    matched_ids = [
        skill_id for skill_id, row in candidate_skills.items() if row.status in _POSITIVE_STATUSES
    ]
    evidence_by_skill = await _load_match_evidence(db, candidate_skills, matched_ids)

    score = compute_score(
        [
            RequirementInput(
                skill_id=str(row.skill_id),
                level=row.requirement_level,
                satisfied=row.skill_id in set(matched_ids),
            )
            for row in requirements
        ]
    )

    matched: list[MatchedSkillResponse] = []
    missing: list[MissingSkillResponse] = []
    for row in requirements:
        skill = skills.get(row.skill_id)
        if skill is None:  # pragma: no cover - FK guarantees it
            continue
        candidate = candidate_skills.get(row.skill_id)
        if candidate is not None and candidate.status in _POSITIVE_STATUSES:
            matched.append(
                MatchedSkillResponse(
                    skill_id=row.skill_id,
                    skill_name=skill.name,
                    requirement_level=RequirementLevelSchema(row.requirement_level),
                    job_excerpt=row.excerpt,
                    candidate_status=candidate.status,
                    candidate_unreviewed=(candidate.status == CandidateSkillStatus.SUGGESTED.value),
                    candidate_evidence=[
                        MatchedEvidenceResponse(
                            source_type=item.source_type,
                            excerpt=item.excerpt,
                            confidence=float(item.confidence),
                        )
                        for item in evidence_by_skill.get(row.skill_id, [])
                    ],
                )
            )
        else:
            missing.append(
                MissingSkillResponse(
                    skill_id=row.skill_id,
                    skill_name=skill.name,
                    requirement_level=RequirementLevelSchema(row.requirement_level),
                    job_excerpt=row.excerpt,
                    # Distinguishes "never had it" from "explicitly
                    # disowned it" — very different messages.
                    candidate_rejected=(
                        candidate is not None
                        and candidate.status == CandidateSkillStatus.REJECTED.value
                    ),
                )
            )

    return _to_match_response(score, matched, missing)


async def _load_match_evidence(
    db: AsyncSession,
    candidate_skills: dict[uuid.UUID, CandidateSkill],
    matched_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[SkillEvidence]]:
    """Evidence for the matched skills only, keyed by SKILL id.

    One query for all of them rather than one per skill. Ordered by
    (created_at, id) — the same key app/api/v1/candidate_skill.py uses —
    so the two endpoints can never disagree about the order, and two
    identical match requests return byte-identical JSON.
    """
    if not matched_ids:
        return {}
    candidate_skill_ids = [candidate_skills[skill_id].id for skill_id in matched_ids]
    by_candidate_skill = {candidate_skills[s].id: s for s in matched_ids}

    rows = (
        await db.scalars(
            select(SkillEvidence)
            .where(SkillEvidence.candidate_skill_id.in_(candidate_skill_ids))
            .order_by(SkillEvidence.created_at, SkillEvidence.id)
        )
    ).all()

    grouped: dict[uuid.UUID, list[SkillEvidence]] = {}
    for row in rows:
        grouped.setdefault(by_candidate_skill[row.candidate_skill_id], []).append(row)
    return grouped


def _to_match_response(
    score: Any,
    matched: list[MatchedSkillResponse],
    missing: list[MissingSkillResponse],
) -> JobMatchResponse:
    return JobMatchResponse(
        formula_version=score.formula_version,
        overall_score=score.overall_score,
        earned_weight=score.earned_weight,
        obtainable_weight=score.obtainable_weight,
        has_requirements=score.has_requirements,
        required_matched=score.required_matched,
        required_total=score.required_total,
        by_level={
            level: LevelBreakdownResponse(matched=b.matched, total=b.total)
            for level, b in score.by_level.items()
        },
        weights=score.weights,
        matched_skills=matched,
        missing_skills=missing,
        # The subset that actually blocks the candidate. Reported
        # separately because skill_match_v1 does not penalise for it —
        # this is what makes a missing hard requirement unmistakable
        # even when the weighted score looks healthy.
        required_missing=[
            item for item in missing if item.requirement_level == RequirementLevelSchema.REQUIRED
        ],
    )
