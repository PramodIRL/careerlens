"""What the candidate should work on, and why (Prompt 6.3).

PURE: selected jobs and their gaps in, a ranked roadmap out. No
database, no network, no LLM, no model inference — the same split
app/matching/score.py has from app/api/v1/saved_job.py, and for the same
reason: a priority nobody can reproduce on paper is a priority nobody
can argue with.

    roadmap_priority_v1
    ===================
    state weight (WHAT KIND OF GAP — the dominant term)
        missing_required   100
        missing_preferred   50
        weak_evidence       25

    job weight (WHICH OF YOUR JOBS ASK FOR IT — by YOUR rank)
        rank 1  5    rank 2  4    rank 3  3    rank 4  2    rank 5+  1

    recurrence = min(sum of job weights over selected jobs, 24)
    score      = state_weight + recurrence

THE BANDS CANNOT CROSS, AND THAT IS THE POINT. The recurrence cap is one
less than the narrowest gap between two state weights (25, between
preferred and weak), so recurrence orders items WITHIN a band and can
never lift one out of it. Required lands in 100-124, preferred in 50-74,
weak in 25-49. A preferred skill wanted by every job you saved still
ranks below a required skill wanted by one — which is the product rule,
now a property of the arithmetic rather than a hope about the inputs.

Without the cap this breaks at scale rather than obviously: with "All
saved jobs" and fifty of them, an uncapped sum reaches 5+4+3+2+46 = 60
and a merely-preferred skill overtakes a required one. The cap is why
test 4 holds under recurrence stress instead of only on small fixtures.

WHAT THIS FORMULA DELIBERATELY DOES NOT DO:

  * It does NOT read `skill_match_v1`. That number answers "how well do
    I currently match this job"; the user's ordering answers "which of
    these do I want". Letting the first weigh on the second demotes a
    job the candidate ranked FIRST because they happen to match it
    poorly, which inverts their stated intent. The direction is not even
    obvious — a low match means MORE to learn, so a weight would be
    picking a side the data does not support. The score is displayed per
    affected job, where a person can read it, and is absent from the
    arithmetic here.

  * It does NOT read `confidence`. That is match quality — "does this
    string denote this skill" — and app/matching/score.py and
    app/schemas/skill.py both refuse to treat it as capability. Ranking
    a learning plan by it would make exactly the claim those two files
    document against.

  * It does NOT rank a REJECTED requirement. The user disowned that
    skill; putting it back in a plan overrides their own decision.

  * It does NOT let time reorder anything. Duration and hours-per-day
    decide how many items survive the cut and where each one sits in
    the calendar (see `schedule_items`), never which item is first. The
    same gap is rank 1 at ten hours and at forty.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

# Bumped whenever the arithmetic changes — including the weights. A
# priority is only reproducible if you know which rule produced it.
FORMULA_VERSION = "roadmap_priority_v1"


class GapState(StrEnum):
    """What kind of gap this is. Three states, never collapsed to two.

    WEAK IS NOT MISSING, and the difference decides what the plan should
    ask for: a missing skill needs learning, a weakly evidenced one
    needs EVIDENCE — the candidate may well have it already and simply
    never reviewed what an extractor found.
    """

    MISSING_REQUIRED = "missing_required"
    MISSING_PREFERRED = "missing_preferred"
    WEAK_EVIDENCE = "weak_evidence"


# Ordinal, not measured — the same kind of judgement app/matching/
# score.py's WEIGHTS are, and stated as such. The GAPS between them are
# the load-bearing part: each is wider than MAX_RECURRENCE (below), so
# no amount of recurrence moves an item between bands.
STATE_WEIGHTS: dict[GapState, int] = {
    GapState.MISSING_REQUIRED: 100,
    GapState.MISSING_PREFERRED: 50,
    GapState.WEAK_EVIDENCE: 25,
}

# Which of YOUR jobs asks for it, weighted by the order YOU put them in.
# Beyond rank 5 every job contributes 1: a candidate's fifth and
# fifteenth choices are both "jobs I would take", and pretending this
# ordering is precise that far down would be false precision.
JOB_RANK_WEIGHTS: tuple[int, ...] = (5, 4, 3, 2, 1)
_TAIL_WEIGHT = 1

# One less than the narrowest gap between two state weights (50 - 25).
# This single constant is what makes the bands non-overlapping.
MAX_RECURRENCE = 24

# Ordinal precedence when several selected jobs want the same skill at
# different levels: the STRONGEST demand wins outright rather than being
# averaged with the others. Same "select, don't blend" rule
# app/skill_matching.py applies to match kinds and app/api/v1/
# skill_profile.py applies to confidence.
_STATE_PRECEDENCE: dict[GapState, int] = {
    GapState.MISSING_REQUIRED: 0,
    GapState.MISSING_PREFERRED: 1,
    GapState.WEAK_EVIDENCE: 2,
}

# A PLANNING ASSUMPTION, NOT A MEASUREMENT. CareerLens has no idea how
# long it takes anyone to learn AWS, and this constant must never be
# presented as though it did — the API labels every derived figure an
# estimate, and the UI says so. It exists only to turn declared hours
# into a sane number of items.
HOURS_PER_ITEM = 3.0
# Floors and ceilings on that conversion. Four so every week has
# something in it; twelve because a plan with more items than a person
# can hold in their head is a list, not a plan.
MIN_ITEMS = 4
MAX_ITEMS = 12

# Bumped whenever the SCHEDULING changes. Separate from
# FORMULA_VERSION on purpose: how the plan is laid out across days can
# evolve without implying the priorities moved, and conflating the two
# would make every layout tweak look like a re-ranking.
#
# v2 (Prompt 6.4b) splits each item into day-level STEPS and caps how
# many days one item may occupy. The priorities are byte-identical; only
# the calendar changed, which is exactly what a separate version is for.
SCHEDULE_VERSION = "roadmap_schedule_v2"

DAYS_PER_WEEK = 7

# THE LONGEST ONE SKILL MAY OCCUPY. A PLANNING ASSUMPTION, NOT A
# MEASUREMENT — the same kind of judgement HOURS_PER_ITEM is, and it
# must never be presented as though CareerLens knew how long learning
# anything takes.
#
# It exists because the previous behaviour divided the whole window
# across however many items survived, so two gaps over eight weeks
# became two four-week blocks. Four weeks on one skill is not a plan a
# mentor would write; it is the arithmetic running out of material and
# padding with time. Two weeks is where a focused push on a single skill
# stops being focused.
#
# WHEN THE MATERIAL RUNS OUT, THE PLAN SAYS SO. The days beyond what the
# items can carry are reported as `unscheduled_days` rather than
# absorbed, because inventing learning requirements to fill a window the
# candidate's own jobs did not justify is exactly the fabrication this
# product refuses everywhere else.
MAX_DAYS_PER_ITEM = 14

# The target length of ONE step, in days. Also a planning assumption.
# Three days is short enough that "what should I do today" has an
# answer and long enough that a step is a piece of work rather than a
# checkbox.
DAYS_PER_STEP = 3


class StepPhase(StrEnum):
    """The learning MODE of one step. Deterministic, never model-chosen.

    THE LADDER IS THE BOUNDARY, NOT THE LESSON. A phase says what kind
    of work belongs in these days — take something in, practise it,
    build with it, produce the artefact, test yourself — and stops
    there. WHICH concepts, WHICH exercise, WHAT gets built and WHICH
    questions are the mentoring judgement, and that is the model's to
    make inside the boundary (see app/roadmap/prompt.py).

    Deciding the mode here rather than in the prompt buys three things:
    the UI can render the phase badge without trusting a model, the
    sequence cannot be reordered or invented, and the weak-evidence rule
    below is enforced by code rather than by a sentence a model may skip.
    """

    LEARN = "learn"
    PRACTICE = "practice"
    BUILD = "build"
    PROVE = "prove"
    SELF_CHECK = "self_check"
    # WEAK EVIDENCE GETS ITS OWN RUNGS. The candidate may already have
    # the skill and only lacks reviewed evidence for it, so a ladder
    # starting at LEARN would tell them to learn something they can
    # already do. Same distinction GapState draws, carried through to
    # the calendar.
    DEMONSTRATE = "demonstrate"
    DOCUMENT = "document"


# WHICH RUNGS, FOR A GIVEN NUMBER OF STEPS. Indexed by `n - 1`, so a
# lookup returns exactly `n` phases and the table's own length is the
# ceiling on how many steps that state can hold.
#
# AN EXPLICIT TABLE, NOT AN ALGORITHM. A subsequence rule that "drops
# from the middle" is three lines shorter and impossible to check by
# eye; this can be read straight down. The shapes are the product
# judgement and belong somewhere a reader can disagree with them.
#
# Note what a ONE-step missing item gets: BUILD, not LEARN. With a
# single block of days the honest instruction is to build something
# small with the skill, because "spend your only day reading" produces
# nothing anybody can look at afterwards.
_PHASE_PLAN: dict[GapState, tuple[tuple[StepPhase, ...], ...]] = {
    GapState.MISSING_REQUIRED: (
        (StepPhase.BUILD,),
        (StepPhase.LEARN, StepPhase.BUILD),
        (StepPhase.LEARN, StepPhase.BUILD, StepPhase.PROVE),
        (StepPhase.LEARN, StepPhase.PRACTICE, StepPhase.BUILD, StepPhase.PROVE),
        (
            StepPhase.LEARN,
            StepPhase.PRACTICE,
            StepPhase.BUILD,
            StepPhase.PROVE,
            StepPhase.SELF_CHECK,
        ),
    ),
    GapState.MISSING_PREFERRED: (
        (StepPhase.BUILD,),
        (StepPhase.LEARN, StepPhase.BUILD),
        (StepPhase.LEARN, StepPhase.BUILD, StepPhase.PROVE),
        (StepPhase.LEARN, StepPhase.PRACTICE, StepPhase.BUILD, StepPhase.PROVE),
        (
            StepPhase.LEARN,
            StepPhase.PRACTICE,
            StepPhase.BUILD,
            StepPhase.PROVE,
            StepPhase.SELF_CHECK,
        ),
    ),
    # Three rungs, and the table's length is the cap: a skill that needs
    # EVIDENCE rather than learning does not need five stages of it.
    GapState.WEAK_EVIDENCE: (
        (StepPhase.DOCUMENT,),
        (StepPhase.DEMONSTRATE, StepPhase.DOCUMENT),
        (StepPhase.DEMONSTRATE, StepPhase.DOCUMENT, StepPhase.PROVE),
    ),
}

# The longest ladder in the table. Used as a bound where the state is
# not yet known; the per-state length is what actually applies.
MAX_STEPS_PER_ITEM = max(len(plans) for plans in _PHASE_PLAN.values())


def phases_for(state: GapState, count: int) -> tuple[StepPhase, ...]:
    """Exactly `count` phases for one item, or as many as the state has.

    Raises on a count below one: a scheduled item always occupies at
    least one day, so a zero-step item is a bug in the caller rather
    than a case to paper over.
    """
    if count < 1:  # pragma: no cover - defensive; spans are >= 1 day
        raise ValueError(f"an item needs at least one step, got {count}")
    plans = _PHASE_PLAN[state]
    return plans[min(count, len(plans)) - 1]


def max_steps_for(state: GapState) -> int:
    """How many steps this state's ladder can express."""
    return len(_PHASE_PLAN[state])


