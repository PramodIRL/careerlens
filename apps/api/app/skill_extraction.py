"""Persists the results of deterministic resume skill matching into
`candidate_skills` and `skill_evidence` (Prompt 2.4).

Splits cleanly from app/skill_matching.py: that module decides WHAT the
text says, this one decides what to WRITE. All the matching rules are
testable without a database, and everything here is about idempotency
and not trampling the user.

THE INVARIANT THIS MODULE EXISTS TO UPHOLD: it never writes
`candidate_skills.status`. New rows are inserted with ON CONFLICT DO
NOTHING, so an existing row keeps whatever the user decided —
"confirmed" is never downgraded, and "rejected" is never resurrected as
a fresh suggestion. Combined with the reconciliation pass below (which
only ever deletes UNREVIEWED rows that have no evidence left), that
makes "the user's choice survives every future extraction run" a
property you can point at rather than a behavior you hope holds.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate_skill import CandidateSkill
from app.models.resume import Resume
from app.models.skill import Skill, SkillAlias
from app.models.skill_evidence import SkillEvidence
from app.schemas.resume import ResumeStatus
from app.schemas.skill import CandidateSkillStatus, EvidenceSourceType, ExtractionMethod
from app.skill_matching import SkillMatch, SkillTerm, find_skill_matches

logger = logging.getLogger(__name__)


@dataclass
class ExtractionSummary:
    """Per-run counts, returned for logging and asserted on in tests —
    a re-run over unchanged text must report only `unchanged`."""

    skills_matched: int = 0
    candidate_skills_created: int = 0
    evidence_written: int = 0
    evidence_removed: int = 0
    suggestions_removed: int = 0


async def load_taxonomy_terms(db: AsyncSession) -> list[SkillTerm]:
    """Every searchable spelling in the CURATED taxonomy.

    Restricted to skills with a category: a row coined by a user through
    their profile's target skills (Prompt 1.3) has none, and must not
    become matchable — it has no aliases, no category, and no review, so
    treating it as taxonomy would let one user's typo start attaching
    itself to other people's resumes. See docs/decisions.md.
    """
    skills = (await db.scalars(select(Skill).where(Skill.category.is_not(None)))).all()
    curated_ids = {skill.id for skill in skills}
    terms = [
        SkillTerm(skill_id=skill.id, term=skill.name, is_alias=False)
        for skill in skills
        if skill.name.strip()
    ]

    aliases = (await db.scalars(select(SkillAlias))).all()
    terms.extend(
        SkillTerm(skill_id=alias.skill_id, term=alias.alias, is_alias=True)
        for alias in aliases
        if alias.skill_id in curated_ids and alias.alias.strip()
    )
    return terms


async def ensure_candidate_skills(
    db: AsyncSession, user_id: uuid.UUID, skill_ids: set[uuid.UUID]
) -> tuple[dict[uuid.UUID, CandidateSkill], int]:
    """Insert a "suggested" row for any skill the user does not already
    have. Returns (rows keyed by skill id, how many were created).

    SOURCE-AGNOSTIC ON PURPOSE. Shared verbatim by resume extraction
    (below) and GitHub extraction (app/github/skill_evidence.py) rather
    than copied, because the ON CONFLICT DO NOTHING below IS the override
    invariant: it is what guarantees an existing row's status is
    untouched, so no automatic run can downgrade a "confirmed" skill or
    resurrect a "rejected" one. Two copies of that would be two places
    for the invariant to drift.

    It also makes concurrent extraction for the same user safe (the same
    reasoning as app/api/v1/profile.py's `_get_or_create_skill`).
    """
    if not skill_ids:
        return {}, 0

    existing_before = set(
        (
            await db.scalars(
                select(CandidateSkill.skill_id).where(
                    CandidateSkill.user_id == user_id,
                    CandidateSkill.skill_id.in_(skill_ids),
                )
            )
        ).all()
    )

    missing = skill_ids - existing_before
    if missing:
        await db.execute(
            pg_insert(CandidateSkill)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "user_id": user_id,
                        "skill_id": skill_id,
                        "status": CandidateSkillStatus.SUGGESTED.value,
                    }
                    for skill_id in sorted(missing, key=str)
                ]
            )
            .on_conflict_do_nothing(index_elements=["user_id", "skill_id"])
        )
        await db.flush()

    rows = (
        await db.scalars(
            select(CandidateSkill).where(
                CandidateSkill.user_id == user_id, CandidateSkill.skill_id.in_(skill_ids)
            )
        )
    ).all()
    return {row.skill_id: row for row in rows}, len(missing)


async def delete_orphaned_suggestions(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Delete this user's UNREVIEWED candidate skills that no longer have
    evidence from ANY source. Returns how many were removed.

    SOURCE-AGNOSTIC, and deliberately so — this is the second half of the
    override invariant, shared by resume and GitHub reconciliation. Two
    properties make it safe to call from either:

      * It only ever touches rows still in "suggested" state, so a
        confirmed or rejected decision is never undone.
      * It requires zero evidence from EVERY source, not just the source
        that happens to be reconciling. So a skill the resume still
        supports survives a GitHub sweep, a skill GitHub still supports
        survives a resume rerun, and a manually added skill (which always
        carries manual evidence) survives both.
    """
    orphaned = (
        await db.scalars(
            select(CandidateSkill.id)
            .outerjoin(SkillEvidence, SkillEvidence.candidate_skill_id == CandidateSkill.id)
            .where(
                CandidateSkill.user_id == user_id,
                CandidateSkill.status == CandidateSkillStatus.SUGGESTED.value,
                SkillEvidence.id.is_(None),
            )
        )
    ).all()
    if not orphaned:
        return 0
    await db.execute(delete(CandidateSkill).where(CandidateSkill.id.in_(orphaned)))
    return len(orphaned)


async def _write_evidence(
    db: AsyncSession,
    candidate_skill: CandidateSkill,
    match: SkillMatch,
    resume_id: uuid.UUID,
    summary: ExtractionSummary,
) -> None:
    """Upsert one evidence row on Prompt 2.3's natural key.

    The `where` clause on the conflict branch is what makes a re-run a
    true no-op: without it every run would bump `updated_at` even when
    the excerpt and confidence are byte-identical.
    """
    values = {
        "id": uuid.uuid4(),
        "candidate_skill_id": candidate_skill.id,
        "source_type": EvidenceSourceType.RESUME.value,
        "source_identifier": str(resume_id),
        "excerpt": match.excerpt,
        "extraction_method": ExtractionMethod.RESUME_ALIAS_MATCH.value,
        "confidence": match.confidence,
    }
    statement = pg_insert(SkillEvidence).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="uq_skill_evidence_natural_key",
        set_={
            "excerpt": statement.excluded.excerpt,
            "confidence": statement.excluded.confidence,
            "updated_at": func.now(),
        },
        where=(
            SkillEvidence.excerpt.is_distinct_from(statement.excluded.excerpt)
            | SkillEvidence.confidence.is_distinct_from(statement.excluded.confidence)
        ),
    )
    # An INSERT always yields a CursorResult at runtime (it has
    # .rowcount); AsyncSession.execute() is only typed as the generic
    # Result[Any]. Same cast, for the same reason, as app/worker.py's
    # _claim_resume.
    result = cast("CursorResult[Any]", await db.execute(statement))
    if result.rowcount:
        summary.evidence_written += 1


