"""Reading and writing a candidate's declared qualification facts
(Prompt 5.1a).

THE MAPPING LAYER between the flat shape the API speaks and the
one-row-per-fact shape the database keeps
(app/models/candidate_qualification.py). Kept here rather than in the
router so both the qualifications endpoint and the eligibility endpoint
read facts through one code path, and so the flat-to-rows translation is
testable without HTTP.

UNKNOWN IS THE ABSENCE OF A ROW. Clearing a fact DELETES its row rather
than nulling its value, so "unknown" has exactly one representation
throughout the system and `resolve_eligibility` can decide it with a
plain dictionary lookup. A row holding NULL would be a second way to
spell the same thing, and the two would eventually disagree.

NOTHING HERE INFERS ANYTHING. Every value written through this module
is one the candidate typed, which is why a write also stamps
`status = confirmed`. That stamp is load-bearing: it is what makes a
user's value distinguishable from an unreviewed extraction, and
therefore what stops app/qualifications/extract.py from refreshing it
on the next resume run. Resume-derived facts arrive through that module
instead, as `suggested`.

SAVING THE FORM CLAIMS EVERY VALUE IN IT. This module previously left
an unchanged field alone, so a single correction could not freeze facts
a later resume run would otherwise refresh. That reason is gone — the
resume hook is removed and manual entry is the only source — so a form
submit now means "these are my values". It is also how a legacy
`suggested` row from the old extractor becomes authoritative: one save,
rather than making the user retype what is already on screen.
"""

import uuid
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.eligibility.resolve import CandidateFact
from app.models.candidate_qualification import CandidateQualification
from app.schemas.eligibility import EligibilityRequirementType
from app.schemas.qualification import (
    CATEGORICAL_FACTS,
    NUMERIC_FACTS,
    QualificationExtractionMethod,
    QualificationFactDetail,
    QualificationFactType,
    QualificationsResponse,
    QualificationsUpdateRequest,
)
from app.schemas.skill import CandidateSkillStatus, EvidenceSourceType

# The flat request/response field for each stored fact. `cgpa_scale` is
# deliberately absent: it is not a fact of its own, it is a property of
# the CGPA fact and lives on that row.
_FIELD_BY_FACT: dict[str, str] = {
    QualificationFactType.CGPA.value: "cgpa",
    QualificationFactType.CLASS_10_PERCENTAGE.value: "class_10_percentage",
    QualificationFactType.CLASS_12_PERCENTAGE.value: "class_12_percentage",
    QualificationFactType.HIGHEST_DEGREE.value: "highest_degree",
    QualificationFactType.FIELD_OF_STUDY.value: "field_of_study",
    QualificationFactType.GRADUATION_YEAR.value: "graduation_year",
    QualificationFactType.YEARS_EXPERIENCE.value: "years_experience",
}
_FACT_BY_FIELD = {field: fact for fact, field in _FIELD_BY_FACT.items()}

CGPA = QualificationFactType.CGPA.value


class QualificationConflictError(ValueError):
    """The requested combination of CGPA fields cannot be stored.

    Rejected rather than silently repaired: a client sending a scale
    believes it is storing something, and quietly discarding it would
    show the user a form that forgets what they typed. Raised BEFORE
    anything is mutated, so a refused request leaves the row untouched.
    """


async def load_rows(db: AsyncSession, user_id: uuid.UUID) -> dict[str, CandidateQualification]:
    """Every declared fact for one candidate, keyed by fact type.

    ONE query. `user_id` comes from the caller (always the JWT subject),
    and every row read is filtered by it.
    """
    rows = await db.scalars(
        select(CandidateQualification).where(CandidateQualification.user_id == user_id)
    )
    return {row.fact_type: row for row in rows.all()}


