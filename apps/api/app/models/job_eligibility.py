"""Job-side eligibility requirements — the qualification bar a posting
sets, as distinct from the skills it asks for (Prompt 5.1a)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class JobEligibilityRequirement(Base):
    """One qualification threshold a saved job states.

    DELIBERATELY NOT `job_skill_requirements`. That table answers "which
    canonical SKILL does this posting want, and how strongly" — every
    row points at `skills.id` and is scored by `skill_match_v1`. A CGPA
    floor is not a skill: it has no taxonomy entry, no evidence trail
    through resumes or repositories, and no meaningful `requirement_level`
    weight. Forcing it in would have meant a nullable `skill_id`, a
    second meaning for `requirement_level`, and a matcher that has to
    branch on which kind of row it is holding — which is exactly how a
    clean domain becomes an opaque one.

    `saved_job_id` CASCADES, and there is no `user_id` column, for the
    same reason as `job_skill_requirements`: the only route to these
    rows is through a saved job whose owner is already checked, and a
    denormalized owner copy could drift from its parent while a join
    cannot.

    `UNIQUE(saved_job_id, requirement_type)` is the reconciliation key.
    One row per job per fact type, however many times the description
    states it — repeated statements collapse to the strictest before
    anything is written (app/eligibility/classify.py).

    NO CANDIDATE REFERENCE AND NO RESULT. Whether a particular person
    clears this bar is derived on read by app/eligibility/resolve.py and
    never stored — the same choice `/match` and `/gaps` already make,
    and for the same reason: a stored verdict is a cache whose
    invalidation trigger (the candidate editing their own facts) lives
    somewhere else entirely.
    """

    __tablename__ = "job_eligibility_requirements"
    __table_args__ = (
        UniqueConstraint(
            "saved_job_id", "requirement_type", name="uq_job_eligibility_requirements_job_type"
        ),
        # A value range is a permanent mathematical invariant, so it is
        # worth enforcing in Postgres — the same reasoning as
        # job_skill_requirements.confidence.
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_job_eligibility_requirements_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    saved_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("saved_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The SAME vocabulary the candidate side uses for `fact_type`, so
    # resolution is a lookup rather than a translation table. Closed set
    # validated by app.schemas.eligibility.EligibilityRequirementType.
    requirement_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # gte / lte / eq / in / between — app.schemas.eligibility.Comparator.
    comparator: Mapped[str] = mapped_column(String(10), nullable=False)
    # Set for numeric requirements; NULL for categorical ones, whose
    # accepted values live in the child table below.
    numeric_value: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), default=None)
    # The upper bound of a BETWEEN, and only that.
    numeric_max: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), default=None)
    # CGPA only. NULL means the posting stated a number without saying
    # which scale it is on ("minimum CGPA 7.5"), which this product
    # treats as UNDETERMINED rather than assuming 10. See
    # app/eligibility/classify.py.
    value_scale: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), default=None)
    # required / preferred, reusing app.job_requirements.classify's
    # vocabulary rather than a second one. Only `required` requirements
    # can make a candidate ineligible.
    requirement_level: Mapped[str] = mapped_column(String(20), nullable=False)
    # True when the posting hedged the list open — "Computer Science or
    # a related field". A value outside the list then resolves to
    # UNDETERMINED, not NOT_SATISFIED: "related" has no deterministic
    # membership test, and guessing one would invent a rejection.
    open_ended: Mapped[bool] = mapped_column(nullable=False, default=False)
    # WHICH text matched, so a stored row can explain itself.
    matched_term: Mapped[str] = mapped_column(String(80), nullable=False)
    # The verbatim clause that drove the parse. Never generated prose —
    # a real slice of saved_jobs.description.
    excerpt: Mapped[str] = mapped_column(String(500), nullable=False)
    # How cleanly the pattern matched, NOT how strongly the posting
    # wants it — that is `requirement_level`, exactly as on the skill
    # side.
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    # Closed set validated by
    # app.schemas.eligibility.EligibilityExtractionMethod.
    extraction_method: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class JobEligibilityRequirementValue(Base):
    """One accepted value of a categorical eligibility requirement.

    A child table rather than a comma-separated column on the parent:
    "does this candidate's degree appear in the accepted list" is a
    membership question, and 1NF is what keeps it a comparison instead
    of a substring search — the same reasoning `profile_target_roles`
    already applies.

    Values are stored NORMALIZED (`btech`, `computer_science`), never as
    the posting spelled them. The spelling that matched is preserved on
    the parent's `matched_term`, so nothing about the original text is
    lost.
    """

    __tablename__ = "job_eligibility_requirement_values"

    requirement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_eligibility_requirements.id", ondelete="CASCADE"), primary_key=True
    )
    value: Mapped[str] = mapped_column(String(80), primary_key=True)
