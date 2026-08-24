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
    # A human-readable name for whatever `source_identifier` points at
    # (Prompt 3.4). Additive: existing clients that ignore it are
    # unaffected.
    #
    #   resume  -> the resume's own `original_filename`
    #   github  -> "owner/repo", which source_identifier already is
    #   manual  -> None; a self-assertion has no external source to name
    #
    # Null ALSO when the referenced row no longer resolves — a resume
    # deleted after its evidence was written. `source_identifier` is a
    # polymorphic string with no foreign key (see
    # app/models/skill_evidence.py), so a dangling reference is an
    # expected state, not an error: the evidence stays as the historical
    # record it is, simply without a display name.
    source_label: str | None = None
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


# --------------------------------------------------------------------
# Unified skill profile (Prompt 3.4)
#
# READ-ONLY PRESENTATION MODELS. Nothing below is persisted, and nothing
# below introduces a score. See app/api/v1/skill_profile.py.
# --------------------------------------------------------------------


class SkillProfileSummary(BaseModel):
    """Account-level rollup of the candidate's evidenced skills.

    EVERY FIELD IS A COUNT OR A BOOLEAN — a fact derived by counting
    stored rows, never a computed rating. Prompt 4.x owns scoring.

    `by_source` COUNTS DISTINCT SKILLS, NOT EVIDENCE ROWS, and the
    difference is the whole point: one repository naming Python in its
    README, description, topic and language writes four evidence rows
    (Prompt 3.3's four extraction methods) but supports exactly one
    skill. Counting rows would make GitHub look four times more
    informative than it is, purely as an artefact of how 3.3 decomposes
    signals.

    Its keys are EvidenceSourceType values. A source contributing
    nothing is present with a count of 0 rather than absent, so a client
    never has to distinguish "no evidence" from "key missing".
    """

    model_config = ConfigDict(from_attributes=True)

    # confirmed + suggested. Excludes rejected, matching `skills` below.
    total: int
    confirmed: int
    suggested: int
    # Counted but NOT listed in `skills` — see SkillProfileResponse.
    rejected: int
    by_source: dict[EvidenceSourceType, int] = Field(default_factory=dict)
    # Skills backed by more than one DISTINCT source type. The most
    # useful single signal this endpoint produces: a skill both a resume
    # and a repository attest to is corroborated in a way neither alone
    # is. Reported as a count, deliberately not turned into a weighting.
    multi_source: int
    # True when nothing is left awaiting review (suggested == 0).
    reviewed: bool


class SkillProfileEntry(BaseModel):
    """One evidenced skill, with its provenance rolled up.

    `strongest_evidence_confidence` IS NOT A SKILL SCORE, and the name is
    deliberately long to make that hard to misread. It is `max()` over
    the confidences already stored on this skill's evidence — a
    SELECTION of one existing value, not a computation over several.

    There is deliberately no blending, averaging, or source weighting.
    Prompt 2.4 defined `confidence` as a match-quality lookup answering
    "does this string denote this skill", NOT "how strong is this as
    evidence of ability" (see docs/decisions.md). Combining those numbers
    would invent the ranking model Prompt 4.x owns — and 4.x already has
    `source_type` and `extraction_method` on every evidence row to weight
    by, so nothing is lost by leaving it alone here.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    skill_id: UUID
    skill_name: str
    skill_category: SkillCategory | None
    status: CandidateSkillStatus
    # Distinct source types backing this skill, sorted for stable output.
    sources: list[EvidenceSourceType] = Field(default_factory=list)
    # How many evidence rows support it — the per-skill figure the
    # summary's `by_source` deliberately does not report.
    evidence_count: int
    strongest_evidence_confidence: float
    evidence: list[SkillEvidenceResponse] = Field(default_factory=list)


class SkillProfileResponse(BaseModel):
    """The candidate's unified, evidence-backed skill profile.

    REJECTED SKILLS ARE COUNTED IN `summary` BUT ABSENT FROM `skills`.
    A rejection is a persistent tombstone meaning "this is not mine"
    (app/schemas/skill.py's CandidateSkillStatus), so listing one inside
    a *profile* would contradict the decision the user made. Counting it
    keeps the tombstone visible rather than silently dropping data, and
    the review surface (GET /api/v1/candidate-skills) still returns all
    three states unchanged.

    `skills` is ordered by skill name, and evidence within each skill by
    (created_at, id), so two identical requests return identical JSON.
    """

    model_config = ConfigDict(from_attributes=True)

    summary: SkillProfileSummary
    skills: list[SkillProfileEntry] = Field(default_factory=list)