def to_response(rows: dict[str, CandidateQualification]) -> QualificationsResponse:
    """The flat view, plus per-fact provenance.

    Anything with no row is `null` — never a default. A REJECTED fact is
    reported as null too: the user has said it is not theirs, so the
    value must not surface as if it still stood. The row itself stays,
    because deleting it would let the next resume run cheerfully
    re-suggest exactly what they turned down.
    """
    payload: dict[str, object] = {}
    facts: dict[str, QualificationFactDetail] = {}
    for fact_type, row in rows.items():
        field = _FIELD_BY_FACT.get(fact_type)
        if field is None:  # pragma: no cover - defensive
            continue
        facts[fact_type] = QualificationFactDetail.model_validate(row)
        if row.status == CandidateSkillStatus.REJECTED.value:
            continue
        payload[field] = row.value_numeric if fact_type in NUMERIC_FACTS else row.value_text
    payload["facts"] = facts

    cgpa_row = rows.get(CGPA)
    if cgpa_row is not None and cgpa_row.status != CandidateSkillStatus.REJECTED.value:
        payload["cgpa_scale"] = cgpa_row.value_scale

    # graduation_year is stored Numeric like every other number, but it
    # is an integer to every reader — surface it as one rather than
    # making clients strip a "2026.00".
    year = payload.get("graduation_year")
    if isinstance(year, Decimal):
        payload["graduation_year"] = int(year)

    updated = [row.updated_at for row in rows.values()]
    payload["updated_at"] = max(updated) if updated else None
    return QualificationsResponse.model_validate(payload)


async def apply_update(
    db: AsyncSession,
    user_id: uuid.UUID,
    body: QualificationsUpdateRequest,
    rows: dict[str, CandidateQualification],
) -> None:
    """Apply a partial update. Does NOT commit — the caller owns the
    transaction.

    `exclude_unset` is the whole point of a PATCH: an omitted field
    means "leave unchanged", an explicit `null` clears it back to
    unknown. Same contract as the profile PATCH.
    """
    updates = body.model_dump(exclude_unset=True)

    # The CGPA value and its scale are two fields of ONE row, so they
    # are resolved together before anything is written. Handling them
    # independently is how a scale ends up orphaned on a cleared value.
    scale_touched = "cgpa_scale" in updates
    value_touched = "cgpa" in updates
    if scale_touched or value_touched:
        existing = rows.get(CGPA)
        # An untouched half keeps whatever is already stored, so
        # PATCHing only the scale does not wipe the value.
        value = (
            updates.get("cgpa") if value_touched else (existing.value_numeric if existing else None)
        )
        scale = (
            updates.get("cgpa_scale")
            if scale_touched
            else (existing.value_scale if existing else None)
        )

        # Validated BEFORE anything is mutated, so a refused request
        # leaves the stored row exactly as it was.
        if value is None and scale is not None and scale_touched:
            # Setting a scale for a CGPA that does not exist. Clearing
            # the CGPA while an old scale is still stored is NOT this
            # case — that just deletes the row below, scale and all.
            raise QualificationConflictError("cgpa_scale requires a cgpa value")
        if value is not None and scale is not None and value > scale:
            raise QualificationConflictError("cgpa cannot exceed cgpa_scale")

        if value is None:
            # Clearing the CGPA takes its scale with it — a scale on
            # its own describes nothing.
            if existing is not None:
                await db.delete(existing)
                rows.pop(CGPA, None)
        else:
            if existing is not None and (
                existing.value_numeric == value and existing.value_scale == scale
            ):
                # Resubmitted unchanged. Leave the row exactly as it is,
                # so a resume-derived CGPA stays refreshable.
                pass
            else:
                if existing is None:
                    existing = CandidateQualification(user_id=user_id, fact_type=CGPA)
                    db.add(existing)
                    rows[CGPA] = existing
                existing.value_numeric = value
                existing.value_scale = scale
                _stamp_manual(existing)

    for field, value in updates.items():
        if field in {"cgpa", "cgpa_scale"}:
            continue
        fact_type = _FACT_BY_FIELD.get(field)
        if fact_type is None:  # pragma: no cover - defensive
            continue
        existing = rows.get(fact_type)
        if value is None:
            if existing is not None:
                await db.delete(existing)
                rows.pop(fact_type, None)
            continue
        if existing is None:
            existing = CandidateQualification(user_id=user_id, fact_type=fact_type)
            db.add(existing)
            rows[fact_type] = existing
        if fact_type in NUMERIC_FACTS:
            existing.value_numeric = Decimal(str(value))
            existing.value_text = None
        else:
            # StrEnum members serialise through model_dump as their
            # value already; str() keeps this honest for either.
            existing.value_text = str(value)
            existing.value_numeric = None
        _stamp_manual(existing)


