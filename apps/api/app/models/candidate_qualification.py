"""Candidate-side qualification facts — CGPA, school marks, degree,
graduation year, experience, work authorization (Prompt 5.1a)."""

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


class CandidateQualification(Base):
    """One qualification fact about one candidate.

    ONE ROW PER FACT, NOT ONE ROW PER CANDIDATE. A wide 1:1 table beside
    `profiles` would be the obvious shape and it is the wrong one: each
    of these facts has its own provenance and its own review state.
    Prompt 5.1b attaches resume-derived evidence and a
    suggested/confirmed/rejected status per fact, which a wide table
    could only express as three parallel columns per field. Row-shaped
    from the start, 5.1b is an additive migration rather than a
    restructuring — and the shape already exists in this codebase, as
    `candidate_skills` + `skill_evidence`.

    PROVENANCE LIVES ON THE ROW, NOT IN A SIDE TABLE (Prompt 5.1b).
    `skill_evidence` is separate from `candidate_skills` because one
    skill genuinely has MANY evidences — a resume line plus four
    distinct GitHub signals. A qualification fact holds exactly ONE
    value, so it has exactly one provenance, and a second table would
    buy a join and an idempotency story for a cardinality that cannot
    arise.

    `status` mirrors `candidate_skills.status` deliberately, including
    its vocabulary: an automatic extractor can now disagree with the
    person the fact is about, which is precisely when a review state
    starts to mean something.

    ABSENCE IS UNKNOWN, NEVER ZERO. A missing row means "we do not
    know", and app/eligibility/resolve.py turns that into UNKNOWN rather
    than a failure. Nothing in this product may default an unstated CGPA
    to 0.0 and then report a candidate as ineligible on that basis.

    `user_id` CASCADES: these facts have no life without the person.
    """

    __tablename__ = "candidate_qualifications"
    __table_args__ = (
        UniqueConstraint("user_id", "fact_type", name="uq_candidate_qualifications_user_fact"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_candidate_qualifications_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Closed set validated by app.schemas.qualification.QualificationFactType,
    # stored as plain text with no database CHECK — the same choice as
    # `resumes.status` and `candidate_skills.status`, so the vocabulary
    # can grow without an ALTER TYPE migration.
    fact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Exactly one of these carries the value, decided by `fact_type`.
    # Numeric rather than Float: a CGPA of 7.5 and a percentage of 82.4
    # are decimal quantities a person reads off a certificate, and
    # binary floating point would make an exact-threshold comparison
    # ("minimum 7.5" against a stored 7.5) a coin toss.
    value_numeric: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), default=None)
    value_text: Mapped[str | None] = mapped_column(String(80), default=None)
    # CGPA only, and NULLABLE ON PURPOSE. 8.2 means nothing without
    # knowing whether the scale is 10 or 4, and this product does not
    # guess: an unstated scale resolves to UNKNOWN rather than inventing
    # comparability. See app/eligibility/resolve.py.
    value_scale: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), default=None)
    # suggested / confirmed / rejected — the SAME vocabulary
    # `candidate_skills.status` uses (app.schemas.skill's
    # CandidateSkillStatus), because it carries the same meaning and the
    # same override invariant. A user edit writes `confirmed`, which is
    # what makes their value distinguishable from an unreviewed
    # extraction; `rejected` is a tombstone that re-extraction must
    # never resurrect. See app/qualifications/extract.py.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="suggested")
    # resume / manual — app.schemas.skill's EvidenceSourceType.
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    # The resume this was read out of. NULL for a value the user typed,
    # which has no document behind it.
    source_identifier: Mapped[str | None] = mapped_column(String(255), default=None)
    # The VERBATIM line the value was read from — never generated prose,
    # and never normalised. A resume that wrote "CGPA: 8.2" keeps that
    # excerpt even though the comparison scale defaults to 10, so the
    # stored evidence says what the document said rather than what the
    # system concluded. NULL for a manually entered value: there is
    # nothing to quote, and inventing a sentence would be worse than an
    # honest absence (the same choice GITHUB_LANGUAGE_MATCH makes).
    excerpt: Mapped[str | None] = mapped_column(String(500), default=None)
    # Closed set validated by
    # app.schemas.qualification.QualificationExtractionMethod.
    extraction_method: Mapped[str | None] = mapped_column(String(40), default=None)
    # How cleanly the pattern matched, NOT how good the qualification
    # is — the same meaning `confidence` carries everywhere else in this
    # codebase.
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
