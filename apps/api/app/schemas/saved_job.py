"""Request/response models for saved job descriptions (Prompt 4.1).

Two rules here are security boundaries rather than input hygiene, and
both are enforced at this layer because it is the only place a client's
bytes are inspected before they reach the database:

  1. `source_url` is validated as an http/https URL AND NEVER FETCHED.
     Validation limits what a browser will do with the link when the
     user clicks it; it does NOT make the value safe for the server to
     request. See app/models/saved_job.py.

  2. `user_id` is never accepted from a client. `extra="forbid"` means a
     request that tries to name an owner is REJECTED with a 422 rather
     than silently ignored — the same reasoning that made the GitHub
     connect request reject a stray `password` field instead of
     dropping it (app/schemas/github.py).
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator

# Bounds live here, not on the columns. `description` is a Text column
# with no useful database length, so the ceiling belongs where it can
# produce a readable validation error instead of a driver exception —
# the same split as `resume_max_size_bytes`, which the upload route
# enforces rather than the schema.
#
# 60,000 characters is roughly six times a long real posting, chosen to
# bound unbounded user input rather than to be a product limit anyone
# will notice. A pasted full HTML page can exceed it and is rejected
# with a clear message.
MAX_DESCRIPTION_LENGTH = 60_000
MAX_COMPANY_LENGTH = 200
MAX_TITLE_LENGTH = 200
MAX_LOCATION_LENGTH = 200
# The practical ceiling browsers and proxies agree on for a URL.
MAX_SOURCE_URL_LENGTH = 2048


class EmploymentType(StrEnum):
    """Closed set of employment arrangements for a saved posting.

    Values are lowercase snake_case, matching every other vocabulary in
    this codebase (`manual_entry`, `resume_alias_match`, `mid`), and are
    stored as plain text with no database CHECK — so adding one later is
    a code change, not an ALTER TYPE migration. See
    app/models/saved_job.py.

    Deliberately coarse. This is a label a candidate picks from a
    dropdown to organize their own list, not an attempt to model every
    contractual arrangement in employment law.
    """

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"


def _clean_required_text(v: str, *, field_name: str, max_length: int) -> str:
    """Trim, reject blank, enforce a ceiling. Mirrors
    app/schemas/profile.py's `_clean_optional_text`, but for fields that
    have no "clear it" state: a saved job with no company or title is
    not identifiable later, so blank is an error rather than a null."""
    trimmed = v.strip()
    if not trimmed:
        raise ValueError(f"{field_name} must not be blank")
    if len(trimmed) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return trimmed


def _clean_optional_text(v: str | None, *, field_name: str, max_length: int) -> str | None:
    """`null` clears the field; a string must be non-blank after
    trimming and within max_length. Same contract, and same reasoning
    about "" versus null, as the profile schema's helper."""
    if v is None:
        return None
    trimmed = v.strip()
    if not trimmed:
        raise ValueError(f"{field_name} must not be blank — send null to clear it")
    if len(trimmed) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return trimmed


def _clean_description(v: str) -> str:
    """The posting, stored VERBATIM.

    Only the outer whitespace is trimmed — internal formatting, blank
    lines and indentation are preserved exactly, because Prompt 4.2 will
    quote spans of this text as evidence and an excerpt must be a real
    slice of what the user saved. Nothing here rewrites, normalizes,
    strips markup, or summarizes.
    """
    trimmed = v.strip()
    if not trimmed:
        raise ValueError("description must not be blank")
    if len(trimmed) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"description must be at most {MAX_DESCRIPTION_LENGTH:,} characters")
    return trimmed


def _clean_source_url(v: str | None) -> str | None:
    """Validate as an absolute http(s) URL, then store the STRING.

    Pydantic's HttpUrl is used as a validator rather than as the stored
    type: it normalizes (adding a trailing slash to a bare host, for
    example), and this field is a value the user typed to find their way
    back to a posting, so it is kept as written.

    Restricted to http/https, which rejects `javascript:`, `file:`,
    `data:` and `ftp:`. That protects the USER's browser when they click
    the stored link. It does not make the URL safe for the server to
    fetch — nothing in CareerLens fetches it, by design.
    """
    if v is None:
        return None
    trimmed = v.strip()
    if not trimmed:
        # An empty box in a form means "no URL", not a validation error.
        return None
    if len(trimmed) > MAX_SOURCE_URL_LENGTH:
        raise ValueError(f"source_url must be at most {MAX_SOURCE_URL_LENGTH} characters")
    parsed = HttpUrl(trimmed)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("source_url must be an http or https link")
    return trimmed


class SavedJobCreateRequest(BaseModel):
    """Everything needed to save a posting. `user_id` is deliberately
    absent — the owner comes from the access token, and `extra="forbid"`
    turns an attempt to supply one into a 422."""

    model_config = ConfigDict(extra="forbid")

    company: str
    title: str
    description: str
    location: str | None = None
    employment_type: EmploymentType | None = None
    source_url: str | None = None

    @field_validator("company")
    @classmethod
    def _check_company(cls, v: str) -> str:
        return _clean_required_text(v, field_name="company", max_length=MAX_COMPANY_LENGTH)

    @field_validator("title")
    @classmethod
    def _check_title(cls, v: str) -> str:
        return _clean_required_text(v, field_name="title", max_length=MAX_TITLE_LENGTH)

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        return _clean_description(v)

    @field_validator("location")
    @classmethod
    def _check_location(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, field_name="location", max_length=MAX_LOCATION_LENGTH)

    @field_validator("source_url")
    @classmethod
    def _check_source_url(cls, v: str | None) -> str | None:
        return _clean_source_url(v)


class SavedJobUpdateRequest(BaseModel):
    """Partial update. An omitted field means "leave unchanged"; an
    explicit `null` clears an OPTIONAL scalar.

    The three required fields (`company`, `title`, `description`) are
    typed non-optional, so `null` for any of them is a 422 rather than a
    way to blank a row the API says must always be identifiable.
    """

    model_config = ConfigDict(extra="forbid")

    company: str | None = None
    title: str | None = None
    description: str | None = None
    location: str | None = None
    employment_type: EmploymentType | None = None
    source_url: str | None = None

    @field_validator("company")
    @classmethod
    def _check_company(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("company is required and cannot be cleared")
        return _clean_required_text(v, field_name="company", max_length=MAX_COMPANY_LENGTH)

    @field_validator("title")
    @classmethod
    def _check_title(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("title is required and cannot be cleared")
        return _clean_required_text(v, field_name="title", max_length=MAX_TITLE_LENGTH)

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("description is required and cannot be cleared")
        return _clean_description(v)

    @field_validator("location")
    @classmethod
    def _check_location(cls, v: str | None) -> str | None:
        return _clean_optional_text(v, field_name="location", max_length=MAX_LOCATION_LENGTH)

    @field_validator("source_url")
    @classmethod
    def _check_source_url(cls, v: str | None) -> str | None:
        return _clean_source_url(v)


class SavedJobResponse(BaseModel):
    """One saved posting.

    Returns only what the caller stored. No extracted skills, no match
    score, no fetched page content — none of that exists in Prompt 4.1,
    so none of it can leak from here.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company: str
    title: str
    location: str | None
    employment_type: EmploymentType | None
    source_url: str | None
    # The posting exactly as the user saved it.
    description: str
    created_at: datetime
    updated_at: datetime