@dataclass
class RoadmapStep:
    """One block of days inside an item, with its learning mode.

    EVERY FIELD HERE IS DETERMINISTIC. The days come from
    `roadmap_schedule_v2`, the phase from the ladder above, the hours
    from the candidate's own declared time. A model writes prose against
    `step_id` and changes none of it.

    A STEP NEVER STRADDLES A WEEK. `schedule_items` splits on week
    boundaries before it splits on length, so `week` is exact rather
    than approximated from a start day — which is what stopped a
    fortnight-long item from being filed under its first week alone and
    leaving the second looking empty.
    """

    step_id: str
    phase: StepPhase
    start_day: int
    end_day: int
    week: int
    # An ESTIMATE, from the candidate's own declared hours. Never a
    # claim about how long anything takes.
    estimated_hours: float = 0.0


@dataclass(frozen=True)
class SelectedJob:
    """One job inside the user's Top-N, with the rank THEY gave it.

    `rank` is 1-based and comes from the user's own ordering. It is not
    derived from any score, and nothing in this module recomputes it.
    """

    saved_job_id: uuid.UUID
    title: str
    company: str
    rank: int
    # Carried for display beside the item, never used in the arithmetic.
    match_score: int | None = None


@dataclass(frozen=True)
class JobDemand:
    """One selected job's demand for one skill."""

    saved_job_id: uuid.UUID
    state: GapState


