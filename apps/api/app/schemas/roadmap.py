"""Request/response models for the learning roadmap (Prompt 6.3).

READ-ONLY PRESENTATION MODELS. Nothing below is persisted. A roadmap is
recomputed from current rows on every request, because a stored plan
asserting "AWS is required by 3 of your top 5 jobs" in a SENTENCE
becomes a false claim about the user's own data the moment two of those
jobs are deleted — and unlike a stale number, a stale paragraph still
reads like a fact.

THE DETERMINISTIC HALF AND THE WRITTEN HALF ARE SEPARABLE, on purpose.
`why`, `state`, `score`, `affected_jobs` and `phase` are computed by
app/roadmap/priority.py; `task` and `success_criteria` come from a
language model and are null when `narrative_status` is "rejected". A
client can render the whole plan without either trusting or receiving a
single generated word.
"""

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RoadmapGapState(StrEnum):
    """Three states, never collapsed to two. Weak is not missing: one
    needs learning, the other needs reviewable evidence."""

    MISSING_REQUIRED = "missing_required"
    MISSING_PREFERRED = "missing_preferred"
    WEAK_EVIDENCE = "weak_evidence"


class RoadmapAffectedJobResponse(BaseModel):
    """One selected job that wants this skill.

    `priority_rank` is the USER's ordering — the primary signal behind
    the item's score. `match_score` is `skill_match_v1` shown for
    context and is explicitly NOT part of `roadmap_priority_v1`: see
    app/roadmap/priority.py for why letting it weigh would invert the
    candidate's own stated intent.
    """

    model_config = ConfigDict(from_attributes=True)

    saved_job_id: uuid.UUID
    title: str
    company: str
    priority_rank: int
    match_score: int | None = None


class RoadmapEvidenceResponse(BaseModel):
    """A real stored evidence row, quoted rather than authored."""

    model_config = ConfigDict(from_attributes=True)

    evidence_id: uuid.UUID
    source_type: str
    source_identifier: str
    excerpt: str | None = None


class RoadmapItemResponse(BaseModel):
    """One thing to work on, with everything needed to justify it.

    `why`, `score`, `state_weight` and `recurrence` are echoed so a
    reader can check the arithmetic by hand — the same reason
    `JobMatchResponse` echoes its weights.

    `estimated_hours` IS AN ESTIMATE, derived by dividing the hours the
    user declared across the items that fit. CareerLens has no way to
    know how long learning a skill takes, and a client must present this
    as an estimate rather than a duration.
    """

    model_config = ConfigDict(from_attributes=True)

    item_id: str
    skill_id: uuid.UUID
    # WHAT TO LEARN — decided deterministically, never by a model.
    skill_name: str
    state: RoadmapGapState
    # Real days in the declared window, 1-based inclusive, decided by
    # `roadmap_schedule_v1`. The spans across a plan sum to exactly
    # `duration_days`.
    start_day: int
    end_day: int
    week: int
    score: int
    state_weight: int
    recurrence: int
    # Deterministic plain language, generated without a model.
    why: str
    affected_jobs: list[RoadmapAffectedJobResponse] = []
    evidence: list[RoadmapEvidenceResponse] = []
    estimated_hours: float = 0.0
    # Written by the provider, null when the narrative was rejected.
    # WHAT TO DO / WHAT YOU END UP WITH / WHAT YOU SHOULD BE ABLE TO DO.
    # `outcome` is an artefact, `success_criteria` a capability: "a
    # running service and a README" versus "you can explain why you
    # chose it". A plan with only the second is a reading list.
    task: str | None = None
    outcome: str | None = None
    success_criteria: str | None = None


class RoadmapWeekResponse(BaseModel):
    """One week of the plan, covering real days.

    The number of weeks is DERIVED from the declared duration rather
    than fixed at four: a 28-day plan is four weeks, a 14-day plan is
    two. Labelling four phases across an arbitrary window told a
    candidate with fourteen days that they had four weeks.

    `focus` and `checkpoint` are the only fields a model writes.
    `checkpoint` is an observable capability for the end of the week —
    "you should be able to explain..." — which is what a candidate can
    actually test themselves against.
    """

    model_config = ConfigDict(from_attributes=True)

    week: int
    label: str
    start_day: int
    end_day: int
    focus: str | None = None
    checkpoint: str | None = None
    items: list[RoadmapItemResponse] = []


class RoadmapResponse(BaseModel):
    """The candidate's current four-phase roadmap.

    ONE COMBINED PLAN, not one per job. It answers "what should I work
    on next, given everything I have saved", and an item routinely cites
    several jobs at once — which is the entire reason recurrence is part
    of the score.

    `narrative_status` is "generated" or "rejected". On rejection every
    deterministic field above is still present and only `task`,
    `success_criteria`, `overview` and `focus` are null: losing the
    wording must never cost the user the priorities.
    """

    model_config = ConfigDict(from_attributes=True)

    formula_version: str
    # Separate from `formula_version`: how the plan is laid out across
    # days can change without implying the priorities moved.
    schedule_version: str
    narrative_schema_version: str
    # "generated" | "rejected"
    narrative_status: str
    # Machine-readable rejection reason, sharing 6.1's vocabulary.
    reason: str | None = None
    provider: str
    selected_job_count: int
    # How many jobs the candidate has saved in total — the ceiling on
    # what they may select. Returned so the client's "N of X" control
    # re-bounds itself after a job is added or deleted.
    saved_job_count: int
    # False when the candidate has saved no jobs. A client must say "you
    # have not saved any jobs yet" rather than "no gaps found" — the
    # first is about the input, the second is a claim about the person.
    has_selected_jobs: bool
    duration_days: int
    hours_per_day: float
    # duration_days x hours_per_day. An input-derived budget, not a
    # prediction of how long anything takes.
    total_hours: float
    overview: str | None = None
    weeks: list[RoadmapWeekResponse] = []
