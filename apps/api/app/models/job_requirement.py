"""Job-side skill requirements derived from a saved job description
(Prompt 4.2)."""

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


class JobSkillRequirement(Base):
    """One canonical skill a saved job asks for, and how strongly.

    DELIBERATELY NOT `skill_evidence`. That table hangs off
    `candidate_skills` and answers "why do we believe this PERSON has
    this skill". This answers "what does this POSTING ask for" — a
    different owner, a different lifecycle, and a `requirement_level`
    that would be meaningless on candidate evidence. Reusing it would
    have meant a nullable `candidate_skill_id` and two unrelated
    meanings in one table, corrupting the Evidence-First model that
    docs/project-brief.md rests on.

    `saved_job_id` CASCADES: a requirement is derived data with no life
    of its own, so deleting the job deletes it. That is also how
    ownership works here — there is no `user_id` column, because the
    only route to these rows is through a saved job whose owner is
    already checked (app/api/v1/saved_job.py). A denormalized owner copy
    could drift from its parent; the join cannot.

    `skill_id` uses RESTRICT, matching `candidate_skills`: a canonical
    skill is shared vocabulary, and deleting one out from under the rows
    referencing it should fail loudly rather than silently discard them.

    `UNIQUE(saved_job_id, skill_id)` is the reconciliation key. ONE ROW
    PER JOB AND SKILL, however many times the description mentions it —
    repeated occurrences are collapsed by the strongest-level rule
    (app/job_requirements/classify.py) before anything is written.

    `excerpt` is NOT NULL, unlike `skill_evidence.excerpt`. Every job
    requirement comes from readable text by construction, so there is no
    "nothing to quote" case here — the absence that made the evidence
    column nullable (a GitHub language statistic, a manual assertion)
    cannot arise. It holds the CLAUSE that drove the classification,
    verbatim from the description, so the stored row shows exactly the
    text that was judged.

    NO SCORE, NO MATCH STATE, NO CANDIDATE REFERENCE. Comparing these
    requirements against a candidate is Prompt 4.3's business; storing
    anything about a candidate here would pre-empt it.
    """

    __tablename__ = "job_skill_requirements"
    __table_args__ = (
        UniqueConstraint("saved_job_id", "skill_id", name="uq_job_skill_requirements_job_skill"),
        # A value range is a permanent mathematical invariant, so unlike
        # the closed-set vocabularies in app/schemas (which are expected
        # to grow), this one is worth enforcing in Postgres — the same
        # reasoning as skill_evidence.confidence.
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_job_skill_requirements_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    saved_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("saved_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    # required / preferred / mentioned — a closed set validated by
    # app.job_requirements.classify.RequirementLevel, stored as plain
    # text with no database CHECK, matching `resumes.status` and
    # `candidate_skills.status`: a vocabulary should grow without an
    # ALTER TYPE migration.
    requirement_level: Mapped[str] = mapped_column(String(20), nullable=False)
    # WHICH spelling matched — "py" versus "Python" is the difference
    # between explaining a match and merely asserting it.
    matched_term: Mapped[str] = mapped_column(String(80), nullable=False)
    # The verbatim clause that drove the classification. Never generated
    # prose, never a summary — a real slice of saved_jobs.description.
    excerpt: Mapped[str] = mapped_column(String(500), nullable=False)
    # Straight from the existing matcher's CONFIDENCE_BY_KIND, unchanged.
    # It answers "does this string denote this skill", NOT "how strongly
    # is it required" — that is what requirement_level is for.
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    # Closed set validated by app.schemas.saved_job.RequirementExtractionMethod.
    extraction_method: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
