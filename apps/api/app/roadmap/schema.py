"""The roadmap's fact contract and the narrative a provider may return
(Prompt 6.3).

TWO SCHEMAS, POINTING OPPOSITE WAYS. `RoadmapFacts` is everything the
model is allowed to see; `RoadmapNarrative` is everything it is allowed
to say. Both are strict and both forbid extra keys, so the surface is
exactly what is written here.

THE NARRATIVE IS A MAP, NOT A LIST, AND THAT IS THE WHOLE SECURITY
DESIGN. Items are keyed by an `item_id` the deterministic ranker
produced, and the validator requires the returned key set to equal the
supplied one EXACTLY. A model cannot add a skill, drop one, or reorder
the plan — not because a rule forbids it, but because the response shape
has nowhere to express it. Compare app/explanation/schema.py, where the
model authors claims and every claim has to be checked: here the
decisions are already made and only the wording is open.

WHAT NEVER TRAVELS: no raw resume, no README, no job description. As in
6.1, there is simply no field for any of them.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "roadmap_narrative_v1"

# Bounds on what a provider may return. Small on purpose: this is a
# four-phase plan a person reads in one sitting, not a syllabus.
MAX_OVERVIEW_CHARS = 600
MAX_TASK_CHARS = 300
MAX_OUTCOME_CHARS = 300
MAX_CRITERIA_CHARS = 300
MAX_FOCUS_CHARS = 120
MAX_CHECKPOINT_CHARS = 300
# One step is a few days of work, so its prose is one or two sentences.
# Shorter than an item's, deliberately: a step that needs a paragraph is
# a step doing the item's job.
MAX_STEP_TASK_CHARS = 280
MAX_STEP_DONE_CHARS = 200
# A plan holds at most MAX_ITEMS items, each with at most
# MAX_STEPS_PER_ITEM steps — but the day arithmetic bounds it far below
# that product, because steps are carved out of a window of at most 56
# days. This is the ceiling the response schema declares; a test pins
# that the scheduler cannot reach it.
MAX_STEPS = 32
MAX_RESPONSE_BYTES = 32_000

# Weeks are derived from the declared duration, so the ceiling follows
# from the longest window the API accepts (56 days).
MAX_WEEKS = 8

# Mirrors app/explanation/schema.py: excerpts are third-party text and
# the bundle must not grow with somebody's import history.
MAX_EVIDENCE_PER_ITEM = 3
MAX_EXCERPT_CHARS = 500


class RoadmapJobFact(BaseModel):
    """One selected job, as the model may refer to it.

    `rank` is the USER's ordering. `match_score` is carried for display
    and is explicitly not part of `roadmap_priority_v1` — see
    app/roadmap/priority.py for why letting it weigh here would invert
    the candidate's stated intent.

    THE DESCRIPTION IS ABSENT, deliberately. A job's title and company
    are how a person recognises which posting an item is about; the
    posting's prose is not needed for that and does not travel.
    """

    model_config = ConfigDict(extra="forbid")

    saved_job_id: uuid.UUID
    title: str
    company: str
    rank: int
    match_score: int | None = None


class RoadmapEvidenceFact(BaseModel):
    """One stored evidence row an item may cite.

    UNTRUSTED CONTENT. `excerpt` is a slice of a resume or a README
    written by somebody else — see app/roadmap/prompt.py for how it is
    delimited and why the real protection is on the way back.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: uuid.UUID
    source_type: str
    source_identifier: str
    excerpt: str | None = None


class RoadmapStepFact(BaseModel):
    """One block of days inside an item, and its learning MODE.

    EVERYTHING HERE IS DECIDED. The days come from
    `roadmap_schedule_v2`, the phase from the deterministic ladder in
    app/roadmap/priority.py. The model writes what to actually DO in
    these days — which concepts, which exercise, which build — and can
    move nothing.

    THE PHASE IS A BOUNDARY, NOT A LESSON PLAN. "practice" says this
    block is for practising; it does not say on what. That choice is
    the mentoring value the model adds, and constraining it further
    would be CareerLens pretending to a pedagogy it has not earned.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str
    # "learn" | "practice" | "build" | "prove" | "self_check"
    # | "demonstrate" | "document"
    phase: str
    start_day: int
    end_day: int
    week: int
    estimated_hours: float = 0.0


class RoadmapItemFact(BaseModel):
    """One decided roadmap item the model writes prose for.

    Everything here is settled before the model runs: which skill, what
    kind of gap, which phase, which jobs, and the plain-language `why`.
    The model contributes a task and a success criterion and nothing
    else.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    skill_name: str
    # "missing_required" | "missing_preferred" | "weak_evidence"
    state: str
    # Real days in the candidate's declared window, 1-based inclusive,
    # decided by `roadmap_schedule_v2`. The model is told when the work
    # happens; it does not get to move it.
    start_day: int
    end_day: int
    week: int
    score: int
    # Deterministic, generated by app/roadmap/priority.py. The model may
    # rephrase around it but may not contradict it — every number in it
    # is in the facts and therefore checkable.
    why: str
    affected_job_ids: list[uuid.UUID] = []
    evidence_ids: list[uuid.UUID] = []
    # An ESTIMATE derived from the hours the user declared, divided
    # across the items that fit. Never presented as a measurement.
    estimated_hours: float = 0.0
    # The day-level decomposition. Sums to this item's own span, so a
    # model reading the facts cannot infer any time it does not have.
    steps: list[RoadmapStepFact] = []


