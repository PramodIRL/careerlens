"""Candidate profile endpoints: read and update full name, headline,
city/country, experience level, target roles, and declared target
skills for a signed-in user's own profile.

Ownership enforcement: both routes are id-addressable
(`/profiles/{user_id}`) rather than an implicit "me" — `user_id` in the
path must match the authenticated caller's own id, checked as the very
first thing in each handler, or the request fails with 403 before any
profile data is touched. See docs/decisions.md for why this shape (over
a self-only `/profile`) was chosen.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.db import get_db
from app.models.profile import Profile, ProfileTargetRole, ProfileTargetSkill
from app.models.skill import Skill
from app.models.user import User
from app.schemas.profile import ExperienceLevel, ProfileResponse, ProfileUpdateRequest

router = APIRouter()

_NOT_YOUR_PROFILE = "not authorized to access this profile"


def _require_self(user_id: uuid.UUID, current_user: User) -> None:
    """Not a credential-guessing surface (see docs/decisions.md's login
    error-design entry for the contrast) — a specific 403 is fine here,
    the same way registration's duplicate-email 409 is specific."""
    if user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_NOT_YOUR_PROFILE)


async def _get_or_create_profile(db: AsyncSession, user_id: uuid.UUID) -> Profile:
    profile = await db.get(Profile, user_id)
    if profile is not None:
        return profile
    profile = Profile(user_id=user_id)
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return profile


async def _load_target_roles(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    rows = await db.scalars(
        select(ProfileTargetRole.role_title)
        .where(ProfileTargetRole.profile_user_id == user_id)
        .order_by(ProfileTargetRole.role_title)
    )
    return list(rows.all())


async def _load_target_skills(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    rows = await db.scalars(
        select(Skill.name)
        .join(ProfileTargetSkill, ProfileTargetSkill.skill_id == Skill.id)
        .where(ProfileTargetSkill.profile_user_id == user_id)
        .order_by(Skill.name)
    )
    return list(rows.all())


async def _get_or_create_skill(db: AsyncSession, name: str) -> Skill:
    """Resolve a user-supplied skill name to a canonical Skill row,
    matching case-insensitively on `slug` and creating a new row only
    when no match exists. Uses `INSERT ... ON CONFLICT DO NOTHING` rather
    than a plain select-then-insert so two concurrent requests coining
    the same new skill (e.g. two profiles' first-ever "Rust") can't race
    into a unique-constraint error — the loser just re-selects the
    winner's row."""
    slug = name.casefold()
    inserted = await db.scalars(
        pg_insert(Skill)
        .values(name=name, slug=slug)
        .on_conflict_do_nothing(index_elements=[Skill.slug])
        .returning(Skill)
    )
    skill = inserted.first()
    if skill is not None:
        return skill
    existing = await db.scalar(select(Skill).where(Skill.slug == slug))
    assert existing is not None, "skill insert conflicted but no row was found by slug"
    return existing


def _to_response(profile: Profile, roles: list[str], skills: list[str]) -> ProfileResponse:
    return ProfileResponse(
        user_id=profile.user_id,
        full_name=profile.full_name,
        headline=profile.headline,
        city=profile.city,
        country=profile.country,
        experience_level=ExperienceLevel(profile.experience_level)
        if profile.experience_level
        else None,
        target_roles=roles,
        target_skills=skills,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.get("/{user_id}", response_model=ProfileResponse)
async def get_profile(
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileResponse:
    _require_self(user_id, current_user)
    profile = await _get_or_create_profile(db, user_id)
    roles = await _load_target_roles(db, user_id)
    skills = await _load_target_skills(db, user_id)
    return _to_response(profile, roles, skills)


@router.patch("/{user_id}", response_model=ProfileResponse)
async def update_profile(
    user_id: uuid.UUID,
    body: ProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileResponse:
    _require_self(user_id, current_user)
    profile = await _get_or_create_profile(db, user_id)

    # Only fields actually present in the JSON body — this is the whole
    # point of exclude_unset for a PATCH: an omitted field means "leave
    # unchanged", not "clear it" (clearing an optional scalar is explicit
    # `null` instead — see ProfileUpdateRequest).
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        roles = await _load_target_roles(db, user_id)
        skills = await _load_target_skills(db, user_id)
        return _to_response(profile, roles, skills)

    for field in ("full_name", "headline", "city", "country"):
        if field in updates:
            setattr(profile, field, updates[field])
    if "experience_level" in updates:
        value = updates["experience_level"]
        profile.experience_level = value.value if isinstance(value, ExperienceLevel) else value

    if "target_roles" in updates:
        await db.execute(
            delete(ProfileTargetRole).where(ProfileTargetRole.profile_user_id == user_id)
        )
        for role_title in updates["target_roles"]:
            db.add(ProfileTargetRole(profile_user_id=user_id, role_title=role_title))

    if "target_skills" in updates:
        await db.execute(
            delete(ProfileTargetSkill).where(ProfileTargetSkill.profile_user_id == user_id)
        )
        for skill_name in updates["target_skills"]:
            skill = await _get_or_create_skill(db, skill_name)
            db.add(ProfileTargetSkill(profile_user_id=user_id, skill_id=skill.id))

    # profiles.updated_at has onupdate=func.now(), which only fires when
    # a *column on that row* changes — a target_roles/target_skills-only
    # PATCH never touches a `profiles` column, so it's set explicitly
    # here to reflect that the profile, as a whole, did change.
    profile.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(profile)
    roles = await _load_target_roles(db, user_id)
    skills = await _load_target_skills(db, user_id)
    return _to_response(profile, roles, skills)
