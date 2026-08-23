import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Resume(Base):
    """Metadata for one uploaded resume file (Prompt 2.1).

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

    `status` is a small closed set (see app.schemas.resume.ResumeStatus),
    stored as plain text rather than a Postgres native enum, matching
    `profiles.experience_level`'s reasoning: adding a state later is a
    code change, not a migration. Prompt 2.1 only ever writes
    "uploaded"; the rest are reserved for Prompt 2.2's extraction step.
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
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploaded")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
