"""Closed vocabularies for the skill taxonomy and evidence model
(Prompt 2.3), plus the request/response models for the candidate-skill
review endpoints (Prompt 2.4).

Each vocabulary is a Python StrEnum validated at the application
boundary, with the underlying column stored as plain text — the same
choice, for the same reason, as `profiles.experience_level` and
`resumes.status`: adding a category or a source type later is a code
change, not an `ALTER TYPE` migration. `SkillEvidence.confidence` is the
deliberate exception that *does* get a database CHECK constraint (see
app/models/skill_evidence.py) — a numeric range is a permanent
invariant, whereas a vocabulary is expected to grow.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_SKILL_NAME_LENGTH = 80


class SkillCategory(StrEnum):
    """Coarse grouping for a canonical skill, used to keep the seeded
    taxonomy reviewable and to group skills in a later UI.

    Intentionally coarse — this is a small curated taxonomy for
    early-career software roles, not an attempt to classify every skill
    in technology (see app/seeds/skill_taxonomy.py).

    Nullable on the column: a skill coined by a user through their
    profile's target skills (app/api/v1/profile.py) genuinely has no
    taxonomy category, and defaulting it to "other" would invent
    information we do not have. Prompt 2.4 relies on exactly that: a
    non-null category is what distinguishes a *curated* taxonomy skill
    from a user-coined one, and only curated skills may become candidate
    skills (see app/api/v1/candidate_skill.py).
    """

    LANGUAGE = "language"
    FRAMEWORK = "framework"
    DATABASE = "database"
    INFRASTRUCTURE = "infrastructure"
    TOOL = "tool"
    TESTING = "testing"
    CONCEPT = "concept"


class EvidenceSourceType(StrEnum):
    """Where a piece of skill evidence came from.

    Each value implies a different convention for
    `SkillEvidence.source_identifier` — see that model's docstring for
    the per-type contract.
    """

    RESUME = "resume"
    GITHUB = "github"
    MANUAL = "manual"


class ExtractionMethod(StrEnum):
    """How a piece of evidence was derived.

    Every member must stay a *deterministic, inspectable* method, per
    docs/project-brief.md's Evidence-First rule: an LLM may later explain
    a persisted result, but must never be the thing that invented it.
    RESUME_ALIAS_MATCH is Prompt 2.4's boundary-aware taxonomy matching
    (app/skill_matching.py) — no model inference anywhere in it, and the
    four GITHUB_* members below run that SAME matcher over GitHub-sourced
    strings rather than introducing a second one.

    WHY GITHUB HAS FOUR VALUES AND NOT ONE. The evidence natural key is
    (candidate_skill_id, source_type, source_identifier,
    extraction_method), and GitHub evidence uses the repository's
    "owner/repo" as its source_identifier — so `extraction_method` is the
    only field distinguishing several signals coming from the SAME
    repository. Collapsing them into one value would force an invented
    precedence rule for which excerpt wins, and would merge facts that
    are genuinely distinct: "Python is named in the README" and "Python
    is 82% of this repository's bytes" are two observations, not one.

    Adding these needs no migration — `skill_evidence.extraction_method`
    is plain text with no database CHECK, exactly so a vocabulary can
    grow without an ALTER TYPE (see this module's own docstring).
    """

    MANUAL_ENTRY = "manual_entry"
    RESUME_ALIAS_MATCH = "resume_alias_match"
    # --- GitHub-derived (Prompt 3.3), one per distinct stored signal ---
    GITHUB_README_MATCH = "github_readme_match"
    GITHUB_DESCRIPTION_MATCH = "github_description_match"
    GITHUB_TOPIC_MATCH = "github_topic_match"
    # The only method that stores a NULL excerpt. A language is a
    # computed byte statistic, not text anybody wrote, so there is
    # nothing to quote — and inventing "Python (82,341 bytes)" would be
    # authored prose presented as a quotation. The byte counts stay
    # inspectable in github_repository_languages.
    GITHUB_LANGUAGE_MATCH = "github_language_match"


class CandidateSkillStatus(StrEnum):
    """Review state of one candidate skill (Prompt 2.4).

    SUGGESTED is what the extractor writes; the other two are only ever
    written by the user, through the candidate-skill endpoints.

    REJECTED is a persistent tombstone, NOT a deletion. If rejecting
    removed the row, the next extraction run over the same resume would
    faithfully recreate it and the user's decision would silently
    evaporate. Keeping the row is what makes the override durable — see
    app/skill_extraction.py, which never writes this column at all.
    """

    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class CandidateSkillDecision(StrEnum):
    """The subset of CandidateSkillStatus a user may set directly.

    "suggested" is deliberately absent: it means "the extractor proposed
    this and nobody has reviewed it yet", which is not a state a person
    can meaningfully return a skill to.
    """

    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class SkillEvidenceResponse(BaseModel):
    """One inspectable reason a skill is attributed to a candidate.

    `confidence` is exposed as a float purely for the client's
    convenience; it is stored as NUMERIC(3,2) so the persisted value
    stays exact (see app/models/skill_evidence.py).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_type: EvidenceSourceType
    # For resume evidence this is the owning user's own resume id, so
    # returning it leaks nothing across users and lets the UI link back.
    source_identifier: str
    # A verbatim span from the source document — never the full text,
    # never a path, never generated prose. Null for evidence with
    # nothing quotable (a manual assertion).
    excerpt: str | None
    extraction_method: ExtractionMethod
    confidence: float
    created_at: datetime


class CandidateSkillResponse(BaseModel):
    """A candidate skill plus every piece of evidence supporting it.

    Evidence is nested rather than fetched separately: the dashboard has
    no use for a skill without its justification, and the Evidence-First
    rule means the two should be hard to display apart.
    """

    id: UUID
    skill_id: UUID
    skill_name: str
    skill_category: SkillCategory | None
    status: CandidateSkillStatus
    evidence: list[SkillEvidenceResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class CandidateSkillCreateRequest(BaseModel):
    """Manual add. `name` is resolved against the curated taxonomy only —
    a canonical name or a known alias — and never creates a new
    `skills` or `skill_aliases` row. See app/api/v1/candidate_skill.py
    for the full contract and why it differs from Prompt 1.3's
    free-text target skills."""

    name: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("name must not be blank")
        if len(trimmed) > _MAX_SKILL_NAME_LENGTH:
            raise ValueError(f"name must be at most {_MAX_SKILL_NAME_LENGTH} characters")
        return trimmed


class CandidateSkillUpdateRequest(BaseModel):
    """Confirm or reject a candidate skill."""

    status: CandidateSkillDecision
