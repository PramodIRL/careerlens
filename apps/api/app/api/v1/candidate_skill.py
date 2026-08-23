"""Candidate skill review endpoints (Prompt 2.4): list the caller's
extracted skills with their evidence, confirm or reject a suggestion,
and manually add a skill from the curated taxonomy.

Ownership: list and create are implicitly scoped to `current_user.id` —
there is no user id in the path or body, so a client can never name a
different owner. Update loads the row by its own id first, then checks
`candidate_skill.user_id == current_user.id`; missing is 404,
someone else's is 403. Same shape as app/api/v1/resume.py's
`_get_owned_resume`, and the same reasoning as docs/decisions.md's
profile-ownership entry. Evidence ownership is never checked directly —
it is reachable only through its candidate skill, exactly as
app/models/skill_evidence.py specifies.

MANUAL ADD RESOLVES AGAINST THE CURATED TAXONOMY ONLY. `name` must be a
canonical skill name or a known alias; nothing here ever creates a
`skills` or `skill_aliases` row. This is deliberately narrower than
Prompt 1.3's profile target skills, which DO coin new `skills` rows from
free text, and the two must not be conflated:

    profile target skills  -> aspirational, "I am aiming for this",
                              free text, no evidence
    candidate skills       -> evidential, "I have this, here is why",
                              curated taxonomy only, always evidenced

A candidate skill feeds Prompt 4.x matching and gap analysis, so
founding one on a typo ("Pyton") or an accidental duplicate ("ReactJS"
beside React) would corrupt scoring for everyone. A self-coined skill
would also be permanently unmatchable — it has no aliases for
app/skill_matching.py to find. Extending the vocabulary is the seed's
job: edit app/seeds/skill_taxonomy.py and run `make seed-skills`.

The curated test is `skills.category IS NOT NULL`. Prompt 2.3 made that
column nullable precisely because user-coined skills have none, so it
already means "part of the curated taxonomy" — and it self-heals, since
re-seeding adopts a user-coined row and fills its category if the skill
is later curated. Aliases only ever come from the seed, so the alias
path implies curated by construction.

There is deliberately NO DELETE route. Hard-deleting a candidate skill
would let the next extraction run re-suggest it, silently discarding the
user's decision; rejection (a persistent tombstone) is the removal
mechanism. See app/schemas/skill.py's CandidateSkillStatus.
"""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.db import get_db
from app.models.candidate_skill import CandidateSkill
from app.models.skill import Skill, SkillAlias
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.schemas.skill import (
    CandidateSkillCreateRequest,
    CandidateSkillResponse,
    CandidateSkillStatus,
    CandidateSkillUpdateRequest,
    EvidenceSourceType,
    ExtractionMethod,
    SkillCategory,
    SkillEvidenceResponse,
)

router = APIRouter()

_NOT_FOUND = "candidate skill not found"
_NOT_YOURS = "not authorized to access this candidate skill"
_MANUAL_CONFIDENCE = Decimal("1.00")


def _unknown_skill(name: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=(
            f"'{name}' is not in the skill taxonomy — only curated skills and their "
            "known aliases can be added"
        ),
    )


async def _resolve_curated_skill(db: AsyncSession, name: str) -> Skill:
    """Resolve user input to a curated canonical Skill, or raise 422.

    The slug rule is `name.strip().casefold()` — identical to
    app/api/v1/profile.py's `_get_or_create_skill` and
    scripts/seed_skills.py, so "Python", "python" and " python " all
    reach the same row.
    """
    slug = name.strip().casefold()

    curated = await db.scalar(select(Skill).where(Skill.slug == slug, Skill.category.is_not(None)))
    if curated is not None:
        return curated

    via_alias = await db.scalar(
        select(Skill)
        .join(SkillAlias, SkillAlias.skill_id == Skill.id)
        .where(SkillAlias.alias_slug == slug)
    )
    if via_alias is not None:
        return via_alias

    # Reached when the name is unknown, and ALSO when a `skills` row
    # exists but was coined through Prompt 1.3's target skills
    # (category IS NULL) — that is the intended behavior, not an
    # oversight: a target skill is an aspiration, not evidence.
    raise _unknown_skill(name)


async def _load_evidence(
    db: AsyncSession, candidate_skill_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[SkillEvidence]]:
    if not candidate_skill_ids:
        return {}
    rows = (
        await db.scalars(
            select(SkillEvidence)
            .where(SkillEvidence.candidate_skill_id.in_(candidate_skill_ids))
            .order_by(SkillEvidence.created_at, SkillEvidence.id)
        )
    ).all()
    grouped: dict[uuid.UUID, list[SkillEvidence]] = {}
    for row in rows:
        grouped.setdefault(row.candidate_skill_id, []).append(row)
    return grouped


