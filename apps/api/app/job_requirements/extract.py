"""Persists deterministic job-requirement extraction into
`job_skill_requirements` (Prompt 4.2).

Splits from app/job_requirements/classify.py the same way
app/skill_extraction.py splits from app/skill_matching.py: that module
decides WHAT the description says, this one decides what to WRITE. All
the classification rules stay testable without a database, and
everything here is about desired-state reconciliation.

DESIRED-STATE, NOT APPEND-ONLY. Every run computes the complete set of
requirements the current description justifies, then makes the table
match it: upsert what belongs, delete what no longer does. So editing

    "Python is required. Docker is preferred."
 -> "Python is preferred. PostgreSQL is required."

updates Python IN PLACE (same id, same created_at), inserts PostgreSQL,
and removes Docker — rather than accumulating three stale rows.

THE BOUNDARY THIS MODULE MUST NOT CROSS. It writes to exactly one table.
No candidate skills, no skill evidence, no resume or GitHub side effects:
a job requirement is a fact about a POSTING, and connecting it to a
person is Prompt 4.3's business. Asserted by tests rather than assumed,
because it would be very easy to add "just a little" matching here.
"""

import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.job_requirements.classify import (
    RequirementLevel,
    classify_at,
    clause_excerpt,
    strongest,
)
from app.models.job_requirement import JobSkillRequirement
from app.models.saved_job import SavedJob
from app.schemas.saved_job import RequirementExtractionMethod
from app.skill_extraction import load_taxonomy_terms
from app.skill_matching import SkillMatch, find_all_skill_matches


@dataclass
class RequirementSummary:
    """Per-run counts, returned for logging and asserted on in tests — a
    re-run over an unchanged description must report zeroes for every
    field except `skills_matched`."""

    skills_matched: int = 0
    requirements_written: int = 0
    requirements_removed: int = 0


@dataclass(frozen=True)
class _DesiredRequirement:
    """What one skill's row should contain after this run."""

    skill_id: uuid.UUID
    level: RequirementLevel
    matched_term: str
    excerpt: str
    confidence: Any


def build_desired_requirements(
    description: str, matches: list[SkillMatch]
) -> dict[uuid.UUID, _DesiredRequirement]:
    """Collapse every occurrence of every skill into one row per skill.

    PURE — no database — so the collapsing rules are testable on their
    own.

    A skill mentioned several times gets the STRONGEST level any of its
    occurrences justifies: a posting that calls Python required
    somewhere requires it, however casually it names it elsewhere. The
    excerpt and matched term come from the occurrence that WON, so the
    stored row quotes the text that actually drove the decision rather
    than an unrelated mention.

    Ties within the winning level break on the earliest position, making
    the result stable across reruns — which is what lets the upsert
    below suppress no-op writes.
    """
    by_skill: dict[uuid.UUID, list[tuple[RequirementLevel, SkillMatch]]] = {}
    for match in matches:
        level = classify_at(description, match.start)
        by_skill.setdefault(match.skill_id, []).append((level, match))

    desired: dict[uuid.UUID, _DesiredRequirement] = {}
    for skill_id, occurrences in by_skill.items():
        winning_level = strongest([level for level, _ in occurrences])
        # First occurrence at the winning level — `matches` arrives in
        # document order, so this is deterministic.
        _, winner = next(pair for pair in occurrences if pair[0] is winning_level)
        desired[skill_id] = _DesiredRequirement(
            skill_id=skill_id,
            level=winning_level,
            matched_term=winner.matched_term,
            # The CLAUSE that was judged, not the matcher's line-scoped
            # excerpt — see app/job_requirements/classify.py on why the
            # two differ.
            excerpt=clause_excerpt(description, winner.start),
            confidence=winner.confidence,
        )
    return desired


