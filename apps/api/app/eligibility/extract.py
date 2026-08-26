"""Persists deterministic eligibility extraction into
`job_eligibility_requirements` (Prompt 5.1a).

Splits from app/eligibility/classify.py the same way
app/job_requirements/extract.py splits from its classifier: that module
decides WHAT the description states, this one decides what to WRITE.

DESIRED-STATE, NOT APPEND-ONLY, exactly as on the skill side. Every run
computes the complete set of bars the current description justifies and
makes the table match it — upsert what belongs, delete what no longer
does. Editing

    "Minimum CGPA 7.5. B.Tech required."
 -> "Minimum CGPA 7.0."

lowers the CGPA row IN PLACE (same id, same created_at) and removes the
degree row, rather than leaving a stale requirement that would quietly
keep reporting a candidate as ineligible.

THE BOUNDARY THIS MODULE MUST NOT CROSS. It writes to exactly two
tables, both keyed to a saved job. No candidate facts, no skill
requirements, no scores, no `skill_match_v1` inputs. Whether a person
clears these bars is derived on read by app/eligibility/resolve.py and
is never stored.
"""

import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.eligibility.classify import ParsedRequirement, parse_eligibility_requirements, strictest
from app.models.job_eligibility import JobEligibilityRequirement, JobEligibilityRequirementValue
from app.models.saved_job import SavedJob
from app.schemas.eligibility import EligibilityExtractionMethod, EligibilityRequirementType


@dataclass
class EligibilitySummary:
    """Per-run counts, returned for logging and asserted on in tests — a
    re-run over an unchanged description must report zeroes for every
    field except `requirements_matched`."""

    requirements_matched: int = 0
    requirements_written: int = 0
    requirements_removed: int = 0


def build_desired_requirements(description: str) -> dict[str, ParsedRequirement]:
    """Collapse every statement into one row per requirement type.

    PURE — no database — so the collapsing rule is testable on its own.
    A posting that names a CGPA floor twice gets the STRICTER of the
    two; ties break on the earliest position, which is what makes a
    re-run over unchanged text produce a byte-identical row and
    therefore write nothing.
    """
    by_type: dict[str, list[ParsedRequirement]] = {}
    for parsed in parse_eligibility_requirements(description):
        by_type.setdefault(parsed.requirement_type.value, []).append(parsed)
    return {key: strictest(group) for key, group in by_type.items()}


async def _write_requirement(
    db: AsyncSession, saved_job_id: uuid.UUID, desired: ParsedRequirement
) -> tuple[uuid.UUID, bool]:
    """Upsert one row on (saved_job_id, requirement_type). Returns the
    row id and whether anything was actually written.

    The `where` clause on the conflict branch is what makes a re-run a
    true no-op rather than merely duplicate-free: without it every run
    would bump `updated_at` on byte-identical rows. Same pattern, for
    the same reason, as app/job_requirements/extract.py.
    """
    values = {
        "id": uuid.uuid4(),
        "saved_job_id": saved_job_id,
        "requirement_type": desired.requirement_type.value,
        "comparator": desired.comparator.value,
        "numeric_value": desired.numeric_value,
        "numeric_max": desired.numeric_max,
        "value_scale": desired.value_scale,
        "requirement_level": desired.requirement_level,
        "open_ended": desired.open_ended,
        "matched_term": desired.matched_term,
        "excerpt": desired.excerpt,
        "confidence": desired.confidence,
        "extraction_method": EligibilityExtractionMethod.JOB_DESCRIPTION_MATCH.value,
    }
    statement = pg_insert(JobEligibilityRequirement).values(**values)
    tracked = (
        "comparator",
        "numeric_value",
        "numeric_max",
        "value_scale",
        "requirement_level",
        "open_ended",
        "matched_term",
        "excerpt",
        "confidence",
    )
    changed = None
    for column in tracked:
        clause = getattr(JobEligibilityRequirement, column).is_distinct_from(
            getattr(statement.excluded, column)
        )
        changed = clause if changed is None else (changed | clause)

    upsert = statement.on_conflict_do_update(
        constraint="uq_job_eligibility_requirements_job_type",
        set_={column: getattr(statement.excluded, column) for column in tracked},
        where=changed,
    ).returning(JobEligibilityRequirement.id)

    written_id = await db.scalar(upsert)
    if written_id is not None:
        return written_id, True

    # The upsert was suppressed because nothing changed, so RETURNING
    # yielded no row — look the existing id up to reconcile its values.
    existing = await db.scalar(
        select(JobEligibilityRequirement.id).where(
            JobEligibilityRequirement.saved_job_id == saved_job_id,
            JobEligibilityRequirement.requirement_type == desired.requirement_type.value,
        )
    )
    assert existing is not None, "suppressed upsert with no existing row"
    return existing, False