def _stamp_manual(row: CandidateQualification) -> None:
    """Mark a row as the user's own word.

    CONFIRMED, and that is what makes the value usable: eligibility
    reads confirmed rows and nothing else, so a fact only counts once
    the candidate has actually asserted it. Any resume provenance is
    cleared with it — the row no longer describes what a document said,
    it describes what the person said.
    """
    row.status = CandidateSkillStatus.CONFIRMED.value
    row.source_type = EvidenceSourceType.MANUAL.value
    row.source_identifier = None
    row.excerpt = None
    row.extraction_method = QualificationExtractionMethod.MANUAL_ENTRY.value
    row.confidence = None


async def reject_fact(
    db: AsyncSession, user_id: uuid.UUID, fact_type: str
) -> CandidateQualification | None:
    """Record that an extracted fact is not the candidate's.

    A TOMBSTONE, NOT A DELETE — the same design as a rejected candidate
    skill, and for the same reason: deleting the row would let the next
    extraction run faithfully re-suggest it and silently discard the
    user's decision. The value is kept so the UI can still show what was
    turned down.
    """
    row = (
        await db.scalars(
            select(CandidateQualification).where(
                CandidateQualification.user_id == user_id,
                CandidateQualification.fact_type == fact_type,
            )
        )
    ).first()
    if row is None:
        return None
    row.status = CandidateSkillStatus.REJECTED.value
    return row


def to_candidate_facts(
    rows: dict[str, CandidateQualification],
) -> dict[EligibilityRequirementType, CandidateFact]:
    """The shape app/eligibility/resolve.py consumes.

    A fact type this deployment does not recognise is skipped rather
    than raising — the vocabulary is stored as plain text so it can
    grow, and an eligibility request must not 500 on a row written by a
    newer version.
    """
    facts: dict[EligibilityRequirementType, CandidateFact] = {}
    for fact_type, row in rows.items():
        if row.status != CandidateSkillStatus.CONFIRMED.value:
            # ONLY THE USER'S OWN WORD COUNTS. A `suggested` row is a
            # machine's reading nobody has accepted, and `rejected` is
            # one they turned down — both read as UNKNOWN here, never as
            # a value that stands. This is what makes the profile
            # authoritative: an eligibility verdict is only ever
            # computed from facts the candidate actually asserted.
            continue
        try:
            key = EligibilityRequirementType(fact_type)
        except ValueError:
            continue
        facts[key] = CandidateFact(
            value_numeric=row.value_numeric if fact_type in NUMERIC_FACTS else None,
            value_text=row.value_text if fact_type in CATEGORICAL_FACTS else None,
            value_scale=row.value_scale,
        )
    return facts


def has_profile(rows: dict[str, CandidateQualification]) -> bool:
    """Whether the candidate has set up a qualification profile at all.

    Counts only CONFIRMED facts, matching what eligibility actually
    uses. Someone holding nothing but unaccepted `suggested` rows has
    not set up a profile — telling them otherwise would send them
    looking for a form they have never filled in.
    """
    return any(row.status == CandidateSkillStatus.CONFIRMED.value for row in rows.values())


async def delete_all(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Clear every declared fact for one candidate. Scoped to that user,
    so no other candidate's rows are reachable."""
    await db.execute(
        delete(CandidateQualification).where(CandidateQualification.user_id == user_id)
    )
