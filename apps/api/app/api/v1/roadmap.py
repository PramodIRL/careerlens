"""The candidate's current learning roadmap (Prompt 6.3).

    GET /api/v1/roadmap?top_n=5&duration_days=28&hours_per_day=1

ONE COMBINED PLAN, NOT ONE PER JOB. It answers "what should I work on
next, given everything I have saved", and an item routinely cites
several jobs at once — which is the entire reason recurrence is part of
`roadmap_priority_v1`.

ITS OWN ROUTER, not an extension of /saved-jobs: the roadmap is
candidate-level and references many jobs, the same reasoning that gave
/skill-profile its own prefix rather than extending /candidate-skills.

STRICTLY READ-ONLY AND NOT PERSISTED. No INSERT, no UPDATE, no commit.
The plan is recomputed from current rows every request, because a stored
roadmap asserting "AWS is required by 3 of your top 5 jobs" in a
SENTENCE becomes a false claim about the user's own data the moment two
of those jobs are deleted — and unlike a stale number, a stale paragraph
still reads like a fact. Add a job, reorder one, confirm a skill, and
the next request is simply correct.

IT CHANGES NO SCORE. `/match` and `/gaps` are called through the SAME
handlers a client calls, so the roadmap reads exactly what the user sees
on each job. `skill_match_v1` and `skill_gap_v1` are not re-derived,
re-weighted or re-interpreted here, and `skill_match_v1` does not enter
`roadmap_priority_v1`'s arithmetic at all.

THE LLM IS AT THE END, NEVER INSIDE. Items, order, states, phases,
affected jobs and every `why` are decided before a provider is called.
A rejected narrative costs the user the wording and none of the
substance.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_current_user
from app.api.v1.saved_job import read_saved_job_gaps, read_saved_job_match
from app.db import get_db
from app.explanation.facts import load_taxonomy_names
from app.models.saved_job import SavedJob
from app.models.user import User
from app.roadmap.adapter import narrate
from app.roadmap.facts import (
    build_facts,
    collect_demands,
    load_evidence_for,
    select_jobs,
    to_selected,
)
from app.roadmap.priority import (
    DAYS_PER_WEEK,
    FORMULA_VERSION,
    SCHEDULE_VERSION,
    Schedule,
    rank_items,
    schedule_items,
    week_count,
)
from app.roadmap.provider import get_roadmap_provider
from app.roadmap.schema import SCHEMA_VERSION as NARRATIVE_SCHEMA_VERSION
from app.roadmap.schema import (
    NarrativeItem,
    NarrativeStep,
    NarrativeWeek,
    RoadmapEvidenceFact,
)
from app.schemas.roadmap import (
    RoadmapAffectedJobResponse,
    RoadmapEvidenceResponse,
    RoadmapGapState,
    RoadmapItemResponse,
    RoadmapResponse,
    RoadmapStepPhase,
    RoadmapStepResponse,
    RoadmapWeekResponse,
)
from app.schemas.saved_job import JobGapResponse, JobMatchResponse

router = APIRouter(tags=["roadmap"])

# THE CEILING ON `top_n` IS THE USER'S OWN SAVED-JOB COUNT, which is a
# per-request fact and cannot be a static Query bound — so the floor is
# declared here and the ceiling is checked in the handler. Asking for
# exactly as many jobs as you have saved IS "all saved jobs"; there is
# deliberately no separate sentinel for it, which would be a second code
# path to keep in step for no difference.
MIN_TOP_N = 1

# What an omitted `top_n` means. Resolved against the caller's actual
# count rather than sent as a fixed default, because a default the user
# never chose must not be able to be INVALID: a new candidate with one
# saved job asking for a roadmap would otherwise be told 5 is out of
# range for a number they never typed.
DEFAULT_TOP_N = 5

# Bounds on the declared preparation window. Eight weeks is where a
# preparation sprint stops being one.
#
# TWO DAYS, NOT SEVEN (Prompt 6.4b). The floor was a week because the
# plan was laid out in weeks, and a sub-week window had nowhere to go.
# It is laid out in DAYS now — weeks are only a presentation grouping,
# `ceil(duration_days / 7)` — so an interview on Thursday is a
# legitimate two-day plan rather than a validation error. One day is
# still refused: a plan with no second day is a task list, and the
# schedule has nothing to sequence.
MIN_DURATION_DAYS = 2
MAX_DURATION_DAYS = 56

# A hard ceiling on declared study time. Sixteen hours is already an
# implausible day; anything above it is a typo or a misreading of the
# field, and silently accepting it would produce estimates nobody should
# act on.
MAX_HOURS_PER_DAY = 16


def _week_label(week: int, duration_days: int) -> str:
    """ "Week 2 · Days 8-14" — the week AND the days it really covers.

    Both halves matter. The week number is how a person talks about a
    plan; the day range is what makes it a schedule rather than a label,
    and it is the thing the previous implementation only pretended to
    have.
    """
    start_day = (week - 1) * DAYS_PER_WEEK + 1
    end_day = min(week * DAYS_PER_WEEK, duration_days)
    return f"Week {week} · Days {start_day}–{end_day}"


def _week_bounds(week: int, duration_days: int) -> tuple[int, int]:
    return (week - 1) * DAYS_PER_WEEK + 1, min(week * DAYS_PER_WEEK, duration_days)


@router.get("", response_model=RoadmapResponse)
async def read_roadmap(
    top_n: int | None = Query(None, ge=MIN_TOP_N),
    duration_days: int = Query(28, ge=MIN_DURATION_DAYS, le=MAX_DURATION_DAYS),
    hours_per_day: float = Query(1.0, gt=0, le=MAX_HOURS_PER_DAY),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoadmapResponse:
    """The candidate's current learning roadmap.

    `top_n` is bounded by the caller's OWN saved-job count, which is a
    per-request fact rather than a static one — so asking for exactly as
    many jobs as you have saved is "all saved jobs", and asking for more
    is a 422 that names the real ceiling. Omitting it entirely resolves
    against that count instead of erroring.

    `top_n`, `duration_days` and `hours_per_day` are QUERY PARAMETERS,
    not stored preferences. The roadmap itself is not persisted, so
    persisting its inputs would buy only convenience — and the line is
    worth holding: priority is data about the user's jobs, while these
    three are arguments to one request for a plan.

    TOP-N IS ENFORCED BEFORE AGGREGATION. Jobs below the cut are never
    assembled into the facts at all, so an excluded job cannot
    contribute a weight, appear as an affected job, or be named by a
    model that was never given its id.
    """
    jobs = (await db.scalars(select(SavedJob).where(SavedJob.user_id == current_user.id))).all()
    saved_job_count = len(jobs)

    # THE DYNAMIC HALF OF THE BOUND. `ge` above catches 0 and below;
    # the ceiling is the caller's own saved-job count and only exists at
    # request time.
    #
    # NOT APPLIED WHEN NOTHING IS SAVED. Any `top_n` exceeds a count of
    # zero, but "you have not saved any jobs yet" is a legitimate empty
    # state rather than a client error, and 422-ing a brand-new user
    # asking for a roadmap would be absurd.
    # An omitted top_n takes as many as the candidate has, up to the
    # default. Only a value they actually typed can be out of range.
    if top_n is None:
        top_n = min(DEFAULT_TOP_N, saved_job_count) or DEFAULT_TOP_N
    elif saved_job_count and top_n > saved_job_count:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"top_n must be between {MIN_TOP_N} and {saved_job_count} — "
                f"you have {saved_job_count} saved "
                f"{'job' if saved_job_count == 1 else 'jobs'}"
            ),
        )

    selected_rows = select_jobs(list(jobs), top_n=top_n)
    total_hours = round(duration_days * hours_per_day, 1)
    provider = get_roadmap_provider()

    if not selected_rows:
        # No saved jobs is not an empty roadmap — it is a statement
        # about the INPUT. A client must say "you have not saved any
        # jobs yet", never "no gaps found", which would be a claim
        # about the person.
        return RoadmapResponse(
            formula_version=FORMULA_VERSION,
            schedule_version=SCHEDULE_VERSION,
            narrative_schema_version=NARRATIVE_SCHEMA_VERSION,
            narrative_status="rejected",
            reason="no_selected_jobs",
            provider=provider.name,
            selected_job_count=0,
            saved_job_count=saved_job_count,
            has_selected_jobs=False,
            duration_days=duration_days,
            hours_per_day=hours_per_day,
            total_hours=total_hours,
            unscheduled_days=duration_days,
            coverage="partial",
        )

    # One match and one gap read per SELECTED job, through the same
    # handlers a client calls — so this cannot drift from what the user
    # sees on the job itself, and neither endpoint changes behaviour
    # because the roadmap exists.
    per_job: dict[uuid.UUID, tuple[JobMatchResponse, JobGapResponse]] = {}
    match_scores: dict[uuid.UUID, int] = {}
    for job in selected_rows:
        match = await read_saved_job_match(job.id, current_user, db)
        gaps = await read_saved_job_gaps(job.id, current_user, db)
        per_job[job.id] = (match, gaps)
        match_scores[job.id] = match.overall_score

    selected = to_selected(selected_rows, match_scores)
    demands, skill_names = collect_demands(per_job)
    evidence_by_skill = await load_evidence_for(
        db, user_id=current_user.id, skill_ids=list(demands)
    )

    ranked = rank_items(
        demands,
        skill_names=skill_names,
        selected_jobs=selected,
        evidence_ids={
            skill_id: [row.id for row in rows] for skill_id, rows in evidence_by_skill.items()
        },
    )
    schedule = schedule_items(ranked, duration_days=duration_days, hours_per_day=hours_per_day)

    facts = build_facts(
        schedule=schedule,
        selected=selected,
        evidence_by_skill=evidence_by_skill,
        duration_days=duration_days,
        hours_per_day=hours_per_day,
        total_hours=total_hours,
    )

    outcome = await narrate(
        facts,
        provider=provider,
        taxonomy=await load_taxonomy_names(db),
    )

    return _to_response(
        schedule=schedule,
        facts_evidence={row.evidence_id: row for row in facts.evidence},
        outcome_status=outcome.status,
        reason=outcome.reason,
        provider=provider.name,
        overview=outcome.narrative.overview if outcome.narrative else None,
        weeks_by_number=(
            {week.week: week for week in outcome.narrative.weeks} if outcome.narrative else {}
        ),
        prose_by_item=(
            {item.item_id: item for item in outcome.narrative.items} if outcome.narrative else {}
        ),
        prose_by_step=(
            {step.step_id: step for step in outcome.narrative.steps} if outcome.narrative else {}
        ),
        selected_job_count=len(selected),
        saved_job_count=saved_job_count,
        duration_days=duration_days,
        hours_per_day=hours_per_day,
        total_hours=total_hours,
    )


def _to_response(
    *,
    schedule: Schedule,
    facts_evidence: dict[uuid.UUID, RoadmapEvidenceFact],
    outcome_status: str,
    reason: str | None,
    provider: str,
    overview: str | None,
    weeks_by_number: dict[int, NarrativeWeek],
    prose_by_item: dict[str, NarrativeItem],
    prose_by_step: dict[str, NarrativeStep],
    selected_job_count: int,
    saved_job_count: int,
    duration_days: int,
    hours_per_day: float,
    total_hours: float,
) -> RoadmapResponse:
    """Assemble the response.

    The deterministic fields are filled unconditionally; the written
    ones are filled only where a validated narrative supplied them. That
    asymmetry is the design: a rejection empties `task`, `done_when`,
    `success_criteria`, `overview` and `focus`, and touches nothing
    else — the days, the phases and the priorities all survive.

    AN ITEM APPEARS IN EVERY WEEK IT REALLY OCCUPIES (Prompt 6.4b),
    carrying only that week's steps. Filing a fortnight-long item under
    its start week alone is what produced "nothing scheduled for this
    week" on a plan where the candidate was in fact meant to be working.
    """
    by_week: dict[int, list[RoadmapItemResponse]] = {}

    for item in schedule.items:
        prose = prose_by_item.get(item.item_id)
        steps_by_week: dict[int, list[RoadmapStepResponse]] = {}
        for step in item.steps:
            step_prose = prose_by_step.get(step.step_id)
            steps_by_week.setdefault(step.week, []).append(
                RoadmapStepResponse(
                    step_id=step.step_id,
                    phase=RoadmapStepPhase(step.phase.value),
                    start_day=step.start_day,
                    end_day=step.end_day,
                    week=step.week,
                    estimated_hours=step.estimated_hours,
                    task=step_prose.task if step_prose else None,
                    done_when=step_prose.done_when if step_prose else None,
                )
            )

        affected_jobs = [
            RoadmapAffectedJobResponse(
                saved_job_id=job.saved_job_id,
                title=job.title,
                company=job.company,
                priority_rank=job.rank,
                match_score=job.match_score,
            )
            for job in item.affected_jobs
        ]
        evidence = [
            RoadmapEvidenceResponse(
                evidence_id=row.evidence_id,
                source_type=row.source_type,
                source_identifier=row.source_identifier,
                excerpt=row.excerpt,
            )
            for evidence_id in item.evidence_ids
            if (row := facts_evidence.get(evidence_id)) is not None
        ]

        for week, steps in sorted(steps_by_week.items()):
            by_week.setdefault(week, []).append(
                RoadmapItemResponse(
                    item_id=item.item_id,
                    skill_id=item.skill_id,
                    skill_name=item.skill_name,
                    state=RoadmapGapState(item.state.value),
                    start_day=item.start_day,
                    end_day=item.end_day,
                    week=item.week,
                    score=item.score,
                    state_weight=item.state_weight,
                    recurrence=item.recurrence,
                    why=item.why,
                    affected_jobs=affected_jobs,
                    evidence=evidence,
                    estimated_hours=item.estimated_hours,
                    steps=steps,
                    task=prose.task if prose else None,
                    outcome=prose.outcome if prose else None,
                    success_criteria=prose.success_criteria if prose else None,
                )
            )

    return RoadmapResponse(
        formula_version=FORMULA_VERSION,
        schedule_version=SCHEDULE_VERSION,
        narrative_schema_version=NARRATIVE_SCHEMA_VERSION,
        narrative_status=outcome_status,
        reason=reason,
        provider=provider,
        selected_job_count=selected_job_count,
        saved_job_count=saved_job_count,
        has_selected_jobs=True,
        duration_days=duration_days,
        hours_per_day=hours_per_day,
        total_hours=total_hours,
        scheduled_days=schedule.scheduled_days,
        unscheduled_days=schedule.unscheduled_days,
        coverage=schedule.coverage,
        overview=overview,
        # EVERY week of the declared window, including any that hold no
        # work: a plan that silently skips week 3 looks like a bug, and
        # "nothing scheduled here" is the honest thing to render. After
        # 6.4b an empty week means the material genuinely ran out —
        # `unscheduled_days` says so in the same response — rather than
        # an item being filed under the wrong week.
        weeks=[
            RoadmapWeekResponse(
                week=week,
                label=_week_label(week, duration_days),
                start_day=_week_bounds(week, duration_days)[0],
                end_day=_week_bounds(week, duration_days)[1],
                focus=narrative.focus if (narrative := weeks_by_number.get(week)) else None,
                checkpoint=narrative.checkpoint if narrative else None,
                items=by_week.get(week, []),
            )
            for week in range(1, week_count(duration_days) + 1)
        ],
    )
