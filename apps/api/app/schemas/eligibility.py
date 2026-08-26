"""Job-side eligibility requirements and the derived eligibility result
(Prompt 5.1a).

TWO VOCABULARIES, ONE NAMESPACE. `EligibilityRequirementType` is
deliberately a subset of `QualificationFactType` using identical string
values, so resolving a requirement is a dictionary lookup rather than a
translation table that could drift. A requirement type that named
something the candidate side cannot hold would be unresolvable by
construction.

NOTHING BELOW IS PERSISTED except the requirement rows themselves. The
result models are computed per request — see app/api/v1/saved_job.py.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.qualification import QualificationFactType


class EligibilityRequirementType(StrEnum):
    """The bars a posting can set that this product will parse.

    Work authorization is deliberately OUT OF SCOPE for Prompt 5.1 —
    not deferred behind a flag, not modelled and unused, simply absent.
    The phrasings are open-ended, and the things that correlate with a
    person's right to work are exactly what a product must never infer
    it from.
    """

    CGPA = QualificationFactType.CGPA.value
    CLASS_10_PERCENTAGE = QualificationFactType.CLASS_10_PERCENTAGE.value
    CLASS_12_PERCENTAGE = QualificationFactType.CLASS_12_PERCENTAGE.value
    HIGHEST_DEGREE = QualificationFactType.HIGHEST_DEGREE.value
    FIELD_OF_STUDY = QualificationFactType.FIELD_OF_STUDY.value
    GRADUATION_YEAR = QualificationFactType.GRADUATION_YEAR.value
    YEARS_EXPERIENCE = QualificationFactType.YEARS_EXPERIENCE.value


class Comparator(StrEnum):
    """How a candidate's value is tested against the requirement."""

    GTE = "gte"
    LTE = "lte"
    EQ = "eq"
    IN = "in"
    BETWEEN = "between"


class EligibilityExtractionMethod(StrEnum):
    """How a requirement row was produced.

    One member today. It exists as a closed vocabulary anyway because
    5.1b and any later manual-entry path must be distinguishable from
    description parsing in the stored row — the same reason
    `ExtractionMethod` carries five members on the skill side.
    """

    JOB_DESCRIPTION_MATCH = "job_description_match"


class EligibilityState(StrEnum):
    """The verdict for one requirement against one candidate.

    FOUR STATES, AND THE LAST TWO ARE NOT FAILURES. Collapsing "we do
    not know" into "does not qualify" is the single worst thing an
    eligibility feature can do: it turns an empty profile field into a
    rejection the candidate never earned and cannot see the cause of.
    """

    # Candidate value known, and it clears the bar.
    SATISFIED = "satisfied"
    # Candidate value known, and it does not. The ONLY state that makes
    # somebody ineligible.
    NOT_SATISFIED = "not_satisfied"
    # The CANDIDATE side is missing something — no value declared, or a
    # CGPA with no scale to compare it on. Fixable by the candidate,
    # and the UI should say so.
    UNKNOWN = "unknown"
    # The REQUIREMENT side cannot be evaluated — the posting gave a CGPA
    # with no scale, or hedged a list open with "or related field".
    # Nothing the candidate does resolves this.
    UNDETERMINED = "undetermined"


class EligibilityFlag(StrEnum):
    """The whole-job answer to "can I apply?".

    NOT A PERCENTAGE, deliberately. A CGPA floor and a degree
    requirement are not commensurable, and weighting them against each
    other would be a second uncalibrated judgement stacked on
    `skill_match_v1`'s — with no data to justify it and no way for a
    reader to check it. The per-requirement breakdown carries the
    explanation; this carries the decision.
    """

    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"
    UNKNOWN = "unknown"


class EligibilityRequirementResponse(BaseModel):
    """One parsed requirement, with everything needed to explain it."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    requirement_type: EligibilityRequirementType
    comparator: Comparator
    numeric_value: Decimal | None
    numeric_max: Decimal | None
    value_scale: Decimal | None
    # Normalized accepted values for a categorical requirement.
    accepted_values: list[str] = []
    requirement_level: str
    open_ended: bool
    matched_term: str
    # A verbatim slice of the saved job's description.
    excerpt: str
    confidence: float
    extraction_method: EligibilityExtractionMethod
    created_at: datetime
    updated_at: datetime


class EligibilityEntryResponse(BaseModel):
    """One requirement, resolved against the candidate.

    Carries the candidate's own value so the UI can render "CGPA 7.8 ≥
    7.5" rather than a bare tick — and `null` when unknown, never a
    stand-in number.
    """

    model_config = ConfigDict(from_attributes=True)

    requirement_type: EligibilityRequirementType
    state: EligibilityState
    comparator: Comparator
    requirement_numeric: Decimal | None = None
    requirement_max: Decimal | None = None
    requirement_scale: Decimal | None = None
    accepted_values: list[str] = []
    requirement_level: str
    candidate_numeric: Decimal | None = None
    candidate_text: str | None = None
    candidate_scale: Decimal | None = None
    # Why this state, as a stable machine-readable token — never a
    # sentence. The UI owns the wording; the API owns the fact.
    reason: str
    excerpt: str


class EligibilityTotalsResponse(BaseModel):
    """Counts across the four states, plus how many of the failures are
    hard requirements."""

    model_config = ConfigDict(from_attributes=True)

    satisfied: int
    not_satisfied: int
    unknown: int
    undetermined: int
    total_requirements: int
    # The subset of `not_satisfied` at requirement_level=required — the
    # only ones that drive the flag.
    required_not_satisfied: int


class JobEligibilityResponse(BaseModel):
    """How the authenticated candidate stands against one job's bars.

    NOT PERSISTED, and never combined with `skill_match_v1`. A candidate
    editing their own CGPA changes this answer for every job at once,
    which is precisely the invalidation trigger a stored verdict would
    have no way to observe.

    `has_requirements` distinguishes "this posting states no bars we
    recognise" from "this candidate clears none of them" — both would
    otherwise render as an empty list, and they mean opposite things.
    """

    model_config = ConfigDict(from_attributes=True)

    formula_version: str
    flag: EligibilityFlag
    has_requirements: bool
    # Whether the candidate has asserted ANY qualification yet. Lets a
    # client distinguish "you have not set up a profile" from "your
    # profile has gaps" — both otherwise arrive as a wall of `unknown`,
    # and only one of them is fixed by filling in a form.
    has_qualification_profile: bool
    totals: EligibilityTotalsResponse
    requirements: list[EligibilityEntryResponse] = []
