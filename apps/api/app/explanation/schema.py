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

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

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


class VerdictFacts(BaseModel):
    """WHAT CAREERLENS DECIDED, said once and unambiguously (6.4b).

    DERIVED, NEVER SUPPLIED. `ExplanationFacts.verdict` is a COMPUTED
    field, not a stored one: every list here is a projection of
    `matched_skills`, `missing_required_skills` and
    `missing_other_skills`, grouped by the two axes the model kept
    confusing. No caller can pass one in, so no caller can pass one that
    disagrees with the skills it claims to summarise — the class of bug
    this whole block exists to catch is not one it can introduce.

    WHY IT EXISTS AT ALL, when the same information is already in the
    facts. It was in the facts and only in the facts — nested, two
    levels down, in the DATA block — while the instruction block carried
    one flat list of skill names with no status on it. On a job with no
    gaps a 7B model reached for that flat list to fill the "and here is
    the gap" half of a summary, and produced "REST APIs is a required
    skill you do not have" about a skill that was matched and merely
    mentioned. This block is what app/explanation/prompt.py renders as a
    per-request roster, so status travels at the same salience as the
    names.

    `has_any_gap` is the one the whole failure turned on. It is a
    BOOLEAN rather than something to infer from three empty lists,
    because "notice that all of these are empty" is exactly the
    inference that went wrong.
    """

    model_config = _FROZEN

    required_matched: list[str] = []
    preferred_matched: list[str] = []
    mentioned_matched: list[str] = []
    required_missing: list[str] = []
    preferred_missing: list[str] = []
    mentioned_missing: list[str] = []
    # True when ANY of the three missing lists holds a skill, or the
    # gap report named one. Never inferred, always stated.
    has_any_gap: bool = False

    def gap_names(self) -> frozenset[str]:
        """Every skill it is TRUE to call a gap."""
        return frozenset(self.required_missing + self.preferred_missing + self.mentioned_missing)

    def matched_names(self) -> frozenset[str]:
        """Every skill it is FALSE to call missing."""
        return frozenset(self.required_matched + self.preferred_matched + self.mentioned_matched)

    def required_names(self) -> frozenset[str]:
        """Every skill it is TRUE to call required."""
        return frozenset(self.required_matched + self.required_missing)

    def non_required_names(self) -> frozenset[str]:
        """Every skill it is FALSE to call required."""
        return frozenset(
            self.preferred_matched
            + self.mentioned_matched
            + self.preferred_missing
            + self.mentioned_missing
        )


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

    @model_validator(mode="before")
    @classmethod
    def _drop_computed(cls, data: object) -> object:
        """Serialised facts must round-trip back into this model.

        `verdict` is COMPUTED, so it appears in the JSON a provider
        receives — and app/explanation/provider.py's mock parses that
        JSON straight back into `ExplanationFacts` to render from. With
        `extra="forbid"` and no field to land in, that parse fails.

        Dropping the computed key on the way IN keeps dump-then-load an
        identity without weakening `extra="forbid"` for anything a
        caller could actually supply: a hand-passed `verdict` is
        discarded and recomputed from the skills, which is the whole
        guarantee `VerdictFacts` documents.
        """
        if isinstance(data, dict) and "verdict" in data:
            return {key: value for key, value in data.items() if key != "verdict"}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> VerdictFacts:
        """The decided status of every skill, one axis at a time.

        COMPUTED, so it travels in the serialised facts AND is available
        to the prompt and the validator without any of the three being
        able to hold a different answer. See `VerdictFacts` for what the
        6.4 browser test found when this information existed only in the
        nested data.

        `has_any_gap` reads `gaps` as well as the missing lists:
        `skill_gap_v1` is the authority on what counts as a gap, and a
        bucket it filled that `missing_skills` did not would otherwise
        go unstated. `needs_confirmation` is deliberately NOT counted —
        unreviewed evidence means the candidate may well have the skill,
        which is the opposite of a gap.
        """

        def named(facts: list[SkillFact], level: str) -> list[str]:
            return [fact.skill_name for fact in facts if fact.requirement_level == level]

        return VerdictFacts(
            required_matched=named(self.matched_skills, "required"),
            preferred_matched=named(self.matched_skills, "preferred"),
            mentioned_matched=named(self.matched_skills, "mentioned"),
            required_missing=[fact.skill_name for fact in self.missing_required_skills],
            preferred_missing=named(self.missing_other_skills, "preferred"),
            mentioned_missing=named(self.missing_other_skills, "mentioned"),
            has_any_gap=bool(
                self.missing_required_skills
                or self.missing_other_skills
                or self.gaps.required_gaps
                or self.gaps.preferred_gaps
                or self.gaps.informational_gaps
            ),
        )

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
    # THE SUMMARY IS TWO SLOTS, NOT ONE (Prompt 6.4b), and that is a
    # structural fix rather than a stylistic one.
    #
    # A summary is "where you stand" followed by "and here is the gap".
    # As ONE free-text field, the second half is always available to be
    # written — so on a job with no gaps at all a 7B model wrote one
    # anyway, inventing a required-and-missing skill out of a matched
    # one. No instruction removes that slot, because the slot is the
    # whole field.
    #
    # Split, the gap half can be PINNED TO NULL in the decoder when the
    # facts contain no gap (see app/explanation/ollama_provider.py), and
    # then the sentence is not something the model declined to write —
    # it is something it could not have written. Same move as
    # `strengths` with no evidence.
    #
    # The wire schema is unchanged: `summary` below rejoins them, and
    # app/api/v1/saved_job.py reads that.
    summary_fit: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    summary_gap: str | None = Field(default=None, max_length=MAX_SUMMARY_CHARS)
    strengths: list[ExplanationClaim] = Field(default=[], max_length=MAX_CLAIMS)
    gaps: list[ExplanationClaim] = Field(default=[], max_length=MAX_CLAIMS)
    next_steps: list[Annotated[str, Field(min_length=1, max_length=MAX_STEP_CHARS)]] = Field(
        default=[], max_length=MAX_STEPS
    )
    cited_evidence_ids: list[uuid.UUID] = []

    @property
    def summary(self) -> str:
        """The two halves as one paragraph, which is what a reader sees.

        A property rather than a field, so there is no third place the
        summary could be stored and no way for it to disagree with the
        halves the validator actually checked.
        """
        gap = (self.summary_gap or "").strip()
        return f"{self.summary_fit.strip()} {gap}".strip() if gap else self.summary_fit.strip()
