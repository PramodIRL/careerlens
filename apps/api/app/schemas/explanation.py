"""Response models for the structured explanation (Prompt 6.1).

TWO HALVES, AND THE SPLIT IS THE POINT.

  * The DETERMINISTIC half — `overall_score` and the three formula
    versions — is echoed straight out of the persisted facts and is
    present whether the explanation was accepted or rejected. No model
    produced any of it.
  * The GENERATED half — summary, strengths, gaps, next steps — is
    present only when `status` is "generated". A rejected explanation
    returns none of it: not a truncated version, not the claims that
    happened to pass.

`cited_evidence` is hydrated from the FACTS, not from the model, so
every excerpt a reader sees is a stored `skill_evidence` row. The model
chooses which rows to point at; it never supplies their text.
"""

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ExplanationStatus(StrEnum):
    GENERATED = "generated"
    REJECTED = "rejected"


class CitedEvidenceResponse(BaseModel):
    """One real evidence row an explanation pointed at."""

    model_config = ConfigDict(from_attributes=True)

    evidence_id: uuid.UUID
    source_type: str
    source_identifier: str
    excerpt: str | None


class ExplanationClaimResponse(BaseModel):
    """One generated sentence, and the rows it cites. Every id here is
    in `cited_evidence`, so the UI can render the claim beside the
    evidence without a second request."""

    model_config = ConfigDict(from_attributes=True)

    text: str
    evidence_ids: list[uuid.UUID] = []


class JobExplanationResponse(BaseModel):
    """A structured explanation of one saved job's match.

    NOT PERSISTED, like `/match` and `/gaps`: an explanation of a score
    goes stale the moment a skill is confirmed or a description edited,
    and a stored one would be a cache with no invalidation trigger.

    A REJECTION IS A NORMAL 200. The deterministic answer is intact and
    worth serving — the model failing to explain it is not an error in
    the match. `reason` says what was wrong in machine-readable terms
    and never quotes what the model actually said.
    """

    model_config = ConfigDict(from_attributes=True)

    status: ExplanationStatus
    # Machine-readable, null when generated. See
    # app/explanation/validate.py's RejectionReason for the values.
    reason: str | None = None
    schema_version: str
    provider: str

    # --- deterministic, echoed from persisted facts ------------------
    match_formula_version: str
    overall_score: int
    has_requirements: bool
    gap_formula_version: str
    semantic_formula_version: str

    # --- generated, absent when rejected -----------------------------
    summary: str | None = None
    strengths: list[ExplanationClaimResponse] = []
    gaps: list[ExplanationClaimResponse] = []
    next_steps: list[str] = []
    cited_evidence: list[CitedEvidenceResponse] = []
