"""The two contracts: what goes in, and what may come back.

INPUT IS PERSISTED FACTS ONLY. There is no field on `ExplanationFacts`
for a resume, a README or a job description — so "raw text never reaches
the model" is a property of the type rather than a rule somebody has to
remember, the same way app/embeddings/retrieval.py's `SemanticHit`
carries no `satisfied` flag.

OUTPUT IS STRICT. `extra="forbid"` plus a bound on every string and
every list, because an unbounded field is how a validated schema still
ends up rendering three pages of model output into a dashboard.

`evidence` on the facts is the CITABLE UNIVERSE. An id that is not in
it cannot be cited — not because a check rejects it (though one does),
but because there is nowhere for it to have come from.
"""

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "match_explanation_v1"

# Bounds on generated text. Chosen to fit the panel this renders into,
# not to be generous: a summary longer than this is not a summary.
MAX_SUMMARY_CHARS = 600
MAX_CLAIM_CHARS = 300
MAX_STEP_CHARS = 200
MAX_CLAIMS = 5
MAX_STEPS = 5
MAX_CLAIM_CITATIONS = 5
# Ceiling on the raw provider response before anything is parsed.
MAX_RESPONSE_BYTES = 16_000

# --- input bounds (Prompt 6.2) ---------------------------------------
# The fact bundle is built from a candidate's OWN stored rows, and
# nothing bounded how many there were: a job touching ten skills, for a
# candidate with twenty imported repositories, could assemble a
# six-figure-character prompt. That is a cost, a latency and a
# context-flooding problem at once, so the bundle is capped here.
#
# Per skill first, so the cap is applied BEFORE `SkillFact.evidence_ids`
# is built and a fact can never cite a row the cap dropped. Rows arrive
# ordered by (created_at, id), so which three survive is deterministic.
MAX_EVIDENCE_PER_SKILL = 3
# Backstop across the whole bundle. 40 x 500 characters is ~20 KB of
# excerpt, which is a readable amount of evidence and a sane prompt.
MAX_EVIDENCE_ITEMS = 40
# Matches `skill_evidence.excerpt`'s column width. A no-op today, kept
# so the prompt stays bounded if that column ever grows.
MAX_EXCERPT_CHARS = 500


# --------------------------------------------------------------------
# Input — assembled from persisted rows by app/explanation/facts.py
# --------------------------------------------------------------------

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class EvidenceFact(BaseModel):
    """One real `skill_evidence` row, quotable and citable."""

    model_config = _FROZEN

    evidence_id: uuid.UUID
    source_type: str
    source_identifier: str
    excerpt: str | None


class SkillFact(BaseModel):
    """One job requirement, as the deterministic matcher resolved it."""

    model_config = _FROZEN

    skill_name: str
    requirement_level: str
    candidate_status: str | None = None
    candidate_rejected: bool = False
    evidence_ids: list[uuid.UUID] = []


class LevelFact(BaseModel):
    model_config = _FROZEN

    matched: int
    total: int


class ScoreFacts(BaseModel):
    """`skill_match_v1`'s output, copied verbatim. The model is told
    these numbers; it is never asked to produce or adjust one."""

    model_config = _FROZEN

    formula_version: str
    overall_score: int
    earned_weight: int
    obtainable_weight: int
    has_requirements: bool
    required_matched: int
    required_total: int
    by_level: dict[str, LevelFact]
    weights: dict[str, int]


class GapFacts(BaseModel):
    """`skill_gap_v1`'s five buckets, reduced to skill names."""

    model_config = _FROZEN

    formula_version: str
    required_gaps: list[str] = []
    preferred_gaps: list[str] = []
    informational_gaps: list[str] = []
    needs_confirmation: list[str] = []
    rejected_requirements: list[str] = []
    totals: dict[str, int] = {}


class SemanticHitFact(BaseModel):
    model_config = _FROZEN

    evidence_id: uuid.UUID
    similarity: float


class SemanticFacts(BaseModel):
    """`semantic_fit_v1`. NOT a calibrated score (see
    app/embeddings/semantic_fit.py) and the prompt says so, so the
    model cannot present it as one."""

    model_config = _FROZEN

    formula_version: str
    fit: int
    band: str
    model_identifier: str
    considered: int
    hits: list[SemanticHitFact] = []


class JobFacts(BaseModel):
    """Stored job metadata. `description` is deliberately absent."""

    model_config = _FROZEN

    saved_job_id: uuid.UUID
    title: str
    company: str


class ExplanationFacts(BaseModel):
    """Everything the model is allowed to know, and nothing else."""

    model_config = _FROZEN

    job: JobFacts
    score: ScoreFacts
    matched_skills: list[SkillFact] = []
    missing_required_skills: list[SkillFact] = []
    missing_other_skills: list[SkillFact] = []
    gaps: GapFacts
    semantic: SemanticFacts
    evidence: list[EvidenceFact] = []

    def evidence_by_id(self) -> dict[uuid.UUID, EvidenceFact]:
        return {row.evidence_id: row for row in self.evidence}

    def skill_names(self) -> frozenset[str]:
        """Every skill this job's facts mention — the only skills an
        explanation of them may name."""
        return frozenset(
            fact.skill_name
            for group in (
                self.matched_skills,
                self.missing_required_skills,
                self.missing_other_skills,
            )
            for fact in group
        )


# --------------------------------------------------------------------
# Output — what a provider must return, before grounding is checked
# --------------------------------------------------------------------


class ExplanationClaim(BaseModel):
    """One sentence about the candidate, and the rows behind it.

    A STRENGTH MUST CITE (enforced in validate.py): a claim that the
    candidate has done something, with nothing to point at, is exactly
    the invention this slice exists to prevent. A gap may cite nothing —
    an absence has no evidence row, and demanding one would invite the
    model to manufacture it.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_CLAIM_CHARS)
    evidence_ids: list[uuid.UUID] = Field(default=[], max_length=MAX_CLAIM_CITATIONS)


class MatchExplanation(BaseModel):
    """The validated explanation.

    `cited_evidence_ids` is REQUIRED of the provider and then REPLACED
    by the union validate.py recomputes from the claims. The model
    declaring its own citation set is part of the contract; the value
    the API returns is never the one it declared.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["match_explanation_v1"]
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    strengths: list[ExplanationClaim] = Field(default=[], max_length=MAX_CLAIMS)
    gaps: list[ExplanationClaim] = Field(default=[], max_length=MAX_CLAIMS)
    next_steps: list[Annotated[str, Field(min_length=1, max_length=MAX_STEP_CHARS)]] = Field(
        default=[], max_length=MAX_STEPS
    )
    cited_evidence_ids: list[uuid.UUID] = []
