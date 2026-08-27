"""Assembling the roadmap's inputs from persisted rows (Prompt 6.3).

WHERE TOP-N IS ENFORCED, AND WHY IT IS HERE. Jobs outside the user's
selection are dropped BEFORE anything is aggregated and before any fact
is built. Not filtered from the output, not excluded by a validator —
never assembled. An unselected job therefore cannot contribute a weight
to `roadmap_priority_v1`, cannot appear as an affected job, and cannot
be named by a model that was never given its id. "Jobs outside Top-N
cannot appear as roadmap causes" is a property of what this module
builds rather than a rule something downstream has to enforce.

IT COMPUTES NO SCORE AND NO GAP. `/match` and `/gaps` are called
unchanged, through the same handlers a client calls, so the roadmap is
reading exactly what the user sees on the job — `skill_match_v1` and
`skill_gap_v1` are not re-derived, re-weighted or re-interpreted here.

WEAK EVIDENCE IS `skill_gap_v1`'s OWN BUCKET. `needs_confirmation` means
persisted evidence exists and the user has not reviewed it — which is
precisely "may already have the skill, lacks reviewed evidence". Reusing
the bucket rather than inventing a threshold means the roadmap's idea of
weak cannot drift from what the gap panel shows. Two things are
deliberately NOT used: `confidence`, which is match quality rather than
capability (app/matching/score.py and app/schemas/skill.py both refuse
to treat it otherwise), and `rejected_requirements`, because the user
disowned that skill and putting it back in a plan overrides their own
decision.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate_skill import CandidateSkill
from app.models.saved_job import SavedJob
from app.models.skill_evidence import SkillEvidence
from app.roadmap.priority import (
    FORMULA_VERSION,
    SCHEDULE_VERSION,
    GapState,
    JobDemand,
    RoadmapItem,
    SelectedJob,
    week_count,
)
from app.roadmap.schema import (
    MAX_EVIDENCE_PER_ITEM,
    MAX_EXCERPT_CHARS,
    RoadmapEvidenceFact,
    RoadmapFacts,
    RoadmapItemFact,
    RoadmapJobFact,
    RoadmapPlanFacts,
)
from app.schemas.saved_job import JobGapResponse, JobMatchResponse, RequirementLevelSchema

# Which gap buckets become which roadmap state. The mapping is the whole
# translation between `skill_gap_v1` and `roadmap_priority_v1`, kept in
# one place so it can be read at a glance.
_GAP_STATE_BY_LEVEL = {
    RequirementLevelSchema.REQUIRED: GapState.MISSING_REQUIRED,
    RequirementLevelSchema.PREFERRED: GapState.MISSING_PREFERRED,
    # A "mentioned" skill is informational. It is a real gap in
    # skill_gap_v1's terms but not something to spend a candidate's
    # scarce preparation hours on ahead of a stated requirement, so it
    # ranks in the lowest band alongside weak evidence.
    RequirementLevelSchema.MENTIONED: GapState.WEAK_EVIDENCE,
}


def select_jobs(jobs: list[SavedJob], *, top_n: int | None) -> list[SavedJob]:
    """The user's Top-N, in the user's own order.

    `position` orders; the RANK is the 1-based index here. That is why a
    gap left by a deleted job never has to be repaired: it simply is not
    visible in the sequence this produces.

    `top_n=None` means "all saved jobs".
    """
    ordered = sorted(jobs, key=lambda job: (job.position, job.created_at, job.id))
    return ordered if top_n is None else ordered[:top_n]


def to_selected(jobs: list[SavedJob], match_scores: dict[uuid.UUID, int]) -> list[SelectedJob]:
    """Attach the user's rank, and the match score for DISPLAY only."""
    return [
        SelectedJob(
            saved_job_id=job.id,
            title=job.title,
            company=job.company,
            rank=index + 1,
            match_score=match_scores.get(job.id),
        )
        for index, job in enumerate(jobs)
    ]


