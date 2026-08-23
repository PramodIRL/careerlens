from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_ROLES = 20
_MAX_SKILLS = 30


class ExperienceLevel(StrEnum):
    """Closed set of experience tiers for an early-career candidate.

    Deliberately a Python enum validated at the API boundary rather than
    a Postgres native enum column (see app/models/profile.py) — adding
    or renaming a tier later is a code change, not a migration.
    """

    STUDENT = "student"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"


def _clean_optional_text(v: str | None, *, field_name: str, max_length: int) -> str | None:
    """Shared rule for full_name/headline/city/country: `null` clears the
    field; a string must be non-blank after trimming and within
    max_length. There is no way to set a field to an empty string —
    clearing is always explicit `null`, never "" (avoids two different
    JSON values meaning the same "unset" thing)."""
    if v is None:
        return None
    trimmed = v.strip()
    if not trimmed:
        raise ValueError(f"{field_name} must not be blank — send null to clear it")
    if len(trimmed) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return trimmed


def _clean_string_list(
    values: list[str] | None, *, field_name: str, max_items: int, max_item_length: int
) -> list[str]:
    """Shared rule for target_roles/target_skills: trim each item, drop
    blanks, cap length, and de-duplicate case-insensitively while keeping
    the first-seen casing (nicer for display than forcing lowercase)."""
    if not values:
        return []
    if len(values) > max_items:
        raise ValueError(f"{field_name} accepts at most {max_items} items")

    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in values:
        item = raw.strip()
        if not item:
            continue
        if len(item) > max_item_length:
            raise ValueError(f"each {field_name} item must be at most {max_item_length} characters")
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    return cleaned


class ProfileUpdateRequest(BaseModel):
    """PATCH body. Every field is optional and independently omittable —
    the endpoint applies only the fields actually present in the JSON
    body (Pydantic's `exclude_unset`), so a client can update just one
    field without resending the rest of the profile.

    `target_roles`/`target_skills`, when present at all (including an
    empty list), fully REPLACE the profile's existing set rather than
    merging — the simplest contract for a tag-list editor to implement
    against.
    """

    full_name: str | None = None
    headline: str | None = None
    city: str | None = None
    country: str | None = None
    experience_level: ExperienceLevel | None = None
    target_roles: list[str] | None = None
    target_skills: list[str] | None = None

    @field_validator("full_name")
    @classmethod
    def _check_full_name(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, field_name="full_name", max_length=200)

    @field_validator("headline")
    @classmethod
    def _check_headline(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, field_name="headline", max_length=200)

    @field_validator("city")
    @classmethod
    def _check_city(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, field_name="city", max_length=120)

    @field_validator("country")
    @classmethod
    def _check_country(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, field_name="country", max_length=120)

    @field_validator("target_roles")
    @classmethod
    def _check_target_roles(cls, v: list[str] | None) -> list[str]:
        return _clean_string_list(
            v, field_name="target_roles", max_items=_MAX_ROLES, max_item_length=100
        )

    @field_validator("target_skills")
    @classmethod
    def _check_target_skills(cls, v: list[str] | None) -> list[str]:
        return _clean_string_list(
            v, field_name="target_skills", max_items=_MAX_SKILLS, max_item_length=80
        )


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    full_name: str | None
    headline: str | None
    city: str | None
    country: str | None
    experience_level: ExperienceLevel | None
    target_roles: list[str] = Field(default_factory=list)
    target_skills: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