def _to_response(
    candidate_skill: CandidateSkill, skill: Skill, evidence: list[SkillEvidence]
) -> CandidateSkillResponse:
    return CandidateSkillResponse(
        id=candidate_skill.id,
        skill_id=skill.id,
        skill_name=skill.name,
        skill_category=SkillCategory(skill.category) if skill.category else None,
        status=CandidateSkillStatus(candidate_skill.status),
        evidence=[
            SkillEvidenceResponse(
                id=row.id,
                source_type=EvidenceSourceType(row.source_type),
                source_identifier=row.source_identifier,
                excerpt=row.excerpt,
                extraction_method=ExtractionMethod(row.extraction_method),
                confidence=float(row.confidence),
                created_at=row.created_at,
            )
            for row in evidence
        ],
        created_at=candidate_skill.created_at,
        updated_at=candidate_skill.updated_at,
    )


async def _get_owned_candidate_skill(
    db: AsyncSession, candidate_skill_id: uuid.UUID, current_user: User
) -> CandidateSkill:
    candidate_skill = await db.get(CandidateSkill, candidate_skill_id)
    if candidate_skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if candidate_skill.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_NOT_YOURS)
    return candidate_skill


@router.get("", response_model=list[CandidateSkillResponse])
async def list_candidate_skills(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CandidateSkillResponse]:
    rows = (
        await db.execute(
            select(CandidateSkill, Skill)
            .join(Skill, Skill.id == CandidateSkill.skill_id)
            .where(CandidateSkill.user_id == current_user.id)
            .order_by(Skill.name)
        )
    ).all()
    evidence = await _load_evidence(db, [candidate_skill.id for candidate_skill, _ in rows])
    return [
        _to_response(candidate_skill, skill, evidence.get(candidate_skill.id, []))
        for candidate_skill, skill in rows
    ]


@router.post("", response_model=CandidateSkillResponse)
async def add_candidate_skill(
    body: CandidateSkillCreateRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CandidateSkillResponse:
    """Manually claim a curated skill. 201 when the candidate skill is
    new, 200 when an existing row was updated — including a previously
    rejected one, which this flips back to confirmed."""
    skill = await _resolve_curated_skill(db, body.name)

    existing = await db.scalar(
        select(CandidateSkill).where(
            CandidateSkill.user_id == current_user.id, CandidateSkill.skill_id == skill.id
        )
    )
    if existing is None:
        candidate_skill = CandidateSkill(
            user_id=current_user.id,
            skill_id=skill.id,
            status=CandidateSkillStatus.CONFIRMED.value,
        )
        db.add(candidate_skill)
        response.status_code = status.HTTP_201_CREATED
    else:
        # A deliberate status write — this endpoint and the PATCH below
        # are the only places one may happen. The extractor never does.
        candidate_skill = existing
        candidate_skill.status = CandidateSkillStatus.CONFIRMED.value
        response.status_code = status.HTTP_200_OK
    await db.flush()

    # source_identifier is taken from the authenticated user, never from
    # the request body (app/models/skill_evidence.py's manual contract).
    # ON CONFLICT DO NOTHING keeps re-adding an already-added skill from
    # duplicating its manual evidence.
    await db.execute(
        pg_insert(SkillEvidence)
        .values(
            id=uuid.uuid4(),
            candidate_skill_id=candidate_skill.id,
            source_type=EvidenceSourceType.MANUAL.value,
            source_identifier=str(current_user.id),
            # Null excerpt: a manual assertion has nothing to quote, and
            # Prompt 2.3 chose nullable over inviting fabricated filler.
            excerpt=None,
            extraction_method=ExtractionMethod.MANUAL_ENTRY.value,
            confidence=_MANUAL_CONFIDENCE,
        )
        .on_conflict_do_nothing(constraint="uq_skill_evidence_natural_key")
    )
    await db.commit()
    await db.refresh(candidate_skill)

    evidence = await _load_evidence(db, [candidate_skill.id])
    return _to_response(candidate_skill, skill, evidence.get(candidate_skill.id, []))


@router.patch("/{candidate_skill_id}", response_model=CandidateSkillResponse)
async def update_candidate_skill(
    candidate_skill_id: uuid.UUID,
    body: CandidateSkillUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CandidateSkillResponse:
    candidate_skill = await _get_owned_candidate_skill(db, candidate_skill_id, current_user)
    candidate_skill.status = body.status.value
    await db.commit()
    await db.refresh(candidate_skill)

    skill = await db.get(Skill, candidate_skill.skill_id)
    assert skill is not None  # FK guarantees it
    evidence = await _load_evidence(db, [candidate_skill.id])
    return _to_response(candidate_skill, skill, evidence.get(candidate_skill.id, []))
