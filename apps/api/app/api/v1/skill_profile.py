"""The candidate's unified, evidence-backed skill profile (Prompt 3.4).

    GET /api/v1/skill-profile  -> summary + per-skill provenance

Three independent writers now populate the same two tables — resume
extraction (Prompt 2.4), GitHub extraction (Prompt 3.3), and manual
entry. This module answers the question none of them answers on its own:
"what skills do I have, and how do we know?"

STRICTLY READ-ONLY. No INSERT, no UPDATE, no DELETE, no commit, no
Celery task, no migration, no new table. `candidate_skills` and
`skill_evidence` remain the only source of truth; this derives a view of
them on every request and stores nothing.

WHY NOTHING IS PERSISTED. A stored aggregate would be a cache with no
invalidation trigger: both extractors run inside Celery workers, outside
any request, so a cached count or rollup would go stale the moment one
of them wrote evidence and nothing would recompute it — precisely the
silent drift Prompt 3.3's full-sweep reconciliation exists to prevent.
It would also need its own idempotency story, duplicating one already
built and tested, and would pre-empt Prompt 4.x by persisting a number
4.x did not choose. Deriving on read costs three indexed queries over
data bounded by a ~33-entry curated taxonomy and Prompt 3.2's 20-repo
cap.

WHY THIS IS A SEPARATE ROUTER FROM /candidate-skills. That endpoint is
the MUTATION surface — POST adds a skill, PATCH records a decision. This
one is presentation. Keeping them apart means the review contract (and
its tests, which Prompt 4.x will also depend on) is untouched by
anything here, and a GET-only module has no ownership-check branch to
get wrong.

OWNERSHIP IS STRUCTURAL, the same shape as GET /api/v1/candidate-skills
and the GitHub routes: no user id in the path, body or query string.
`user_id` comes only from the JWT, so one user cannot address another's
profile even to be refused.

NO SCORE IS COMPUTED HERE. See SkillProfileEntry in app/schemas/skill.py.
"""

import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.db import get_db
from app.models.candidate_skill import CandidateSkill
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.schemas.skill import (
    CandidateSkillStatus,
    EvidenceSourceType,
    ExtractionMethod,
    SkillCategory,
    SkillEvidenceResponse,
    SkillProfileEntry,
    SkillProfileResponse,
    SkillProfileSummary,
)
from app.skill_provenance import label_for, load_resume_labels

router = APIRouter()

# Statuses that appear in the `skills` array. "rejected" is deliberately
# absent — it is a tombstone meaning "this is not mine", so listing it in
# a *profile* would contradict the user's own decision. It is still
# counted in the summary, so nothing is silently dropped.
_LISTED_STATUSES = (CandidateSkillStatus.CONFIRMED.value, CandidateSkillStatus.SUGGESTED.value)


@router.get("", response_model=SkillProfileResponse)
async def read_skill_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillProfileResponse:
    """Derive the caller's skill profile from stored evidence.

    Three bounded queries, no N+1: the skills, their evidence, and one
    lookup of the caller's resume filenames for provenance labels.

    Ordering is fixed — skills by name, evidence by (created_at, id) —
    so two identical requests return byte-identical JSON. That is what
    makes the view trustworthy without being cached: it is a pure
    function of committed rows.
    """
    rows = (
        await db.execute(
            select(CandidateSkill, Skill)
            .join(Skill, Skill.id == CandidateSkill.skill_id)
            .where(CandidateSkill.user_id == current_user.id)
            .order_by(Skill.name)
        )
    ).all()

    # Counts cover EVERY status, including rejected; the listing below
    # filters. Computing both from one query keeps the summary and the
    # list guaranteed consistent with each other.
    status_counts: dict[str, int] = defaultdict(int)
    for candidate_skill, _ in rows:
        status_counts[candidate_skill.status] += 1

    evidence_by_skill = await _load_evidence(db, [cs.id for cs, _ in rows])
    resume_labels = await load_resume_labels(db, current_user.id)

    # A source appears with 0 rather than being absent, so a client never
    # has to distinguish "contributed nothing" from "key missing".
    by_source: dict[EvidenceSourceType, int] = {source: 0 for source in EvidenceSourceType}
    multi_source = 0
    entries: list[SkillProfileEntry] = []

    for candidate_skill, skill in rows:
        evidence = evidence_by_skill.get(candidate_skill.id, [])

        # DISTINCT source types, not evidence rows — one repository can
        # write four rows for one skill (Prompt 3.3's four extraction
        # methods) and that is still a single source supporting it.
        sources = sorted({EvidenceSourceType(row.source_type) for row in evidence})

        # Rejected skills are excluded from the listing AND from the
        # source rollup: a source cannot be said to "support" a skill the
        # user has explicitly disowned.
        if candidate_skill.status not in _LISTED_STATUSES:
            continue

        for source in sources:
            by_source[source] += 1
        if len(sources) > 1:
            multi_source += 1

        entries.append(
            SkillProfileEntry(
                id=candidate_skill.id,
                skill_id=skill.id,
                skill_name=skill.name,
                skill_category=SkillCategory(skill.category) if skill.category else None,
                status=CandidateSkillStatus(candidate_skill.status),
                sources=sources,
                evidence_count=len(evidence),
                strongest_evidence_confidence=_strongest(evidence),
                evidence=[_to_evidence_response(row, resume_labels) for row in evidence],
            )
        )

    confirmed = status_counts[CandidateSkillStatus.CONFIRMED.value]
    suggested = status_counts[CandidateSkillStatus.SUGGESTED.value]
    return SkillProfileResponse(
        summary=SkillProfileSummary(
            total=confirmed + suggested,
            confirmed=confirmed,
            suggested=suggested,
            rejected=status_counts[CandidateSkillStatus.REJECTED.value],
            by_source=by_source,
            multi_source=multi_source,
            reviewed=suggested == 0,
        ),
        skills=entries,
    )


def _strongest(evidence: list[SkillEvidence]) -> float:
    """The single highest confidence among this skill's evidence.

    `max()`, deliberately — a SELECTION of one stored value, never a
    blend of several. Averaging or weighting would invent the ranking
    model Prompt 4.x owns; see SkillProfileEntry in app/schemas/skill.py.

    0.0 when there is no evidence at all, which is reachable: a confirmed
    skill survives losing every piece of evidence (a disconnected GitHub
    account, a deleted resume) because the user asserted it. That is the
    existing, intended semantics from Prompt 3.3, not a gap.
    """
    if not evidence:
        return 0.0
    return float(max(row.confidence for row in evidence))


def _to_evidence_response(
    row: SkillEvidence, resume_labels: dict[str, str]
) -> SkillEvidenceResponse:
    return SkillEvidenceResponse(
        id=row.id,
        source_type=EvidenceSourceType(row.source_type),
        source_identifier=row.source_identifier,
        excerpt=row.excerpt,
        extraction_method=ExtractionMethod(row.extraction_method),
        confidence=float(row.confidence),
        source_label=label_for(row.source_type, row.source_identifier, resume_labels),
        created_at=row.created_at,
    )


async def _load_evidence(
    db: AsyncSession, candidate_skill_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[SkillEvidence]]:
    """Same query and ordering as app/api/v1/candidate_skill.py's
    equivalent, so the two endpoints can never disagree about what
    evidence exists or in what order it appears."""
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
