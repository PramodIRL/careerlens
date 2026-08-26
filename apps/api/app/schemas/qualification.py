"""Candidate qualification facts: the vocabulary, and the request and
response shapes for reading and declaring them (Prompt 5.1a).

THE FLAT SHAPE IS A PRESENTATION CHOICE. Storage is one row per fact
(app/models/candidate_qualification.py), because 5.1b gives each fact
its own provenance and review state. A form does not want eight round
trips, so the API presents them as one object and the router maps
between the two. Nothing about the flat shape implies the facts share a
lifecycle.

EVERY FIELD IS OPTIONAL AND MAY BE NULL, and that is load-bearing rather
than lenient. `null` here means "we do not know", which
app/eligibility/resolve.py reports as UNKNOWN. There is no default, no
sentinel and no zero: an unstated CGPA must never become a 0.0 that
reports somebody as failing a bar they may well clear.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.skill import CandidateSkillStatus, EvidenceSourceType


class QualificationFactType(StrEnum):
    """The facts this product is willing to hold about a candidate.

    Deliberately short. Each entry exists because a real posting states
    a bar against it; nothing here is speculative, and adding a fact
    type is a decision about what personal data this product stores, not
    a routine schema change.
    """

    CGPA = "cgpa"
    CLASS_10_PERCENTAGE = "class_10_percentage"
    CLASS_12_PERCENTAGE = "class_12_percentage"
    HIGHEST_DEGREE = "highest_degree"
    FIELD_OF_STUDY = "field_of_study"
    GRADUATION_YEAR = "graduation_year"
    YEARS_EXPERIENCE = "years_experience"


# Which facts carry a number and which carry a normalized string. The
# storage column is chosen from this, so a fact type can never be
# written into both columns or neither.
NUMERIC_FACTS: frozenset[str] = frozenset(
    {
        QualificationFactType.CGPA.value,
        QualificationFactType.CLASS_10_PERCENTAGE.value,
        QualificationFactType.CLASS_12_PERCENTAGE.value,
        QualificationFactType.GRADUATION_YEAR.value,
        QualificationFactType.YEARS_EXPERIENCE.value,
    }
)
CATEGORICAL_FACTS: frozenset[str] = frozenset(
    {
        QualificationFactType.HIGHEST_DEGREE.value,
        QualificationFactType.FIELD_OF_STUDY.value,
    }
)


class Degree(StrEnum):
    """Normalized degree vocabulary.

    A Python enum rather than a `skills`-style lookup table: these
    values carry no evidence, no aliases needing their own rows and no
    relations, so a table would buy nothing but a join and a migration
    every time the list grows.
    """

    DIPLOMA = "diploma"
    BTECH = "btech"
    BE = "be"
    BSC = "bsc"
    BCA = "bca"
    MTECH = "mtech"
    ME = "me"
    MSC = "msc"
    MCA = "mca"
    MBA = "mba"
    PHD = "phd"


class FieldOfStudy(StrEnum):
    """Normalized field-of-study vocabulary.

    Widened in Prompt 5.1b to cover what real engineering resumes
    actually say. NOTE the deliberate asymmetry: the job-side pattern
    list (app/eligibility/classify.py's `_FIELD_TERMS`) was NOT widened
    with it, because 5.1b is a candidate-side slice and touching the job
    extractor was out of scope. A posting naming one of the new fields
    therefore still produces no requirement — a gap to close on the job
    side, not a reason to hold the candidate side back.

    OTHER is a real answer, not a fallback the extractor may reach for.
    Nothing infers it; only a person selecting it puts it on a row.
    """

    COMPUTER_SCIENCE = "computer_science"
    COMPUTER_ENGINEERING = "computer_engineering"
    INFORMATION_TECHNOLOGY = "information_technology"
    INFORMATION_SCIENCE = "information_science"
    AI_ML = "ai_ml"
    DATA_SCIENCE = "data_science"
    ELECTRONICS = "electronics"
    ELECTRICAL = "electrical"
    MECHANICAL = "mechanical"
    MECHATRONICS = "mechatronics"
    CIVIL = "civil"
    CHEMICAL = "chemical"
    AEROSPACE = "aerospace"
    AUTOMOBILE = "automobile"
    INSTRUMENTATION = "instrumentation"
    INDUSTRIAL_IOT = "industrial_iot"
    BIOTECHNOLOGY = "biotechnology"
    MATHEMATICS = "mathematics"
    OTHER = "other"


class QualificationExtractionMethod(StrEnum):
    """How a qualification fact was produced.

    A vocabulary of its own rather than a member added to
    app.schemas.skill's ExtractionMethod: that enum's members are the
    closed set `skill_evidence.extraction_method` is validated against,
    and widening it would change what a skill evidence row is allowed to
    claim. Two domains, two vocabularies.
    """

    # The person typed it. Carries no excerpt — see the model.
    MANUAL_ENTRY = "manual_entry"
    # Read from a labelled line in a resume by
    # app/qualifications/extract.py. Deterministic regex over a
    # controlled vocabulary; no model inference anywhere in it.
    RESUME_LABEL_MATCH = "resume_label_match"


# Bounds that are facts about the quantity, not policy: a percentage
# cannot exceed 100, and a CGPA scale of 0 would make every comparison a
# division by zero. Kept narrow enough to catch a typo, wide enough not
# to argue with an unfamiliar grading system.
_Percentage = Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]
_Cgpa = Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]
_Scale = Annotated[Decimal, Field(gt=0, le=100, decimal_places=2)]
_Year = Annotated[int, Field(ge=1950, le=2100)]
_Years = Annotated[Decimal, Field(ge=0, le=70, decimal_places=2)]


class QualificationFactDetail(BaseModel):
    """Where one fact came from, and whether the candidate has reviewed
    it.

    `excerpt` is a VERBATIM slice of the resume. A resume that wrote
    "CGPA: 8.2" keeps exactly that, even though the comparison scale
    defaults to 10 — the stored evidence says what the document said,
    never what the system concluded from it.
    """

    model_config = ConfigDict(from_attributes=True)

    status: CandidateSkillStatus
    source_type: EvidenceSourceType
    source_identifier: str | None = None
    excerpt: str | None = None
    extraction_method: QualificationExtractionMethod | None = None
    confidence: float | None = None


class QualificationsResponse(BaseModel):
    """Everything the candidate has declared, `null` where unknown."""

    model_config = ConfigDict(from_attributes=True)

    cgpa: Decimal | None = None
    # Reported beside the value, always. A CGPA without its scale is not
    # a weaker signal, it is an incomparable one.
    cgpa_scale: Decimal | None = None
    class_10_percentage: Decimal | None = None
    class_12_percentage: Decimal | None = None
    highest_degree: Degree | None = None
    field_of_study: FieldOfStudy | None = None
    graduation_year: int | None = None
    years_experience: Decimal | None = None
    updated_at: datetime | None = None
    # Per-fact provenance and review state, so the UI can render "from
    # your resume" beside a value and "Not found in your resume" where
    # there is none. Keyed by fact type; absent for anything unknown.
    facts: dict[str, QualificationFactDetail] = {}


class QualificationsUpdateRequest(BaseModel):
    """A partial update. An omitted field is left alone; an explicit
    `null` clears it back to unknown.

    `extra="forbid"` so a client that misspells a field, or tries to
    name an owner, gets a 422 rather than having it silently dropped —
    the same contract as the profile and saved-job request models.
    """

    model_config = ConfigDict(extra="forbid")

    cgpa: _Cgpa | None = None
    cgpa_scale: _Scale | None = None
    class_10_percentage: _Percentage | None = None
    class_12_percentage: _Percentage | None = None
    highest_degree: Degree | None = None
    field_of_study: FieldOfStudy | None = None
    graduation_year: _Year | None = None
    years_experience: _Years | None = None

    @model_validator(mode="after")
    def _scale_requires_a_value(self) -> "QualificationsUpdateRequest":
        """A scale on its own is meaningless, so reject it at the edge.

        The reverse is NOT rejected: a CGPA with no scale is a normal
        thing for someone to type, and the honest response is to store
        it and report UNDETERMINED against any threshold — not to refuse
        the input, and certainly not to pick a scale for them.
        """
        fields = self.model_fields_set
        if "cgpa_scale" in fields and self.cgpa_scale is not None:
            if "cgpa" in fields and self.cgpa is None:
                raise ValueError("cgpa_scale cannot be set while clearing cgpa")
        return self
