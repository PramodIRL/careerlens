import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CandidateSkill(Base):
    """One claim that a particular user has a particular canonical skill.

    The join between `users` and `skills`, promoted to a first-class row
    with its own surrogate `id` rather than a composite-key link table
    (the shape `profile_target_skills` uses). The reason is
    `skill_evidence`: evidence hangs off a candidate skill, and a
    surrogate id lets it do that with a single-column foreign key instead
    of carrying both `user_id` and `skill_id` around.

    `UNIQUE(user_id, skill_id)` is the idempotency key — a candidate has
    at most one row per skill, however many independent sources end up
    supporting it. Multiple supporting observations are represented as
    multiple `skill_evidence` rows against this one row, never as
    duplicate candidate skills.

    Ownership is `user_id` -> `users.id ON DELETE CASCADE`, referencing
    `users` rather than `profiles` for the same reason `resumes` does:
    a profile row is created lazily (app/api/v1/profile.py), so requiring
    one here would couple skill storage to whether the candidate has
    opened their profile page yet.

    `skill_id` uses RESTRICT, not CASCADE: a canonical skill is shared
    vocabulary, and deleting one out from under the candidates who
    reference it should fail loudly rather than silently discard their
    claims.

    Deliberately has no status/confirmed/source columns. The brief's "a
    user can correct or remove extracted skills" is expressible without
    them — removing is deleting this row (which cascades its evidence),
    and correcting is a delete plus an add. Scoring and aggregation
    columns belong to Prompt 4.x, not here.
    """

    __tablename__ = "candidate_skills"
    __table_args__ = (
        UniqueConstraint("user_id", "skill_id", name="uq_candidate_skills_user_skill"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
