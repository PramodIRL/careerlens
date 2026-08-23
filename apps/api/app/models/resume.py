import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Resume(Base):
    """Metadata for one uploaded resume file, plus its text-extraction
    job state (Prompt 2.1 for upload/storage, Prompt 2.2 for extraction).

    The file's actual bytes never live in Postgres — they're written
    through the ResumeStorage interface (app/storage/) under
    `storage_key`, an opaque, server-generated locator that is never
    returned to a client (see app/api/v1/resume.py and
    app/schemas/resume.py's ResumeResponse) — that's what keeps raw
    filesystem paths out of API responses. `original_filename` is
    display-only metadata, sanitized on the way in, and is never used to
    build a filesystem path — only `storage_key` does that.

    Unlike `profiles` (1:1 with a user), a user can have several
    resumes, so this has its own `id` rather than being keyed on
    `user_id`.

    `status` is a small closed set (see app.schemas.resume.ResumeStatus:
    queued/processing/succeeded/failed), stored as plain text rather
    than a Postgres native enum, matching `profiles.experience_level`'s
    reasoning: adding a state later is a code change, not a migration.
    A resume starts "queued" the instant it's uploaded (Prompt 2.2's
    worker — app/worker.py — picks it up automatically) and only ever
    reaches a terminal state (succeeded/failed) once, via an atomic
    claim (`UPDATE ... WHERE status = 'queued'`) that also prevents two
    concurrent executions from processing the same resume twice.

    There is no separate `resume_extraction_jobs`/history table — these
    columns hold only the *current* extraction outcome, not a log of
    every attempt. A resume is never re-extracted once it reaches a
    terminal state in this prompt (no manual "retry" trigger exists),
    so a single current-state row is sufficient.
    """

    __tablename__ = "resumes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    # The extraction result. Never returned by any API response in this
    # prompt (see ResumeResponse) — reading it back is for a later
    # prompt (skill extraction) to do server-side.
    extracted_text: Mapped[str | None] = mapped_column(Text, default=None)
    # A curated, safe message only — e.g. "the document could not be
    # read" — never the raw exception/traceback. See app/worker.py.
    error_message: Mapped[str | None] = mapped_column(String(500), default=None)
    # How many times extraction has actually run for this resume.
    # Mirrors Celery's own `self.request.retries` rather than being a
    # hand-maintained counter — see app/worker.py.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # When the job reached a terminal state (succeeded or failed) —
    # distinct from `updated_at`, which also changes on the
    # queued -> processing transition.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
