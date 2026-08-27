"""A job description a candidate has saved (Prompt 4.1)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SavedJob(Base):
    """One job posting a candidate saved, stored for later matching.

    Its own `id` rather than being keyed on the owner (the `profiles` /
    `github_connections` shape): a candidate saves many jobs, so each
    needs its own identity for the id-addressed routes to reference —
    the same reasoning as `resumes`.

    Ownership is `user_id` -> `users.id ON DELETE CASCADE`, referencing
    `users` rather than `profiles` because a profile row is created
    lazily (app/api/v1/profile.py); requiring one here would couple
    saving a job to whether the candidate has opened their profile page.

    `source_url` IS STORED AND NEVER FETCHED. It is metadata the user
    typed so they can find the posting again — CareerLens does not
    request it, follow it, or render it as an embed. That is a security
    boundary, not an unimplemented feature: the value is arbitrary
    user-supplied input, so server-side fetching would turn this column
    into a server-side request forgery vector against whatever the API
    host can reach. Validation (app/schemas/saved_job.py) restricts it
    to http/https, which limits what a *browser* will do with the link
    when a user clicks it; it does not make the URL safe to fetch here.

    `description` HOLDS THE POSTING VERBATIM. Prompt 4.1 deliberately
    stores it unparsed and unnormalized: turning it into skills is
    Prompt 4.2's job, and scoring it against a candidate is Prompt
    4.3's. Nothing in this slice may derive, summarize, or infer from
    this text. `Text` rather than `String(n)` for the same reason as
    `resumes.extracted_text` — a posting has no useful column-level
    length, so the bound is enforced at the API boundary where it can
    produce a readable error instead of a database exception.

    `employment_type` is a closed set validated by
    app.schemas.saved_job.EmploymentType, stored as plain text with no
    database CHECK — the same convention as `resumes.status` and
    `profiles.experience_level`, so extending the vocabulary is a code
    change rather than an ALTER TYPE migration.

    NO SCORE, NO EXTRACTED SKILLS, NO MATCH STATE. Those columns belong
    to later prompts and are deliberately absent here.
    """

    __tablename__ = "saved_jobs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "position",
            name="uq_saved_jobs_user_id_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_saved_jobs_user_id_position", "user_id", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Required: a saved job the candidate cannot identify later is not
    # worth storing, and Prompt 4.3 needs both to label a match.
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # Optional: remote and unspecified postings are ordinary, and
    # inventing "Unknown" would be information we do not have.
    location: Mapped[str | None] = mapped_column(String(200), default=None)
    employment_type: Mapped[str | None] = mapped_column(String(30), default=None)
    # 2048 characters is the practical ceiling browsers and proxies
    # agree on for a URL; anything longer is not a link a user pasted.
    source_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # The candidate's OWN ordering of their list (Prompt 6.3), and the
    # primary job-importance signal `roadmap_priority_v1` reads.
    # Deliberately not derived from `skill_match_v1`: "how well do I
    # match this" is a different question from "which of these do I
    # want", and letting the first answer the second would demote a job
    # the user ranked first because they happen to match it poorly.
    #
    # IT ORDERS, IT DOES NOT LABEL. The rank a user sees is the 1-based
    # index in the sorted response, so a gap left by a deleted job is
    # invisible and no renumbering pass is ever needed. Unique per user
    # and DEFERRABLE — a reorder rewrites a whole list in one
    # transaction and legitimately holds duplicates until COMMIT.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
