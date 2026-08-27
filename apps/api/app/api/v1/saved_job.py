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
from app.eligibility.extract import (
    extract_job_eligibility,
    list_job_eligibility_requirements,
    requirement_type_of,
)
from app.eligibility.resolve import RequirementInput as EligibilityRequirementInput
from app.eligibility.resolve import resolve_eligibility
from app.embeddings.provider import get_embedding_provider
from app.embeddings.semantic_fit import compute_semantic_fit
from app.job_requirements.extract import extract_job_requirements, list_job_requirements
from app.matching.resolve import (
    ResolutionState,
    resolve_requirements,
)
from app.matching.score import RequirementInput, compute_score
from app.models.candidate_skill import CandidateSkill
from app.models.job_eligibility import JobEligibilityRequirement
from app.models.saved_job import SavedJob
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.qualifications.store import has_profile, to_candidate_facts
from app.qualifications.store import load_rows as load_qualification_rows
from app.schemas.eligibility import (
    Comparator,
    EligibilityEntryResponse,
    EligibilityExtractionMethod,
    EligibilityRequirementResponse,
    EligibilityRequirementType,
    EligibilityTotalsResponse,
    JobEligibilityResponse,
)
from app.schemas.saved_job import (
    GapEntryResponse,
    GapTotalsResponse,
    JobGapResponse,
    JobMatchResponse,
    JobSemanticResponse,
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
    SemanticEvidenceResponse,
)

router = APIRouter()

# Separate from skill_match_v1 on purpose: the gap BUCKETING policy can
# evolve without implying the score formula changed.
GAP_FORMULA_VERSION = "skill_gap_v1"

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

    # Prompt 5.1a: the job's qualification bars, derived in the SAME
    # transaction as the skill requirements, so a job can never be
    # stored with one of the two extractions stale. The two write to
    # entirely separate tables and neither reads the other's.
    await extract_job_eligibility(db, saved_job)

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
        await extract_job_eligibility(db, saved_job)

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


# Candidate-side policy — which review states satisfy a requirement,
# and how "missing" decomposes — lives in app/matching/resolve.py, and
# BOTH this endpoint and the gap endpoint below call it. Implementing
# that judgement twice would eventually let /match and /gaps disagree
# about the same skill, which is exactly the contradiction that would
# destroy trust in the explanation.


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

    # ONE resolution, shared with the gap endpoint. Both views of the
    # same judgement come from the same function, so they cannot drift.
    resolutions = resolve_requirements(
        [
            (row.skill_id, skills[row.skill_id].name, row.requirement_level, row.excerpt)
            for row in requirements
            if row.skill_id in skills
        ],
        {skill_id: row.status for skill_id, row in candidate_skills.items()},
    )

    # Evidence for every candidate skill this job touches — not only the
    # satisfying ones, because a REJECTED requirement must be able to
    # show the evidence the user disowned.
    evidence_by_skill = await _load_match_evidence(db, candidate_skills, list(candidate_skills))

    score = compute_score(
        [
            RequirementInput(
                skill_id=str(item.skill_id),
                level=item.requirement_level,
                satisfied=item.satisfied,
            )
            for item in resolutions
        ]
    )

    matched: list[MatchedSkillResponse] = []
    missing: list[MissingSkillResponse] = []
    for item in resolutions:
        if item.satisfied:
            matched.append(
                MatchedSkillResponse(
                    skill_id=item.skill_id,
                    skill_name=item.skill_name,
                    requirement_level=RequirementLevelSchema(item.requirement_level),
                    job_excerpt=item.job_excerpt,
                    candidate_status=item.candidate_status or "",
                    candidate_unreviewed=(item.state is ResolutionState.NEEDS_CONFIRMATION),
                    candidate_evidence=_to_evidence(evidence_by_skill.get(item.skill_id, [])),
                )
            )
        else:
            missing.append(
                MissingSkillResponse(
                    skill_id=item.skill_id,
                    skill_name=item.skill_name,
                    requirement_level=RequirementLevelSchema(item.requirement_level),
                    job_excerpt=item.job_excerpt,
                    # Distinguishes "never had it" from "explicitly
                    # disowned it" — very different messages.
                    candidate_rejected=(item.state is ResolutionState.REJECTED),
                )
            )

    return _to_match_response(score, matched, missing)


