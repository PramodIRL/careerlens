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
    # The candidate's own ordering key (Prompt 6.3). It ORDERS, it does
    # not LABEL: the rank a user sees is this job's 1-based index in the
    # list response, so a gap left by a deleted job is invisible.
    position: int
    created_at: datetime
    updated_at: datetime


class SavedJobOrderRequest(BaseModel):
    """A new ordering for the caller's saved jobs (Prompt 6.3).

    A FULL PERMUTATION, NOT A PARTIAL LIST. The request must name every
    job the caller owns, exactly once. A partial list is rejected rather
    than interpreted: it is genuinely ambiguous about where the omitted
    jobs go, and the friendly reading — "leave them where they are" —
    silently drops somebody's job to the bottom of their own priorities.

    `user_id` is absent and `extra="forbid"` turns an attempt to supply
    one into a 422, the same structural ownership every other request
    model here has.
    """

    model_config = ConfigDict(extra="forbid")

    # Highest priority first. Index 0 becomes the user's #1.
    job_ids: list[UUID]

    @field_validator("job_ids")
    @classmethod
    def _check_job_ids(cls, v: list[UUID]) -> list[UUID]:
        if not v:
            raise ValueError("job_ids must not be empty")
        if len(set(v)) != len(v):
            raise ValueError("job_ids must not contain duplicates")
        return v


class JobDraftResponse(BaseModel):
    """An UNSAVED, unverified job draft produced by a PDF import
    (Prompt 4.1b).

    NOTHING HERE IS PERSISTED. The import endpoint returns this and
    writes no rows at all; the user reviews and corrects it, and only the
    existing `POST /api/v1/saved-jobs` creates a SavedJob. That is what
    makes "never silently save incorrect extracted data" structural
    rather than a promise — there is no code path from an import to a
    stored row that does not pass through the user.

    Every field except `description` is nullable, and a null means "we
    could not determine this", which the UI renders as an empty box. It
    never means "we guessed something" — the PDF extractor reads only
    fields the document explicitly labels. `description` is always
    present: an extraction that cannot find one fails outright instead
    of returning an empty draft.

    `source_url` is always None for a PDF import and exists here so the
    draft matches the saved-job shape the form submits; the user may
    still type a link in before saving.
    """

    model_config = ConfigDict(from_attributes=True)

    company: str | None
    title: str | None
    location: str | None
    employment_type: EmploymentType | None
    source_url: str | None
    description: str
    # Plain-language prompts for the review step, e.g. "Company could not
    # be read from the page — please add it."
    notes: list[str] = []


class RequirementLevelSchema(StrEnum):
    """How strongly a posting asks for a skill (Prompt 4.2).

    Mirrors app.job_requirements.classify.RequirementLevel at the API
    boundary. MENTIONED is the default and does NOT mean "weakly
    required" — it means the posting named a curated skill and said
    nothing about necessity.
    """

    REQUIRED = "required"
    PREFERRED = "preferred"
    MENTIONED = "mentioned"


class RequirementExtractionMethod(StrEnum):
    """How a job requirement was derived.

    One value today, and it must stay a DETERMINISTIC, inspectable
    method per docs/project-brief.md's Evidence-First rule: clause-local
    classification over the existing curated taxonomy and the existing
    matcher. No model inference anywhere in it.
    """

    JOB_DESCRIPTION_MATCH = "job_description_match"