@dataclass
class RoadmapItem:
    """One thing to work on, and everything needed to justify it.

    `why` is generated HERE, deterministically, from the same numbers
    that produced `score`. No model writes it, so a reader can check the
    claim against the roadmap's own inputs — which is what lets somebody
    use this plan without trusting a language model at all.
    """

    skill_id: uuid.UUID
    skill_name: str
    state: GapState
    score: int
    state_weight: int
    recurrence: int
    # Highest-priority first, so the UI can name the top job directly.
    affected_jobs: list[SelectedJob] = field(default_factory=list)
    evidence_ids: list[uuid.UUID] = field(default_factory=list)
    why: str = ""
    # Filled by `schedule_items`. 1-based and inclusive, so an item on
    # days 1-4 really occupies four days of the declared window.
    start_day: int = 0
    end_day: int = 0
    week: int = 0
    estimated_hours: float = 0.0
    # The day-level decomposition, filled by `schedule_items`. Sums to
    # this item's own span by construction.
    steps: list[RoadmapStep] = field(default_factory=list)

    @property
    def item_id(self) -> str:
        """The key the LLM fills prose in against.

        The canonical skill id as a string. Stable across two identical
        requests, and — because the narrative is a MAP from these ids —
        the reason a model cannot add, drop or reorder an item.
        """
        return str(self.skill_id)


