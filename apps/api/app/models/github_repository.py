"""Normalized public GitHub repository data (Prompt 3.2)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GitHubRepository(Base):
    """One public repository belonging to a user's connected account.

    IDENTITY IS `github_repo_id`, NEVER `full_name`. A GitHub repository
    can be renamed or transferred, which changes "owner/repo" while the
    numeric id stays put. Upserting on the name would therefore create a
    second row for the same repository on the next run — silently
    doubling a candidate's evidence in Prompt 3.3. `UNIQUE(user_id,
    github_repo_id)` is what makes reruns idempotent.

    Scoped per user rather than globally unique on `github_repo_id`,
    deliberately: Prompt 3.1 lets two users connect the same public
    account (it verifies existence, never ownership), so each gets their
    own rows. A global unique would silently re-break that decision.

    SOFT DELETE, not removal. When a repository disappears from a
    COMPLETE listing, `deleted_at` is set and the row stays. Prompt 3.3
    will cite these repositories as evidence, and hard-deleting would
    make a skill lose its support with nothing left to explain why. A
    repository that reappears (unarchived, or made public again) has
    `deleted_at` cleared.

    WHAT IS DELIBERATELY NOT STORED: no raw GitHub JSON payload, no
    owner object (avatar, email, profile URLs), no clone/git/ssh URLs,
    no default branch, no watcher or subscriber counts, no license text.
    The README is the ONLY retained raw source snapshot in this
    product — see the readme_* columns below and docs/decisions.md.
    """

    __tablename__ = "github_repositories"
    __table_args__ = (
        UniqueConstraint("user_id", "github_repo_id", name="uq_github_repositories_user_repo"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # GitHub's immutable numeric repository id. BigInteger because it is
    # GitHub's key space, not ours.
    github_repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # "owner/repo" — the display value, and exactly what
    # app/models/skill_evidence.py reserves as `source_identifier` for
    # source_type='github'. Kept in sync on every run, so a rename is
    # reflected rather than duplicated.
    full_name: Mapped[str] = mapped_column(String(400), nullable=False)
    # The repository's own words. Prompt 3.3 matches this against the
    # taxonomy the same way it matches resume text.
    description: Mapped[str | None] = mapped_column(String(1000), default=None)
    # A fork is not evidence of the candidate's own work. A fork still
    # gets a base row — that data is already in the listing payload and
    # costs no extra request — but it is excluded from DETAIL fetching
    # (languages + README), so it never spends a request against the
    # 60/hour budget, and it does not count against the per-run cap.
    # The flag is also what lets Prompt 3.3 decline to credit one.
    is_fork: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # GitHub's single "primary language" guess. The full byte breakdown
    # lives in github_repository_languages.
    primary_language: Mapped[str | None] = mapped_column(String(100), default=None)
    # Recorded as facts, NOT combined into any score. Ranking and
    # weighting are Prompt 4.x's business; storing a number is not the
    # same as deciding what it is worth.
    stargazers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    forks_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # "recent update timestamps" — GitHub's own, distinct from this
    # row's created_at/updated_at, which describe OUR record.
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    github_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    github_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    # --- The one retained raw source snapshot -----------------------
    # Prompt 3.3 must quote a verbatim excerpt as evidence, and a hash
    # cannot be quoted, so the text itself is kept — truncated to
    # GITHUB_README_MAX_CHARS. `readme_sha` is GitHub's blob SHA: it
    # makes the snapshot traceable and lets a rerun skip re-storing
    # unchanged content. `readme_byte_size` is the size of the ORIGINAL
    # content, so a truncated record still says how much there really
    # was, and `readme_truncated` means nothing is silently lossy.
    readme_text: Mapped[str | None] = mapped_column(Text, default=None)
    readme_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    readme_byte_size: Mapped[int | None] = mapped_column(Integer, default=None)
    readme_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # When languages + README were last fetched for this repository.
    #
    # This is the resume marker for a paused run, and it has to be its
    # own column rather than being inferred from `updated_at`: base rows
    # are upserted for EVERY listed repository (that data is already in
    # the listing and costs no extra request), so `updated_at` moves for
    # repositories whose detail was never fetched. Comparing this against
    # the run's `started_at` is what lets a rate-limited run resume
    # without re-spending requests on repositories it already handled.
    #
    # Set on a per-repository FAILURE too, not only on success: the
    # attempt happened and was counted, and an ordinary per-repository
    # failure is deliberately not auto-retried within the same run.
    # Null means "base data only" — either a fork, or beyond the
    # per-run cap.
    detail_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # When this repository was last present in a COMPLETE listing.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Set when a complete listing no longer contains it; cleared if it
    # comes back. Never used to record a per-repository 404, which is a
    # race, not an authoritative absence — see app/github/ingestion.py.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GitHubRepositoryLanguage(Base):
    """One language and its byte count within a repository.

    Normalized rows rather than a JSON column: Prompt 3.3 joins these
    against the curated taxonomy, and docs/project-brief.md's
    Evidence-First rule wants stored signals inspectable by query, not
    by parsing a blob. Same reasoning that gave profile_target_skills
    its own table instead of a comma-separated column.

    Reconciled by difference on every run (delete what GitHub no longer
    reports, insert what is new), so an unchanged repository's rows keep
    their original created_at.
    """

    __tablename__ = "github_repository_languages"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("github_repositories.id", ondelete="CASCADE"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(100), primary_key=True)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GitHubRepositoryTopic(Base):
    """One topic tag on a repository ("machine-learning", "fastapi").

    Same normalization reasoning as languages. Topics arrive inside the
    repository listing payload, so they cost no extra request — which
    matters against a 60-request/hour budget.
    """

    __tablename__ = "github_repository_topics"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("github_repositories.id", ondelete="CASCADE"), primary_key=True
    )
    topic: Mapped[str] = mapped_column(String(100), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