class JobSkillRequirementResponse(BaseModel):
    """One skill a saved job asks for.

    Every field exists to answer a question the Evidence-First rule
    requires this product to be able to answer:

        which skill matched?     -> skill_id / skill_name
        what text supports it?   -> excerpt (verbatim from the posting)
        which spelling matched?  -> matched_term ("py" vs "Python")
        how strongly asked for?  -> requirement_level
        how sure of the match?   -> confidence
        what derived it?         -> extraction_method
        when?                    -> created_at / updated_at

    `saved_job_id` is deliberately absent: these are only ever returned
    nested under the job they belong to, so echoing it back would be
    redundant. There is no requirement id in any route either — see
    app/api/v1/saved_job.py on why ownership stays structural.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    skill_id: UUID
    skill_name: str
    skill_category: str | None
    requirement_level: RequirementLevelSchema
    # WHICH spelling matched — the difference between explaining a match
    # and merely asserting it.
    matched_term: str
    # The verbatim clause that drove the classification. Never generated
    # prose: a real slice of the saved job's description.
    excerpt: str
    # From the existing matcher, unchanged. Answers "does this string
    # denote this skill", NOT "how strongly is it required".
    confidence: float
    extraction_method: RequirementExtractionMethod
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------
# Candidate <-> job matching (Prompt 4.3)
#
# READ-ONLY PRESENTATION MODELS. Nothing below is persisted: a match is
# recomputed from current rows on every request. See
# app/api/v1/saved_job.py.
# --------------------------------------------------------------------


class MatchedEvidenceResponse(BaseModel):
    """One reason the candidate is credited with a skill.

    A verbatim reference to an EXISTING `skill_evidence` row — the
    matcher authors no evidence and persists no generated text.
    """

    model_config = ConfigDict(from_attributes=True)

    source_type: str
    excerpt: str | None
    confidence: float


class LevelBreakdownResponse(BaseModel):
    """Matched-out-of-total for one requirement level, so a client can
    render "Required 4 / 5" without recomputing it."""

    model_config = ConfigDict(from_attributes=True)

    matched: int
    total: int


class MatchedSkillResponse(BaseModel):
    """A job requirement the candidate satisfies.

    Carries enough to explain itself without a second request: which
    skill, how strongly the job asks for it, the job's own words, the
    candidate's review state, and the candidate-side evidence.

    `candidate_unreviewed` is the `suggested` case surfaced explicitly.
    It still counts toward the score — an extractor found real, persisted
    evidence — but the client can prompt the user to confirm it rather
    than presenting an unreviewed guess as settled fact.
    """

    model_config = ConfigDict(from_attributes=True)

    skill_id: UUID
    skill_name: str
    requirement_level: RequirementLevelSchema
    # The job's own words that produced this requirement.
    job_excerpt: str
    # "confirmed" or "suggested" — never "rejected", which cannot match.
    candidate_status: str
    candidate_unreviewed: bool
    candidate_evidence: list[MatchedEvidenceResponse] = []


class MissingSkillResponse(BaseModel):
    """A job requirement the candidate does not satisfy.

    `candidate_rejected` distinguishes two very different situations that
    would otherwise look identical: the candidate never had this skill,
    versus the candidate explicitly disowned it. The UI can then say
    "you rejected this" instead of "you don't have this" — and a
    rejection is a tombstone, so it never counts as matched however much
    stale evidence remains.
    """

    model_config = ConfigDict(from_attributes=True)

    skill_id: UUID
    skill_name: str
    requirement_level: RequirementLevelSchema
    job_excerpt: str
    candidate_rejected: bool


class JobMatchResponse(BaseModel):
    """How well the authenticated candidate matches one saved job.

    NOT PERSISTED, and that is the design. A stored score would be a
    cache with no invalidation trigger: confirming a skill, rejecting
    one, or editing the job description would each silently stale it.
    Recomputing from current rows means the next request is always
    correct, with no "rebuild score" workflow to forget.

    `formula_version` is returned because a score is meaningless without
    knowing which arithmetic produced it — a screenshotted 77% cannot be
    reproduced otherwise.

    REQUIRED COVERAGE IS REPORTED SEPARATELY, on purpose. skill_match_v1
    does not penalise or cap for missing required skills, so a job can
    score well on weighted coverage while a hard requirement is unmet.
    `required_matched` / `required_total` / `required_missing` are what
    make that unmistakable rather than buried in one opaque number.
    """

    model_config = ConfigDict(from_attributes=True)

    formula_version: str
    overall_score: int
    earned_weight: int
    obtainable_weight: int
    # False when the job has no recognised skill requirements at all. A
    # client must say "no skill requirements detected", NOT "0% match" —
    # the first is a statement about the job, the second about the
    # candidate, and they are not the same claim.
    has_requirements: bool
    required_matched: int
    required_total: int
    by_level: dict[str, LevelBreakdownResponse]
    # Echoed so a reader can check the arithmetic by hand.
    weights: dict[str, int]
    matched_skills: list[MatchedSkillResponse] = []
    missing_skills: list[MissingSkillResponse] = []
    # The subset of missing_skills the job marks required — the ones
    # that actually block the candidate.
    required_missing: list[MissingSkillResponse] = []


# --------------------------------------------------------------------
# Explainable skill gaps (Prompt 4.4)
#
# READ-ONLY PRESENTATION MODELS. Nothing below is persisted: gaps are
# recomputed from current rows on every request. See
# app/api/v1/saved_job.py.
# --------------------------------------------------------------------


class GapEntryResponse(BaseModel):
    """One job requirement the candidate does not currently satisfy,
    with everything needed to explain why.

    THE API SUPPLIES FACTS, THE UI SUPPLIES WORDING. `job_excerpt` is a
    verbatim slice of the saved job's description and
    `candidate_evidence` are real `skill_evidence` rows. Nothing here is
    generated prose, and a genuinely missing skill carries an EMPTY
    evidence list rather than an invented sentence saying so.

    `candidate_status` is null when no candidate_skills row exists at
    all — which is a different thing from a row that says "rejected",
    and the two must stay distinguishable.
    """

    model_config = ConfigDict(from_attributes=True)

    skill_id: UUID
    skill_name: str
    # Preserved from the job requirement, so a rejected REQUIRED skill
    # is still visibly required.
    requirement_level: RequirementLevelSchema
    job_excerpt: str
    # "confirmed" / "suggested" / "rejected", or null when the candidate
    # has no row for this skill.
    candidate_status: str | None
    # Populated for needs-confirmation and rejected entries, where real
    # stored evidence exists. Empty for genuinely missing skills.
    candidate_evidence: list[MatchedEvidenceResponse] = []


class GapTotalsResponse(BaseModel):
    """Counts per bucket, so a client can summarise without re-counting
    arrays it may not have rendered."""

    model_config = ConfigDict(from_attributes=True)

    required_gaps: int
    preferred_gaps: int
    informational_gaps: int
    needs_confirmation: int
    rejected_requirements: int
    satisfied: int
    total_requirements: int


class JobGapResponse(BaseModel):
    """Explainable skill gaps for one saved job.

    FIVE BUCKETS, DELIBERATELY NOT ONE "missing" LIST. The product
    question is not just "what am I missing" but "why is this a gap",
    and three of those answers are materially different to a person:

        required/preferred/informational_gaps
            we found no candidate skill for this at all
        needs_confirmation
            we DID find evidence — you just have not reviewed it
        rejected_requirements
            you told us this is not yours

    Collapsing them would report a skill the user deliberately rejected
    as though the system simply failed to find it.

    NOT PERSISTED. Gap state changes when a skill is confirmed,
    rejected or manually added, when resume or GitHub evidence changes,
    and when a job description edit reconciles requirements — six
    invalidation triggers, several firing from Celery workers outside
    any request. A stored gap row would be stale almost immediately.

    `formula_version` is SEPARATE from `skill_match_v1`: the bucketing
    policy here can evolve without implying the score changed.

    BUCKETS ARE ORDERED required -> preferred -> informational ->
    needs_confirmation -> rejected, and entries within each bucket are
    sorted by skill name rather than insertion order, so two identical
    requests return byte-identical JSON. That ordering reflects the
    STORED REQUIREMENT LEVEL only and claims nothing about career
    importance.
    """

    model_config = ConfigDict(from_attributes=True)

    formula_version: str
    required_gaps: list[GapEntryResponse] = []
    preferred_gaps: list[GapEntryResponse] = []
    informational_gaps: list[GapEntryResponse] = []
    needs_confirmation: list[GapEntryResponse] = []
    rejected_requirements: list[GapEntryResponse] = []
    totals: GapTotalsResponse


class SemanticEvidenceResponse(BaseModel):
    """One piece of candidate evidence a job's wording sits near.

    Every field exists so a reader can check the claim: the excerpt is
    the candidate's own stored words, `similarity` is the number that
    put it in the list, and the ids let it be traced to the exact rows.

    THERE IS NO SKILL FIELD, deliberately. This says "this evidence
    looks relevant", never "the candidate has skill X" — that remains
    the deterministic matcher's business alone.
    """

    embedding_id: UUID
    source_type: str
    source_id: str
    evidence_id: UUID
    excerpt: str | None
    evidence_source_type: str
    evidence_source_identifier: str
    similarity: float


class JobSemanticResponse(BaseModel):
    """Semantic relevance for one saved job.

    NOT A CALIBRATED SCORE. `fit` and `band` come from provisional
    thresholds tuned against a seven-sentence fixture (see
    app/embeddings/semantic_fit.py) and must not be read as a hiring
    signal, a ranking, or a measure of competence. They summarise
    vector-space proximity between a job's wording and evidence the
    candidate already has stored.

    SEPARATE FROM `/match` ON PURPOSE. `skill_match_v1`'s
    `overall_score` is unchanged and unaffected by anything here, and
    nothing in this response can make a required skill count as
    satisfied.
    """

    formula_version: str
    fit: int
    band: str
    model_identifier: str
    considered: int
    evidence: list[SemanticEvidenceResponse]