async def _reconcile(
    db: AsyncSession,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    matched_candidate_skill_ids: set[uuid.UUID],
    summary: ExtractionSummary,
) -> None:
    """Remove what this resume no longer supports.

    Needed because the taxonomy is deliberately editable: dropping a
    skill or alias from the seed must not strand a suggestion that
    nothing justifies any more.

    Scoped hard, in two steps. First, delete only THIS resume's
    alias-match evidence for skills it no longer mentions — evidence
    from another resume, from a manual entry, or from GitHub extraction
    (Prompt 3.3) is never touched. Second, delete only candidate skills
    that are still "suggested" AND now have no evidence at all. A
    confirmed or rejected row survives regardless, and so does a
    manually added one (it always carries manual evidence).
    """
    stale_evidence = delete(SkillEvidence).where(
        SkillEvidence.source_type == EvidenceSourceType.RESUME.value,
        SkillEvidence.source_identifier == str(resume_id),
        SkillEvidence.extraction_method == ExtractionMethod.RESUME_ALIAS_MATCH.value,
    )
    if matched_candidate_skill_ids:
        stale_evidence = stale_evidence.where(
            SkillEvidence.candidate_skill_id.notin_(matched_candidate_skill_ids)
        )
    removed = cast("CursorResult[Any]", await db.execute(stale_evidence))
    summary.evidence_removed += removed.rowcount or 0

    summary.suggestions_removed += await delete_orphaned_suggestions(db, user_id)


async def extract_skills_for_resume(db: AsyncSession, resume_id: uuid.UUID) -> ExtractionSummary:
    """Match one resume's extracted text against the curated taxonomy and
    persist the results. Commits once, at the end.

    A no-op unless the resume exists, reached "succeeded", and actually
    has text — there is nothing to match otherwise, and inventing skills
    from a failed extraction is exactly what the Evidence-First rule
    forbids.
    """
    summary = ExtractionSummary()
    resume = await db.get(Resume, resume_id)
    if resume is None:
        logger.info("skill extraction skipped: resume %s no longer exists", resume_id)
        return summary
    if resume.status != ResumeStatus.SUCCEEDED.value or not resume.extracted_text:
        logger.info(
            "skill extraction skipped: resume %s has no extracted text (status=%s)",
            resume_id,
            resume.status,
        )
        return summary

    terms = await load_taxonomy_terms(db)
    matches = find_skill_matches(resume.extracted_text, terms)
    summary.skills_matched = len(matches)

    by_skill, created = await ensure_candidate_skills(
        db, resume.user_id, {match.skill_id for match in matches}
    )
    summary.candidate_skills_created += created

    matched_candidate_skill_ids: set[uuid.UUID] = set()
    for match in matches:
        candidate_skill = by_skill.get(match.skill_id)
        if candidate_skill is None:  # pragma: no cover - defensive
            continue
        matched_candidate_skill_ids.add(candidate_skill.id)
        await _write_evidence(db, candidate_skill, match, resume_id, summary)

    await _reconcile(db, resume.user_id, resume_id, matched_candidate_skill_ids, summary)
    await db.commit()
    return summary