async def _sync_values(
    db: AsyncSession, requirement_id: uuid.UUID, desired: tuple[str, ...]
) -> bool:
    """Make the accepted-value rows match `desired`. Returns whether
    anything changed.

    Compared as a SET before writing, rather than delete-then-insert
    every run. Order carries no meaning here — these are alternatives,
    not a ranking — so an unchanged set must produce no writes at all,
    which is what keeps the whole extraction idempotent.
    """
    current = set(
        (
            await db.scalars(
                select(JobEligibilityRequirementValue.value).where(
                    JobEligibilityRequirementValue.requirement_id == requirement_id
                )
            )
        ).all()
    )
    wanted = set(desired)
    if current == wanted:
        return False

    await db.execute(
        delete(JobEligibilityRequirementValue).where(
            JobEligibilityRequirementValue.requirement_id == requirement_id
        )
    )
    for value in sorted(wanted):
        db.add(JobEligibilityRequirementValue(requirement_id=requirement_id, value=value))
    return True


async def _remove_stale(db: AsyncSession, saved_job_id: uuid.UUID, live_types: set[str]) -> int:
    """Delete this job's requirements the current description no longer
    supports.

    Scoped to one saved job, so no other job's rows are reachable. An
    empty desired set is a legitimate input — a description edited down
    to state no recognisable bar should end with no requirements, not
    with the previous run's leftovers. Child value rows go with them by
    ON DELETE CASCADE.
    """
    statement = delete(JobEligibilityRequirement).where(
        JobEligibilityRequirement.saved_job_id == saved_job_id
    )
    if live_types:
        statement = statement.where(JobEligibilityRequirement.requirement_type.notin_(live_types))
    result = cast("CursorResult[Any]", await db.execute(statement))
    return result.rowcount or 0


async def extract_job_eligibility(db: AsyncSession, saved_job: SavedJob) -> EligibilitySummary:
    """Reconcile one saved job's eligibility bars against its description.

    Does NOT commit — the caller owns the transaction, which is what
    lets the saved-job routes write the job, its skill requirements and
    its eligibility requirements in ONE commit. A job can never be
    stored with one of the three stale.

    Synchronous by design, the same call the skill extractor already
    makes from the same routes: a description is capped at 60,000
    characters and this is compiled-regex scanning that finishes in
    milliseconds. A queue here would buy a "requirements not ready" UI
    state and a cache with no invalidation trigger, for nothing.
    """
    summary = EligibilitySummary()
    desired = build_desired_requirements(saved_job.description)
    summary.requirements_matched = len(desired)

    for requirement in desired.values():
        requirement_id, wrote = await _write_requirement(db, saved_job.id, requirement)
        values_changed = await _sync_values(db, requirement_id, requirement.accepted_values)
        if wrote or values_changed:
            summary.requirements_written += 1

    summary.requirements_removed = await _remove_stale(db, saved_job.id, set(desired))
    return summary


async def list_job_eligibility_requirements(
    db: AsyncSession, saved_job_id: uuid.UUID
) -> list[tuple[JobEligibilityRequirement, list[str]]]:
    """One job's eligibility requirements with their accepted values,
    ordered for stable output.

    TWO queries regardless of how many requirements there are — the
    values are fetched for all of them at once rather than per row. The
    ordering is by requirement type so two identical requests return
    byte-identical JSON without the caller sorting anything.
    """
    rows = list(
        (
            await db.scalars(
                select(JobEligibilityRequirement)
                .where(JobEligibilityRequirement.saved_job_id == saved_job_id)
                .order_by(JobEligibilityRequirement.requirement_type)
            )
        ).all()
    )
    if not rows:
        return []

    grouped: dict[uuid.UUID, list[str]] = {}
    value_rows = await db.execute(
        select(
            JobEligibilityRequirementValue.requirement_id,
            JobEligibilityRequirementValue.value,
        )
        .where(JobEligibilityRequirementValue.requirement_id.in_([row.id for row in rows]))
        .order_by(JobEligibilityRequirementValue.value)
    )
    for requirement_id, value in value_rows:
        grouped.setdefault(requirement_id, []).append(value)

    return [(row, grouped.get(row.id, [])) for row in rows]


def requirement_type_of(row: JobEligibilityRequirement) -> EligibilityRequirementType | None:
    """The row's type as an enum, or None when this deployment does not
    know it.

    Stored as plain text so the vocabulary can grow; a row written by a
    newer version is skipped rather than raising, so an eligibility
    request never 500s on data it does not recognise.
    """
    try:
        return EligibilityRequirementType(row.requirement_type)
    except ValueError:
        return None