def _to_evidence(rows: list[SkillEvidence]) -> list[MatchedEvidenceResponse]:
    """Real stored evidence rows, never generated text."""
    return [
        MatchedEvidenceResponse(
            source_type=row.source_type,
            excerpt=row.excerpt,
            confidence=float(row.confidence),
        )
        for row in rows
    ]


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


@router.get("/{saved_job_id}/gaps", response_model=JobGapResponse)
async def read_saved_job_gaps(
    saved_job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobGapResponse:
    """What this job asks for that the candidate does not satisfy, and
    why.

    BUILT ON THE SAME RESOLVER AS `/match`, so the two can never
    contradict each other: a requirement `/match` reports as matched
    cannot appear in any gap bucket here, and one it reports as missing
    lands in exactly one. That consistency is structural rather than
    something two test suites have to keep in step by hand.

    STRICTLY READ-ONLY and NOT PERSISTED. Gap state changes when a skill
    is confirmed, rejected or manually added, when resume or GitHub
    evidence changes, and when a job edit reconciles requirements — and
    several of those fire from Celery workers outside any request. A
    stored gap row would be stale almost immediately, so it is derived
    on read from four bounded queries.

    IT COMPUTES NO SCORE. `skill_match_v1` stays in
    app/matching/score.py and is not duplicated or re-derived here.

    OWNERSHIP IS STRUCTURAL — the same `_get_owned_job` the rest of this
    module uses, and the candidate is always the authenticated user.
    """
    saved_job = await _get_owned_job(db, saved_job_id, current_user)
    requirements = await list_job_requirements(db, saved_job.id)

    if not requirements:
        return _empty_gap_response()

    skill_ids = [row.skill_id for row in requirements]
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

    resolutions = resolve_requirements(
        [
            (row.skill_id, skills[row.skill_id].name, row.requirement_level, row.excerpt)
            for row in requirements
            if row.skill_id in skills
        ],
        {skill_id: row.status for skill_id, row in candidate_skills.items()},
    )
    # Every candidate skill this job touches, so a REJECTED requirement
    # can show the evidence the user disowned.
    evidence_by_skill = await _load_match_evidence(db, candidate_skills, list(candidate_skills))

    buckets: dict[str, list[GapEntryResponse]] = {
        "required": [],
        "preferred": [],
        "mentioned": [],
        "needs_confirmation": [],
        "rejected": [],
    }
    satisfied = 0
    for item in resolutions:
        if item.state is ResolutionState.SATISFIED:
            satisfied += 1
            continue

        entry = GapEntryResponse(
            skill_id=item.skill_id,
            skill_name=item.skill_name,
            requirement_level=RequirementLevelSchema(item.requirement_level),
            job_excerpt=item.job_excerpt,
            candidate_status=item.candidate_status,
            # Real rows for needs-confirmation and rejected; empty for a
            # genuinely missing skill, never an invented sentence.
            candidate_evidence=_to_evidence(evidence_by_skill.get(item.skill_id, [])),
        )

        if item.state is ResolutionState.NEEDS_CONFIRMATION:
            # Evidence exists — calling this a gap would tell the user to
            # go learn something they have already demonstrated.
            buckets["needs_confirmation"].append(entry)
        elif item.state is ResolutionState.REJECTED:
            # The user's own decision, preserved and shown rather than
            # reported back as an absence.
            buckets["rejected"].append(entry)
        else:
            buckets[item.requirement_level].append(entry)

    # Sorted by skill name, not insertion order, so two identical
    # requests return byte-identical JSON.
    for entries in buckets.values():
        entries.sort(key=lambda entry: entry.skill_name)

    return JobGapResponse(
        formula_version=GAP_FORMULA_VERSION,
        required_gaps=buckets["required"],
        preferred_gaps=buckets["preferred"],
        informational_gaps=buckets["mentioned"],
        needs_confirmation=buckets["needs_confirmation"],
        rejected_requirements=buckets["rejected"],
        totals=GapTotalsResponse(
            required_gaps=len(buckets["required"]),
            preferred_gaps=len(buckets["preferred"]),
            informational_gaps=len(buckets["mentioned"]),
            needs_confirmation=len(buckets["needs_confirmation"]),
            rejected_requirements=len(buckets["rejected"]),
            satisfied=satisfied,
            total_requirements=len(resolutions),
        ),
    )


def _empty_gap_response() -> JobGapResponse:
    """A job with no recognised requirements. Distinct from "you match
    everything" — the UI must not congratulate a candidate for a job
    that simply asks for nothing we recognise."""
    return JobGapResponse(
        formula_version=GAP_FORMULA_VERSION,
        totals=GapTotalsResponse(
            required_gaps=0,
            preferred_gaps=0,
            informational_gaps=0,
            needs_confirmation=0,
            rejected_requirements=0,
            satisfied=0,
            total_requirements=0,
        ),
    )


# --------------------------------------------------------------------
# Job eligibility (Prompt 5.1a)
#
# A SEPARATE DOMAIN FROM SKILLS, end to end. These two endpoints read
# `job_eligibility_requirements` and `candidate_qualifications`, and
# touch neither `skill_match_v1` nor `skill_gap_v1`. `/match` and
# `/gaps` above are byte-for-byte what they were before this prompt —
# no new field, no new input, no cap applied to the score. The two
# answers sit beside each other in the UI and are never multiplied
# together, because "82% skill match" and "does not meet the CGPA bar"
# are different kinds of claim and collapsing them would destroy both.
# --------------------------------------------------------------------


@router.get(
    "/{saved_job_id}/eligibility-requirements",
    response_model=list[EligibilityRequirementResponse],
)
async def read_saved_job_eligibility_requirements(
    saved_job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EligibilityRequirementResponse]:
    """The qualification bars extracted from this job's description.

    The requirements themselves, with no candidate involved — the
    eligibility counterpart of `/requirements`. Useful on its own for
    showing what a posting demands before knowing anything about who is
    asking.

    An empty list when the description states no bar this extractor
    recognises. That is the common case and is NOT an error: most
    postings say nothing about CGPA.
    """
    saved_job = await _get_owned_job(db, saved_job_id, current_user)
    rows = await list_job_eligibility_requirements(db, saved_job.id)
    return [_to_requirement_response(row, values) for row, values in rows]


def _to_requirement_response(
    row: JobEligibilityRequirement, values: list[str]
) -> EligibilityRequirementResponse:
    return EligibilityRequirementResponse(
        id=row.id,
        requirement_type=EligibilityRequirementType(row.requirement_type),
        comparator=Comparator(row.comparator),
        numeric_value=row.numeric_value,
        numeric_max=row.numeric_max,
        value_scale=row.value_scale,
        accepted_values=values,
        requirement_level=row.requirement_level,
        open_ended=row.open_ended,
        matched_term=row.matched_term,
        excerpt=row.excerpt,
        confidence=float(row.confidence),
        extraction_method=EligibilityExtractionMethod(row.extraction_method),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/{saved_job_id}/eligibility", response_model=JobEligibilityResponse)
async def read_saved_job_eligibility(
    saved_job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobEligibilityResponse:
    """How the authenticated candidate stands against this job's bars.

    STRICTLY READ-ONLY AND NOT PERSISTED, for a sharper version of the
    reason `/match` and `/gaps` are: this answer changes for EVERY saved
    job the moment the candidate edits one field of their own
    qualifications. A stored verdict would have no way to observe that,
    so it is derived from four bounded queries on every request.

    UNKNOWN IS NOT FAILURE. A candidate who has declared nothing gets
    `unknown` against every bar and an overall flag of `unknown` — never
    `not_eligible`. Only a value we actually hold, failing a bar we
    actually understood, makes anyone ineligible. See
    app/eligibility/resolve.py.

    IT COMPUTES NO SKILL SCORE and does not read a single skill row.
    """
    saved_job = await _get_owned_job(db, saved_job_id, current_user)
    rows = await list_job_eligibility_requirements(db, saved_job.id)

    inputs: list[EligibilityRequirementInput] = []
    excerpt_by_type: dict[str, str] = {}
    for row, values in rows:
        requirement_type = requirement_type_of(row)
        if requirement_type is None:
            # Written by a newer version than this deployment knows.
            # Skipped rather than raising — see requirement_type_of.
            continue
        inputs.append(
            EligibilityRequirementInput(
                requirement_type=requirement_type,
                comparator=Comparator(row.comparator),
                requirement_level=row.requirement_level,
                numeric_value=row.numeric_value,
                numeric_max=row.numeric_max,
                value_scale=row.value_scale,
                accepted_values=tuple(values),
                open_ended=row.open_ended,
                excerpt=row.excerpt,
                requirement_id=row.id,
            )
        )
        excerpt_by_type[requirement_type.value] = row.excerpt

    qualification_rows = await load_qualification_rows(db, current_user.id)
    # Only CONFIRMED facts are authoritative — see
    # app/qualifications/store.py. A resume-derived suggestion nobody
    # has accepted reads as UNKNOWN, never as a value that counts.
    facts = to_candidate_facts(qualification_rows)
    result = resolve_eligibility(inputs, facts)

    return JobEligibilityResponse(
        formula_version=result.formula_version,
        flag=result.flag,
        has_requirements=result.has_requirements,
        has_qualification_profile=has_profile(qualification_rows),
        totals=EligibilityTotalsResponse(
            satisfied=result.totals.satisfied,
            not_satisfied=result.totals.not_satisfied,
            unknown=result.totals.unknown,
            undetermined=result.totals.undetermined,
            total_requirements=result.totals.total_requirements,
            required_not_satisfied=result.totals.required_not_satisfied,
        ),
        requirements=[
            EligibilityEntryResponse(
                requirement_type=resolution.requirement.requirement_type,
                state=resolution.state,
                comparator=resolution.requirement.comparator,
                requirement_numeric=resolution.requirement.numeric_value,
                requirement_max=resolution.requirement.numeric_max,
                requirement_scale=resolution.requirement.value_scale,
                accepted_values=list(resolution.requirement.accepted_values),
                requirement_level=resolution.requirement.requirement_level,
                candidate_numeric=resolution.candidate_numeric,
                candidate_text=resolution.candidate_text,
                candidate_scale=resolution.candidate_scale,
                reason=resolution.reason,
                excerpt=excerpt_by_type.get(resolution.requirement.requirement_type.value, ""),
            )
            for resolution in result.resolutions
        ],
    )


@router.get("/{saved_job_id}/semantic", response_model=JobSemanticResponse)
async def read_saved_job_semantic(
    saved_job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobSemanticResponse:
    """Candidate evidence that sits near this job's wording.

    SUPPORTING EVIDENCE, NOT A SKILL CLAIM. This endpoint answers "what
    of the candidate's stored evidence looks relevant to this posting?"
    It does not answer "does the candidate have skill X" — that is
    `/match`, and nothing here changes it. `overall_score`,
    `skill_match_v1` and `skill_gap_v1` are untouched by this route, and
    no response from it can make a required skill count as satisfied.

    ITS OWN ENDPOINT so a cold or unconfigured model can never slow down
    or break the deterministic score. A client that never calls this
    sees exactly the product Phase 4 shipped.

    EMBEDS NOTHING IN THE REQUEST PATH. Both the job and the evidence
    were embedded ahead of time by `make embeddings-backfill`; this
    reads stored vectors. A job with no embeddings yet returns an empty
    result rather than an error — "nothing to compare" is a truthful
    answer, and inventing one would not be.

    OWNERSHIP is the same `_get_owned_job` the rest of this module uses,
    and retrieval filters on `user_id` independently, so evidence can
    never cross users.
    """
    saved_job = await _get_owned_job(db, saved_job_id, current_user)

    result = await compute_semantic_fit(
        db,
        user_id=current_user.id,
        saved_job_id=saved_job.id,
        model_identifier=get_embedding_provider().model_identifier,
    )

    return JobSemanticResponse(
        formula_version=result.formula_version,
        fit=result.fit,
        band=result.band,
        model_identifier=result.model_identifier,
        considered=result.considered,
        evidence=[
            SemanticEvidenceResponse(
                embedding_id=hit.hit.embedding_id,
                source_type=hit.hit.source_type,
                source_id=hit.hit.source_id,
                evidence_id=hit.evidence_id,
                excerpt=hit.excerpt,
                evidence_source_type=hit.evidence_source_type,
                evidence_source_identifier=hit.evidence_source_identifier,
                similarity=hit.hit.similarity,
            )
            for hit in result.hits
        ],
    )