def collect_demands(
    per_job: dict[uuid.UUID, tuple[JobMatchResponse, JobGapResponse]],
) -> tuple[dict[uuid.UUID, list[JobDemand]], dict[uuid.UUID, str]]:
    """Turn each selected job's match and gaps into per-skill demands.

    Only the SELECTED jobs are ever passed in, so nothing here has to
    exclude anything — see this module's docstring.

    A skill the candidate satisfies with reviewed evidence produces no
    demand at all: `matched_skills` is read only to know what NOT to
    ask for. That is what keeps a strongly matched skill out of the
    plan without a rule anywhere that says "skip matched skills".
    """
    demands: dict[uuid.UUID, list[JobDemand]] = {}
    names: dict[uuid.UUID, str] = {}

    for saved_job_id, (match, gaps) in per_job.items():
        for missing in match.missing_skills:
            # `candidate_rejected` is the user's own tombstone. It is a
            # gap in the score's terms and deliberately not a roadmap
            # item — see this module's docstring.
            if missing.candidate_rejected:
                continue
            names[missing.skill_id] = missing.skill_name
            state = _GAP_STATE_BY_LEVEL[missing.requirement_level]
            demands.setdefault(missing.skill_id, []).append(
                JobDemand(saved_job_id=saved_job_id, state=state)
            )

        for entry in gaps.needs_confirmation:
            names[entry.skill_id] = entry.skill_name
            demands.setdefault(entry.skill_id, []).append(
                JobDemand(saved_job_id=saved_job_id, state=GapState.WEAK_EVIDENCE)
            )

    return demands, names


async def load_evidence_for(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    skill_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[SkillEvidence]]:
    """Stored evidence for these skills, capped per skill.

    Same two-query shape and same (created_at, id) ordering as
    app/explanation/facts.py, and capped for the same reason: a prompt
    must not grow with somebody's import history.
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
    return {skill_id: rows[:MAX_EVIDENCE_PER_ITEM] for skill_id, rows in grouped.items()}


def _normalize_excerpt(excerpt: str | None) -> str | None:
    """Strip control characters and re-cap the length.

    Hygiene only. What stops third-party text escaping into the
    instruction is JSON encoding — see app/roadmap/prompt.py.
    """
    if excerpt is None:
        return None
    cleaned = "".join(char for char in excerpt if char.isprintable() or char == "\n")
    return cleaned[:MAX_EXCERPT_CHARS]


def build_facts(
    *,
    items: list[RoadmapItem],
    selected: list[SelectedJob],
    evidence_by_skill: dict[uuid.UUID, list[SkillEvidence]],
    duration_days: int,
    hours_per_day: float,
    total_hours: float,
) -> RoadmapFacts:
    """Everything the provider may see, and nothing else.

    Built from the DECIDED plan: the items arrive ranked, phased and
    explained, and this only serialises them. Nothing here can change an
    order, and there is no field for a raw resume, a README or a job
    description.
    """
    catalogue: dict[uuid.UUID, RoadmapEvidenceFact] = {}
    item_facts: list[RoadmapItemFact] = []

    for item in items:
        evidence_ids: list[uuid.UUID] = []
        for row in evidence_by_skill.get(item.skill_id, []):
            catalogue.setdefault(
                row.id,
                RoadmapEvidenceFact(
                    evidence_id=row.id,
                    source_type=row.source_type,
                    source_identifier=row.source_identifier,
                    excerpt=_normalize_excerpt(row.excerpt),
                ),
            )
            evidence_ids.append(row.id)
        item.evidence_ids = evidence_ids

        item_facts.append(
            RoadmapItemFact(
                item_id=item.item_id,
                skill_name=item.skill_name,
                state=item.state.value,
                start_day=item.start_day,
                end_day=item.end_day,
                week=item.week,
                score=item.score,
                why=item.why,
                affected_job_ids=[job.saved_job_id for job in item.affected_jobs],
                evidence_ids=evidence_ids,
                estimated_hours=item.estimated_hours,
            )
        )

    return RoadmapFacts(
        plan=RoadmapPlanFacts(
            formula_version=FORMULA_VERSION,
            schedule_version=SCHEDULE_VERSION,
            selected_job_count=len(selected),
            duration_days=duration_days,
            hours_per_day=hours_per_day,
            total_hours=total_hours,
            weeks=week_count(duration_days),
        ),
        jobs=[
            RoadmapJobFact(
                saved_job_id=job.saved_job_id,
                title=job.title,
                company=job.company,
                rank=job.rank,
                match_score=job.match_score,
            )
            for job in selected
        ],
        items=item_facts,
        evidence=list(catalogue.values()),
    )