def job_weight(rank: int) -> int:
    """What one job at `rank` contributes to a skill it demands."""
    if rank <= 0:  # pragma: no cover - defensive; ranks are 1-based
        raise ValueError(f"rank must be 1-based, got {rank}")
    if rank <= len(JOB_RANK_WEIGHTS):
        return JOB_RANK_WEIGHTS[rank - 1]
    return _TAIL_WEIGHT


def _strongest(states: list[GapState]) -> GapState:
    return min(states, key=lambda state: _STATE_PRECEDENCE[state])


def _describe(item: RoadmapItem, selected_count: int) -> str:
    """The plain-language reason, built from the item's own numbers.

    Deliberately says what the FACTS say and stops. No advice, no
    encouragement, and no claim about the candidate's ability.
    """
    jobs = len(item.affected_jobs)
    plural = "job" if jobs == 1 else "jobs"
    if item.state is GapState.WEAK_EVIDENCE:
        opening = f"{item.skill_name} has evidence you have not reviewed yet"
        demand = f"and is wanted by {jobs} of your {selected_count} selected {plural}"
    else:
        level = "required" if item.state is GapState.MISSING_REQUIRED else "preferred"
        opening = f"{item.skill_name} is missing"
        demand = f"and is {level} by {jobs} of your {selected_count} selected {plural}"

    top = item.affected_jobs[0] if item.affected_jobs else None
    context = f", including your #{top.rank} priority ({top.company} — {top.title})" if top else ""
    return f"{opening} {demand}{context}."


def rank_items(
    demands: dict[uuid.UUID, list[JobDemand]],
    *,
    skill_names: dict[uuid.UUID, str],
    selected_jobs: list[SelectedJob],
    evidence_ids: dict[uuid.UUID, list[uuid.UUID]] | None = None,
) -> list[RoadmapItem]:
    """Score every gap and return them highest-priority first.

    Deterministic and order-independent: the result is built from sums
    and a total ordering with a name tie-break, so shuffling the input
    cannot change any output. Two identical requests return
    byte-identical JSON without the caller sorting anything.
    """
    by_id = {job.saved_job_id: job for job in selected_jobs}
    evidence_ids = evidence_ids or {}
    items: list[RoadmapItem] = []

    for skill_id, skill_demands in demands.items():
        # A demand from a job outside the selection is not filtered here
        # — it never reaches this function. app/roadmap/facts.py drops
        # unselected jobs before aggregation, so an excluded job cannot
        # contribute a weight, appear as an affected job, or reach the
        # model's input at all.
        relevant = [demand for demand in skill_demands if demand.saved_job_id in by_id]
        if not relevant:
            continue

        state = _strongest([demand.state for demand in relevant])
        state_weight = STATE_WEIGHTS[state]
        raw_recurrence = sum(job_weight(by_id[demand.saved_job_id].rank) for demand in relevant)
        recurrence = min(raw_recurrence, MAX_RECURRENCE)

        affected = sorted(
            (by_id[demand.saved_job_id] for demand in relevant), key=lambda job: job.rank
        )
        items.append(
            RoadmapItem(
                skill_id=skill_id,
                skill_name=skill_names.get(skill_id, ""),
                state=state,
                score=state_weight + recurrence,
                state_weight=state_weight,
                recurrence=recurrence,
                affected_jobs=affected,
                evidence_ids=list(evidence_ids.get(skill_id, [])),
            )
        )

    # Score, then more jobs, then the best rank reached, then name. The
    # last is not cosmetic: without a total order two equal items could
    # swap between identical requests.
    items.sort(
        key=lambda item: (
            -item.score,
            -len(item.affected_jobs),
            item.affected_jobs[0].rank if item.affected_jobs else 0,
            item.skill_name,
        )
    )
    for item in items:
        item.why = _describe(item, len(selected_jobs))
    return items