class RoadmapPlanFacts(BaseModel):
    """The shape of the plan, as the user configured it."""

    model_config = ConfigDict(extra="forbid")

    formula_version: str
    schedule_version: str
    selected_job_count: int
    duration_days: int
    hours_per_day: float
    total_hours: float
    # Derived from the duration, not a fixed four.
    weeks: int
    # The weeks that actually HOLD WORK, which is not the same thing.
    # A 28-day plan spans four weeks; before 6.4b two items scheduled
    # across it registered in weeks 1 and 3 only, so a model told
    # "weeks: 4" themed all four and was rejected for naming weeks the
    # schedule never used. Weeks are now derived from STEPS, which never
    # straddle a boundary, so this list is the weeks a candidate really
    # has work in. `_check_weeks` still verifies it independently.
    work_weeks: list[int] = []
    # How many of the declared days the plan actually fills, and how
    # many it does not. An honest deterministic fact: when the
    # candidate's saved jobs do not justify the window they asked for,
    # the remainder is reported rather than padded with invented work.
    scheduled_days: int = 0
    unscheduled_days: int = 0
    # "full" | "partial"
    coverage: str = "full"


class RoadmapFacts(BaseModel):
    """Everything a provider is allowed to see, and nothing else.

    Jobs outside the user's Top-N are absent — not filtered late, but
    never assembled (app/roadmap/facts.py). A model cannot cite a job it
    was never given an id for.
    """

    model_config = ConfigDict(extra="forbid")

    plan: RoadmapPlanFacts
    jobs: list[RoadmapJobFact] = []
    items: list[RoadmapItemFact] = []
    evidence: list[RoadmapEvidenceFact] = []

    def item_ids(self) -> set[str]:
        return {item.item_id for item in self.items}

    def step_ids(self) -> set[str]:
        return {step.step_id for item in self.items for step in item.steps}

    def week_numbers(self) -> set[int]:
        """Every week that actually holds work.

        FROM STEPS, NOT FROM ITEM START DAYS (Prompt 6.4b). An item
        running days 1-14 is work in weeks 1 AND 2; keying off its start
        day filed it under week 1 alone and left week 2 rendering
        "nothing scheduled" while the candidate was meant to be working.
        Steps never straddle a boundary, so this is exact.

        A week with no step in it still gets no narrative entry — asking
        a model to theme an empty week invites it to invent something to
        put there.
        """
        return {step.week for item in self.items for step in item.steps}

    def job_ids(self) -> set[uuid.UUID]:
        return {job.saved_job_id for job in self.jobs}

    def evidence_ids(self) -> set[uuid.UUID]:
        return {row.evidence_id for row in self.evidence}

    def skill_names(self) -> set[str]:
        return {item.skill_name for item in self.items}


class NarrativeItem(BaseModel):
    """The model's contribution for ONE already-decided item.

    No skill name, no state, no phase, no score, no job list. Those are
    decided, and a field for them would be a field to disagree through.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    # WHAT TO DO. Observable work, not "learn X" — the instruction asks
    # for it and nothing here can enforce prose quality. What IS
    # enforced is that the sentence invents no skill, no number and no
    # link.
    task: str = Field(min_length=1, max_length=MAX_TASK_CHARS)
    # WHAT YOU END UP WITH. An artefact — a deployed service, a written
    # page, a test suite. Distinct from `success_criteria`, which is a
    # capability: "a running service and a README" is the outcome, "you
    # can explain why you chose it" is the criterion. A plan with only
    # the second is a reading list.
    outcome: str = Field(min_length=1, max_length=MAX_OUTCOME_CHARS)
    # WHAT YOU SHOULD BE ABLE TO DO. Checkable by the candidate alone.
    success_criteria: str = Field(min_length=1, max_length=MAX_CRITERIA_CHARS)


class NarrativeStep(BaseModel):
    """The model's contribution for ONE already-decided block of days.

    No phase, no days, no hours, no ordering — those are decided, and a
    field for them would be a field to disagree through. What IS the
    model's: which concepts this block covers, which exercise, what gets
    built. That is the mentoring judgement the deterministic layer has
    no basis to make.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str
    # WHAT TO DO IN THESE DAYS. Specific enough to start this morning:
    # the sub-topics by name, and the thing they are practised on.
    task: str = Field(min_length=1, max_length=MAX_STEP_TASK_CHARS)
    # THE FINISH LINE FOR THIS BLOCK — observable, so "am I on track"
    # has an answer before the item is over.
    done_when: str = Field(min_length=1, max_length=MAX_STEP_DONE_CHARS)


class NarrativeWeek(BaseModel):
    """A theme and a checkpoint for one week.

    PHRASING ONLY. Which weeks exist, and which items fall in each, were
    decided by `roadmap_schedule_v1` before the model ran — and the
    validator requires the returned week numbers to match that set
    exactly, so inventing a week or dropping one is unrepresentable.

    `checkpoint` is the observable-capability question: "by the end of
    this week you should be able to explain...". It is what turns a list
    of tasks into something a candidate can test themselves against, and
    it is grounded like every other sentence here.
    """

    model_config = ConfigDict(extra="forbid")

    week: int = Field(ge=1, le=MAX_WEEKS)
    focus: str = Field(min_length=1, max_length=MAX_FOCUS_CHARS)
    checkpoint: str = Field(min_length=1, max_length=MAX_CHECKPOINT_CHARS)


class RoadmapNarrative(BaseModel):
    """A validated narrative for a plan that was already decided."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(pattern=f"^{SCHEMA_VERSION}$")
    overview: str = Field(min_length=1, max_length=MAX_OVERVIEW_CHARS)
    weeks: list[NarrativeWeek] = Field(default=[], max_length=MAX_WEEKS)
    items: list[NarrativeItem] = Field(default=[], max_length=16)
    steps: list[NarrativeStep] = Field(default=[], max_length=MAX_STEPS)
