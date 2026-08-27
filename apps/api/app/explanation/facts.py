"""Assembling the fact bundle from persisted rows (Prompt 6.1).

WHAT IS DELIBERATELY ABSENT. There is no resume text, no README, and no
job description here — only `title` and `company`, which are stored
fields a person typed. app/explanation/schema.py has nowhere to put the
rest, so this module could not leak it even by mistake.

WHERE THE FACTS COME FROM. The `/match`, `/gaps` and `/semantic`
responses, unchanged — the explanation reads the same objects the API
already returns rather than recomputing anything. Nothing in this
package re-derives a score, and `skill_match_v1` is not imported.

THE ONE EXTRA QUERY. Evidence ids are what make a citation checkable,
and `MatchedEvidenceResponse` does not carry them. Rather than add a
field to the match response — in the slice that promises `/match` is
byte-for-byte unchanged — this module loads them itself, bounded by the
skills this job actually asks about.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.explanation.schema import (
    EvidenceFact,
    ExplanationFacts,
    GapFacts,
    JobFacts,
    LevelFact,
    ScoreFacts,
    SemanticFacts,
    SemanticHitFact,
    SkillFact,
)
from app.models.candidate_skill import CandidateSkill
from app.models.saved_job import SavedJob
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.schemas.saved_job import (
    JobGapResponse,
    JobMatchResponse,
    JobSemanticResponse,
    RequirementLevelSchema,
)


async def load_taxonomy_names(db: AsyncSession) -> frozenset[str]:
    """The curated skill vocabulary, for the validator's invented-skill
    check. One bounded read of a ~33-row table — and it is NEVER put in
    the prompt: handing a model every skill the system knows is an
    invitation to pick one."""
    return frozenset((await db.scalars(select(Skill.name))).all())


async def load_evidence_by_skill(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    skill_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[SkillEvidence]]:
    """Real evidence rows for the candidate, keyed by SKILL id.

    Two queries, never one per skill, and ordered by (created_at, id) —
    the same key app/api/v1/saved_job.py uses, so the ids cited in an
    explanation come back in the same order the match panel shows them.
    """
    if not skill_ids:
        return {}

    candidate_skills = (
        await db.scalars(
            select(CandidateSkill).where(
                CandidateSkill.user_id == user_id,
                CandidateSkill.skill_id.in_(skill_ids),
            )
        )
    ).all()
    if not candidate_skills:
        return {}

    skill_by_candidate = {row.id: row.skill_id for row in candidate_skills}
    rows = (
        await db.scalars(
            select(SkillEvidence)
            .where(SkillEvidence.candidate_skill_id.in_(list(skill_by_candidate)))
            .order_by(SkillEvidence.created_at, SkillEvidence.id)
        )
    ).all()

    grouped: dict[uuid.UUID, list[SkillEvidence]] = {}
    for row in rows:
        grouped.setdefault(skill_by_candidate[row.candidate_skill_id], []).append(row)
    return grouped


def build_facts(
    *,
    job: SavedJob,
    match: JobMatchResponse,
    gaps: JobGapResponse,
    semantic: JobSemanticResponse,
    evidence_by_skill: dict[uuid.UUID, list[SkillEvidence]],
) -> ExplanationFacts:
    """PURE: responses and rows in, one fact bundle out. No database, no
    provider, no scoring — the same split every other module in this
    codebase keeps between deriving facts and acting on them."""
    catalogue: dict[uuid.UUID, EvidenceFact] = {}

    def _evidence_ids(skill_id: uuid.UUID) -> list[uuid.UUID]:
        ids: list[uuid.UUID] = []
        for row in evidence_by_skill.get(skill_id, []):
            catalogue.setdefault(
                row.id,
                EvidenceFact(
                    evidence_id=row.id,
                    source_type=row.source_type,
                    source_identifier=row.source_identifier,
                    excerpt=row.excerpt,
                ),
            )
            ids.append(row.id)
        return ids

    matched = [
        SkillFact(
            skill_name=item.skill_name,
            requirement_level=item.requirement_level.value,
            candidate_status=item.candidate_status,
            evidence_ids=_evidence_ids(item.skill_id),
        )
        for item in match.matched_skills
    ]
    missing_required = [
        SkillFact(
            skill_name=item.skill_name,
            requirement_level=item.requirement_level.value,
            candidate_rejected=item.candidate_rejected,
            evidence_ids=_evidence_ids(item.skill_id),
        )
        for item in match.missing_skills
        if item.requirement_level is RequirementLevelSchema.REQUIRED
    ]
    missing_other = [
        SkillFact(
            skill_name=item.skill_name,
            requirement_level=item.requirement_level.value,
            candidate_rejected=item.candidate_rejected,
            evidence_ids=_evidence_ids(item.skill_id),
        )
        for item in match.missing_skills
        if item.requirement_level is not RequirementLevelSchema.REQUIRED
    ]

    # Semantic hits point at evidence rows too, and may reach ones this
    # job's requirements never touched — they belong in the catalogue,
    # or a legitimate citation would read as unknown.
    hits: list[SemanticHitFact] = []
    for hit in semantic.evidence:
        catalogue.setdefault(
            hit.evidence_id,
            EvidenceFact(
                evidence_id=hit.evidence_id,
                source_type=hit.evidence_source_type,
                source_identifier=hit.evidence_source_identifier,
                excerpt=hit.excerpt,
            ),
        )
        hits.append(SemanticHitFact(evidence_id=hit.evidence_id, similarity=hit.similarity))

    return ExplanationFacts(
        job=JobFacts(saved_job_id=job.id, title=job.title, company=job.company),
        score=ScoreFacts(
            formula_version=match.formula_version,
            overall_score=match.overall_score,
            earned_weight=match.earned_weight,
            obtainable_weight=match.obtainable_weight,
            has_requirements=match.has_requirements,
            required_matched=match.required_matched,
            required_total=match.required_total,
            by_level={
                level: LevelFact(matched=value.matched, total=value.total)
                for level, value in match.by_level.items()
            },
            weights=dict(match.weights),
        ),
        matched_skills=matched,
        missing_required_skills=missing_required,
        missing_other_skills=missing_other,
        gaps=GapFacts(
            formula_version=gaps.formula_version,
            required_gaps=[entry.skill_name for entry in gaps.required_gaps],
            preferred_gaps=[entry.skill_name for entry in gaps.preferred_gaps],
            informational_gaps=[entry.skill_name for entry in gaps.informational_gaps],
            needs_confirmation=[entry.skill_name for entry in gaps.needs_confirmation],
            rejected_requirements=[entry.skill_name for entry in gaps.rejected_requirements],
            totals=gaps.totals.model_dump(),
        ),
        semantic=SemanticFacts(
            formula_version=semantic.formula_version,
            fit=semantic.fit,
            band=semantic.band,
            model_identifier=semantic.model_identifier,
            considered=semantic.considered,
            hits=hits,
        ),
        # Sorted so two identical requests build a byte-identical
        # bundle, which is what makes the mock's output reproducible.
        evidence=sorted(catalogue.values(), key=lambda row: str(row.evidence_id)),
    )