async def _write_requirement(
    db: AsyncSession, saved_job_id: uuid.UUID, desired: _DesiredRequirement
) -> bool:
    """Upsert one row on (saved_job_id, skill_id). Returns whether
    anything was actually written.

    The `where` clause on the conflict branch is what makes a re-run a
    true no-op rather than merely duplicate-free: without it every run
    would bump `updated_at` on byte-identical rows. Same pattern, for the
    same reason, as app/skill_extraction.py's `_write_evidence`.
    """
    values = {
        "id": uuid.uuid4(),
        "saved_job_id": saved_job_id,
        "skill_id": desired.skill_id,
        "requirement_level": desired.level.value,
        "matched_term": desired.matched_term,
        "excerpt": desired.excerpt,
        "confidence": desired.confidence,
        "extraction_method": RequirementExtractionMethod.JOB_DESCRIPTION_MATCH.value,
    }
    statement = pg_insert(JobSkillRequirement).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="uq_job_skill_requirements_job_skill",
        set_={
            "requirement_level": statement.excluded.requirement_level,
            "matched_term": statement.excluded.matched_term,
            "excerpt": statement.excluded.excerpt,
            "confidence": statement.excluded.confidence,
            "updated_at": func.now(),
        },
        where=(
            JobSkillRequirement.requirement_level.is_distinct_from(
                statement.excluded.requirement_level
            )
            | JobSkillRequirement.matched_term.is_distinct_from(statement.excluded.matched_term)
            | JobSkillRequirement.excerpt.is_distinct_from(statement.excluded.excerpt)
            | JobSkillRequirement.confidence.is_distinct_from(statement.excluded.confidence)
        ),
    )
    # An INSERT always yields a CursorResult at runtime (it has
    # .rowcount); AsyncSession.execute() is only typed as Result[Any].
    result = cast("CursorResult[Any]", await db.execute(statement))
    return bool(result.rowcount)


async def _remove_stale(
    db: AsyncSession, saved_job_id: uuid.UUID, live_skill_ids: set[uuid.UUID]
) -> int:
    """Delete this job's requirements for skills the current description
    no longer supports.

    Scoped to one saved job, so no other job's rows are reachable. An
    empty desired set is a legitimate input — a description edited down
    to mention no taxonomy skill at all should end with no requirements,
    not with the previous run's leftovers.
    """
    statement = delete(JobSkillRequirement).where(JobSkillRequirement.saved_job_id == saved_job_id)
    if live_skill_ids:
        statement = statement.where(JobSkillRequirement.skill_id.notin_(live_skill_ids))
    result = cast("CursorResult[Any]", await db.execute(statement))
    return result.rowcount or 0


async def extract_job_requirements(db: AsyncSession, saved_job: SavedJob) -> RequirementSummary:
    """Reconcile one saved job's requirements against its description.

    Does NOT commit — the caller owns the transaction. That is what lets
    the saved-job routes save the job and its requirements in ONE commit,
    so a job can never be stored with stale or missing requirements.

    Synchronous by design. A description is capped at 60,000 characters
    and the curated taxonomy is 33 skills plus 26 aliases, so this is
    compiled-regex scanning that finishes in milliseconds — nothing like
    the PDF parsing or the 60-request/hour GitHub budget that justified a
    worker elsewhere. A queue here would add a cache with no invalidation
    trigger and a "requirements not ready" UI state, for no benefit.
    """
    summary = RequirementSummary()

    # The curated-only gate, reused unchanged: a skill coined by a user
    # through their profile's target skills has no category and must not
    # become matchable. Prompt 4.2 creates no taxonomy of its own and
    # never invents a skill from job text.
    terms = await load_taxonomy_terms(db)

    # find_ALL_skill_matches, not find_skill_matches: classification
    # needs every occurrence, because one skill can appear in a required
    # clause and a passing mention in the same posting.
    matches = find_all_skill_matches(saved_job.description, terms)
    desired = build_desired_requirements(saved_job.description, matches)
    summary.skills_matched = len(desired)

    for requirement in desired.values():
        if await _write_requirement(db, saved_job.id, requirement):
            summary.requirements_written += 1

    summary.requirements_removed = await _remove_stale(db, saved_job.id, set(desired))
    return summary


async def list_job_requirements(
    db: AsyncSession, saved_job_id: uuid.UUID
) -> list[JobSkillRequirement]:
    """One job's requirements, strongest first then alphabetical by
    skill.

    Ordered on a stable key so two identical requests return identical
    output. The ordering is by the level's STRENGTH, not its name, so
    required sorts above preferred above mentioned rather than
    alphabetically.
    """
    rows = await db.scalars(
        select(JobSkillRequirement).where(JobSkillRequirement.saved_job_id == saved_job_id)
    )
    order = {
        RequirementLevel.REQUIRED.value: 0,
        RequirementLevel.PREFERRED.value: 1,
        RequirementLevel.MENTIONED.value: 2,
    }
    return sorted(rows.all(), key=lambda row: (order.get(row.requirement_level, 99), row.id.hex))
