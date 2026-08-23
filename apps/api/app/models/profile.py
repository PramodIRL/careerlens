import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Profile(Base):
    """A candidate's profile — full name, headline, location, experience
    level. Strictly 1:1 with `users`: `user_id` is both the primary key
    and the foreign key, so there is no separate profile id to look up
    or keep in sync (see docs/decisions.md).

    Target roles and target skills are declared separately
    (ProfileTargetRole / ProfileTargetSkill below, and app/models/skill.py)
    rather than as columns here, since both are multi-valued — a comma-
    separated column would violate 1NF and make "does this profile want
    X" a string-matching query instead of a join.

    Lazily created on first GET/PATCH of `/api/v1/profiles/{user_id}`
    (see app/api/v1/profile.py) — every field is nullable so a brand-new
    profile has no "not found" state to special-case.
    """

    __tablename__ = "profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    full_name: Mapped[str | None] = mapped_column(String(200), default=None)
    headline: Mapped[str | None] = mapped_column(String(200), default=None)
    city: Mapped[str | None] = mapped_column(String(120), default=None)
    country: Mapped[str | None] = mapped_column(String(120), default=None)
    # A closed set validated in app/schemas/profile.py (ExperienceLevel),
    # stored as plain text rather than a Postgres native enum type so
    # adding/renaming a tier later is a code change, not an ALTER TYPE
    # migration.
    experience_level: Mapped[str | None] = mapped_column(String(20), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProfileTargetRole(Base):
    """One role a profile is targeting (e.g. "Backend Engineer").

    Plain normalized text, not a lookup table like Skill — see
    docs/decisions.md for why roles and skills are treated differently.
    `role_title` is stored trimmed, with its original casing kept for
    display; the API layer de-duplicates case-insensitively before
    writing, so the composite primary key is a defensive backstop against
    literal duplicate rows rather than the sole de-duplication mechanism.
    """

    __tablename__ = "profile_target_roles"

    profile_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.user_id", ondelete="CASCADE"), primary_key=True
    )
    role_title: Mapped[str] = mapped_column(String(100), primary_key=True)


class ProfileTargetSkill(Base):
    """Many-to-many link between a profile and a declared target Skill."""

    __tablename__ = "profile_target_skills"

    profile_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.user_id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True
    )
