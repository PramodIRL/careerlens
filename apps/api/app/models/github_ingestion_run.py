"""State and progress of one GitHub ingestion run (Prompt 3.2)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GitHubIngestionRun(Base):
    """One attempt to import a user's public GitHub repositories.

    Its own table rather than columns on `github_connections`, unlike
    Prompt 2.2 which put extraction state directly on the resume row.
    The difference is that a resume is extracted once, whereas re-import
    is the explicit point here — so "what did the last run do, and what
    failed" needs a record per run. Runs are small, and keeping them is
    what makes ingestion auditable rather than merely repeatable.

    AT MOST ONE ACTIVE RUN PER USER, enforced by a partial unique index
    (see __table_args__) rather than by the handler checking first. A
    double-clicked button, two browser tabs, or a retried POST would
    otherwise start two runs racing to upsert the same repositories.

    THE THREE COUNTS ARE NOT THE SAME NUMBER, and conflating them is
    exactly the UI bug this design exists to prevent:

        repositories_available        every public repo GitHub listed,
                                      forks included — the honest "your
                                      account has N repos" figure
        repositories_forks_excluded   how many of those are forks, which
                                      are skipped before the cap applies
                                      and never spend a request
        repositories_total            what this run actually fetched
                                      detail for:
                                      min(cap, available - forks)

    "The cap was hit" is derived at the response boundary, not stored:
    (available - forks) > cap. Without all three, a UI can only say "20
    imported", which reads as "you have 20 repositories".

    THERE IS NO "partial" STATUS. A run that listed successfully but lost
    three READMEs is `succeeded` with repositories_failed = 3 —
    partiality is data, surfaced in the UI, not a fifth state. Keeping
    this vocabulary identical to `resumes.status` is worth more than the
    extra nuance.

    RATE LIMITING IS NEVER A repositories_failed RESULT. It is handled at
    the run level: the run pauses and the task retries from GitHub's own
    reset time, preserving every repository already committed. Only
    timeouts, upstream errors and vanished repositories count as
    per-repository failures. See app/github/ingestion.py.
    """

    __tablename__ = "github_ingestion_runs"
    __table_args__ = (
        Index(
            "uq_github_ingestion_runs_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'processing')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # queued / processing / succeeded / failed — the same four strings as
    # `resumes.status`, stored as plain text with no database CHECK,
    # matching that model's reasoning: a vocabulary should be able to
    # grow without a migration.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")

    # Null until the listing completes; that null IS the "still
    # discovering how much there is" state, which the UI renders as an
    # indeterminate progress message rather than a fake 0-of-0.
    repositories_available: Mapped[int | None] = mapped_column(Integer, default=None)
    repositories_forks_excluded: Mapped[int | None] = mapped_column(Integer, default=None)
    repositories_total: Mapped[int | None] = mapped_column(Integer, default=None)
    # Incremented as each repository is committed, so progress is
    # visible while the run is still going rather than jumping from 0 to
    # done at the end.
    repositories_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Repositories whose DETAIL could not be fetched (timeout, upstream
    # error, or vanished between listing and detail). Their base row is
    # still saved from the listing — this never means "nothing stored".
    repositories_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # A curated, safe message only — never a raw exception, an upstream
    # status code or a response body. Same rule as `resumes.error_message`.
    error_message: Mapped[str | None] = mapped_column(String(500), default=None)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