def item_budget(total_hours: float) -> int:
    """How many items the declared HOURS can carry.

    THE ONLY THING TIME IS ALLOWED TO DO. It sets the size of the plan;
    it never touches the order, because the list is truncated from the
    BOTTOM. The top-ranked gap is the top-ranked gap at ten hours and at
    forty — what changes is how much else survives with it.
    """
    return max(MIN_ITEMS, min(MAX_ITEMS, round(total_hours / HOURS_PER_ITEM)))


def week_count(duration_days: int) -> int:
    """How many weeks the declared window spans.

    DERIVED, NOT FIXED. A 28-day plan is four weeks — the shape the
    product brief describes — but a 14-day plan is two and a 56-day plan
    is eight. Four labelled phases stretched over an arbitrary duration
    was the previous behaviour and it was a label, not a schedule: it
    told a candidate with fourteen days that they had four weeks.
    """
    return -(-duration_days // DAYS_PER_WEEK)  # ceiling division


@dataclass(frozen=True)
class Schedule:
    """A laid-out plan, and an honest statement of what it does not fill.

    `unscheduled_days` is a DETERMINISTIC FACT, not an apology. When the
    candidate's own saved jobs produce two gaps and they ask for eight
    weeks, the material genuinely runs out — and the two available
    answers are to stretch one skill across a month or to say so. This
    says so, and app/api/v1/roadmap.py reports it beside the plan.
    """

    items: list["RoadmapItem"]
    duration_days: int
    scheduled_days: int
    unscheduled_days: int

    @property
    def coverage(self) -> str:
        """ "full" when the items fill the declared window, else
        "partial". Derived rather than stored, so the two can never
        disagree."""
        return "full" if self.unscheduled_days == 0 else "partial"


def _week_segments(start_day: int, end_day: int) -> list[tuple[int, int, int]]:
    """Split a day range at WEEK boundaries. `(start, end, week)`.

    THE REASON EVERY STEP HAS AN EXACT WEEK. Splitting here, before
    length is considered, means no step can span a boundary — so a
    week's contents are the steps whose week equals it, with no
    overlap rule and no item filed under a week it only starts in.
    That last case is the whole 6.4b defect: a fortnight-long item
    reported week 1 and left week 2 rendering "nothing scheduled" while
    the candidate was in fact meant to be working on it.
    """
    segments: list[tuple[int, int, int]] = []
    cursor = start_day
    while cursor <= end_day:
        week = (cursor - 1) // DAYS_PER_WEEK + 1
        segments.append((cursor, min(week * DAYS_PER_WEEK, end_day), week))
        cursor = segments[-1][1] + 1
    return segments


def _step_counts(segments: list[tuple[int, int, int]], target: int) -> list[int]:
    """How many steps each week-segment gets, summing to `target`.

    Every segment starts with one — a week holding work always shows
    work — and the remaining budget goes to whichever segment currently
    has the most days per step. That is the "longest first" rule stated
    as a ratio, which keeps step lengths even across uneven segments
    instead of loading the first one.

    A segment never gets more steps than it has days: a zero-day step
    would be a row in the UI with no time attached to it.
    """
    counts = [1] * len(segments)
    for _ in range(max(target - len(segments), 0)):
        best: tuple[float, int] | None = None
        for index, (start, end, _week) in enumerate(segments):
            days = end - start + 1
            if counts[index] >= days:
                continue
            ratio = days / counts[index]
            # Strictly greater, so ties go to the EARLIER segment and
            # the result is order-stable across identical requests.
            if best is None or ratio > best[0]:
                best = (ratio, index)
        if best is None:
            break
        counts[best[1]] += 1
    return counts


def _build_steps(item: "RoadmapItem", *, hours_per_day: float) -> list[RoadmapStep]:
    """Decompose one scheduled item into day-level steps.

    THE DAYS TILE THE ITEM EXACTLY. Each segment is divided with the
    same `divmod` the item layout uses, so the steps of an item sum to
    its span, the items sum to the scheduled days, and the total
    estimated hours cannot exceed `duration_days x hours_per_day`. The
    bound is arithmetic, not a check somebody has to remember to run.

    HOW MANY STEPS is a function of the span and the state's ladder, and
    of nothing else — not of the model, and not of how much prose fits.
    """
    segments = _week_segments(item.start_day, item.end_day)
    span = item.end_day - item.start_day + 1
    target = max(
        len(segments),
        min(max_steps_for(item.state), max(round(span / DAYS_PER_STEP), 1)),
    )
    counts = _step_counts(segments, target)

    steps: list[RoadmapStep] = []
    phases = phases_for(item.state, sum(counts))
    for (start, end, week), count in zip(segments, counts, strict=True):
        base, extra = divmod(end - start + 1, count)
        cursor = start
        for index in range(count):
            length = base + (1 if index < extra else 0)
            steps.append(
                RoadmapStep(
                    step_id=f"{item.item_id}:{len(steps) + 1}",
                    phase=phases[len(steps)],
                    start_day=cursor,
                    end_day=cursor + length - 1,
                    week=week,
                    estimated_hours=round(length * hours_per_day, 1),
                )
            )
            cursor += length
    return steps


def schedule_items(
    items: list[RoadmapItem],
    *,
    duration_days: int,
    hours_per_day: float,
) -> Schedule:
    """Trim to the budget and lay the survivors across the real calendar.

    DAYS ARE DISTRIBUTED, NOT HOURS, and that is what makes the window
    exact: the spans are `divmod(scheduled, n)` so they sum to
    `scheduled` by construction. There is no overflow past what the
    candidate declared, and `estimated_hours` follows from the span
    rather than being computed separately and hoped to agree. Dividing
    HOURS instead — the original approach — could round each item up
    past the window at low hours-per-day.

    NO ITEM MAY EXCEED `MAX_DAYS_PER_ITEM` (Prompt 6.4b). The days the
    items cannot carry are left UNSCHEDULED and reported, rather than
    divided into blocks nobody would call a plan. `base` is therefore
    bounded by fourteen, and so is `base + 1`: a remainder exists only
    when `scheduled` is short of `14 x n`, which is exactly when `base`
    is at most thirteen.

    AN ITEM NEEDS AT LEAST ONE DAY, so the count is additionally bounded
    by `duration_days`. Without that a twelve-item budget over seven
    days divides to a span of zero and every item starts on day one.

    THE REMAINDER GOES TO THE HIGHEST-RANKED ITEMS. When the days do not
    divide evenly somebody has to get the extra day, and giving it to
    the top priority is the defensible direction.

    SCHEDULING DOES NOT RE-RANK. The order arriving here is
    `roadmap_priority_v1`'s and leaves untouched; this function only
    decides where each item sits in the calendar and how it breaks down.
    """
    count = min(item_budget(duration_days * hours_per_day), duration_days)
    kept = items[:count]
    if not kept:
        return Schedule(
            items=[],
            duration_days=duration_days,
            scheduled_days=0,
            unscheduled_days=duration_days,
        )

    scheduled = min(duration_days, len(kept) * MAX_DAYS_PER_ITEM)
    base, extra = divmod(scheduled, len(kept))
    cursor = 1
    for index, item in enumerate(kept):
        span = base + (1 if index < extra else 0)
        item.start_day = cursor
        item.end_day = cursor + span - 1
        item.week = (item.start_day - 1) // DAYS_PER_WEEK + 1
        # An ESTIMATE, and every layer above this is required to say so.
        # It is the candidate's own declared time divided across the
        # plan, not a claim about how long learning anything takes.
        item.estimated_hours = round(span * hours_per_day, 1)
        item.steps = _build_steps(item, hours_per_day=hours_per_day)
        cursor = item.end_day + 1

    return Schedule(
        items=kept,
        duration_days=duration_days,
        scheduled_days=scheduled,
        unscheduled_days=duration_days - scheduled,
    )
