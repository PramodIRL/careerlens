import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Skill(Base):
    """A canonical, deduplicated skill name.

    Shared vocabulary, not owned by any one profile: Prompt 1.3 links it
    from `profile_target_skills` (see app/models/profile.py), and later
    prompts (resume-extracted skills, GitHub evidence, job requirements)
    are expected to match against this same table rather than each
    inventing their own free-text skill spelling — see docs/decisions.md
    for the full normalization rationale.

    `slug` is the dedup key (lowercased, trimmed) so "Python", "python",
    and " Python " all resolve to one row; `name` keeps a display form.
    """

    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
