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


class SkillEvidence(Base):
    """Why we believe a candidate has a skill — one inspectable record
    supporting one CandidateSkill.

    This table is what makes docs/project-brief.md's Evidence-First rule
    enforceable rather than aspirational: a skill shown to a user must be
    traceable to stored rows here, and an LLM may later *explain* these
    rows but must never be what produced them.

    Ownership is derived, never stored. There is deliberately no
    `user_id` column: the owner is reached only through
    `candidate_skill_id -> candidate_skills.user_id`. A denormalized copy
    could drift out of agreement with its parent, and the join is cheap.
    API-layer checks in later prompts follow the existing pattern from
    app/api/v1/resume.py's `_get_owned_resume` — load, compare against
    `current_user.id`, 404 for missing and 403 for someone else's.
    `ON DELETE CASCADE` means removing a candidate skill removes its
    evidence, and removing a user cascades through both levels.

    `source_identifier` is a polymorphic string, not a foreign key — no
    single FK can point at three different source kinds. Its meaning
    depends on `source_type` (app.schemas.skill.EvidenceSourceType):

        resume  -> the `resumes.id` UUID, as a string
        github  -> "owner/repo"                        (Prompt 3.x)
        manual  -> the id of the user who asserted it

    The cost of that polymorphism is the absence of referential
    integrity: deleting a resume (Prompt 2.1's delete endpoint) leaves
    evidence citing an id that no longer resolves. Accepted for now and
    documented in docs/decisions.md — evidence is a historical record,
    and nothing in Prompt 2.3 writes any.

    `excerpt` holds a short verbatim span from the source that a human
    can read as justification, e.g. "Built a REST API with FastAPI".
    It is nullable because some evidence has no quotable text at all (a
    GitHub language statistic, a manual assertion), and forcing a value
    would invite fabricated filler — precisely what the Evidence-First
    rule prohibits. It must never contain: the full extracted resume text
    (that already lives in `resumes.extracted_text`), a `storage_key` or
    any filesystem path (Prompt 2.1 deliberately keeps those out of
    responses), credentials that happen to appear in a document, or an
    LLM-written paraphrase. An excerpt is quoted, never authored.

    `UNIQUE(candidate_skill_id, source_type, source_identifier,
    extraction_method)` is the idempotency key that lets Prompt 2.4
    re-run extraction over the same resume without accumulating
    duplicates. It means one representative excerpt per
    candidate-skill/source/method: if "Python" appears five times in one
    resume, that is one evidence row, not five.

    That is sufficient for the MVP because the excerpt's job is to let a
    human verify the claim, and one clear quotation from a document does
    that as well as five near-identical ones — while every question the
    MVP actually asks ("which sources support this skill?", "how
    confident are we?", "show me why") is answered per source, not per
    mention. Counting mentions is a scoring concern, and scoring belongs
    to Prompt 4.x.

    If per-mention granularity is ever needed, the schema change is
    additive and contained: add an `excerpt_hash` (or an explicit
    `occurrence_index`) column and extend this unique constraint to
    include it. That is a new migration plus an updated write path in the
    extractor — no change to ownership, cascades, or any other table.
    """

    __tablename__ = "skill_evidence"
    __table_args__ = (
        # A value range is a permanent mathematical invariant, so unlike
        # the closed-set *vocabularies* in app/schemas/skill.py (which are
        # expected to grow and so stay out of the database), this one is
        # worth enforcing in Postgres.
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_skill_evidence_confidence_range"
        ),
        UniqueConstraint(
            "candidate_skill_id",
            "source_type",
            "source_identifier",
            "extraction_method",
            name="uq_skill_evidence_natural_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    candidate_skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_skills.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Closed sets validated by app.schemas.skill's EvidenceSourceType /
    # ExtractionMethod, stored as plain text — see that module on why the
    # vocabularies stay out of the database.
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    excerpt: Mapped[str | None] = mapped_column(String(500), default=None)
    extraction_method: Mapped[str] = mapped_column(String(40), nullable=False)
    # Numeric, not float: binary floats cannot represent values like 0.7
    # exactly, which would make score comparisons and test assertions
    # non-deterministic once Prompt 4.x aggregates these.
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
