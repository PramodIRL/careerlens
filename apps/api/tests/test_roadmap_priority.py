"""The deterministic roadmap formula (Prompt 6.3).

PURE ARITHMETIC, NO DATABASE. Every test here is the formula on paper,
which is the point of app/roadmap/priority.py being importable without a
session: a priority nobody can reproduce by hand is a priority nobody
can argue with.

The load-bearing property is that THE BANDS CANNOT CROSS. Several tests
below stress recurrence deliberately hard — every selected job wanting
the same preferred skill — because that is where an uncapped sum would
quietly overtake a required one, and it would do so only at scale, long
after a small fixture had passed.
"""

import uuid
from typing import Any

import pytest

from app.roadmap.priority import (
    DAYS_PER_WEEK,
    JOB_RANK_WEIGHTS,
    MAX_DAYS_PER_ITEM,
    MAX_ITEMS,
    MAX_RECURRENCE,
    MIN_ITEMS,
    STATE_WEIGHTS,
    GapState,
    JobDemand,
    SelectedJob,
    StepPhase,
    item_budget,
    job_weight,
    max_steps_for,
    phases_for,
    rank_items,
    schedule_items,
    week_count,
)
from app.roadmap.schema import MAX_STEPS

_PYTHON = uuid.UUID("11111111-1111-4111-8111-111111111111")
_AWS = uuid.UUID("22222222-2222-4222-8222-222222222222")
_DOCKER = uuid.UUID("33333333-3333-4333-8333-333333333333")

_NAMES = {_PYTHON: "Python", _AWS: "AWS", _DOCKER: "Docker"}


def _jobs(count: int) -> list[SelectedJob]:
    return [
        SelectedJob(
            saved_job_id=uuid.UUID(int=index, version=4),
            title=f"Engineer {index}",
            company=f"Company {index}",
            rank=index + 1,
        )
        for index in range(count)
    ]


def _rank(
    demands: dict[uuid.UUID, list[JobDemand]], jobs: list[SelectedJob]
) -> list[tuple[str, int]]:
    items = rank_items(demands, skill_names=_NAMES, selected_jobs=jobs)
    return [(item.skill_name, item.score) for item in items]


# --- the weights themselves --------------------------------------------


def test_the_recurrence_cap_is_narrower_than_every_band_gap() -> None:
    """THE PROPERTY EVERYTHING ELSE RESTS ON. If this ever fails, some
    number of jobs exists at which a preferred skill outranks a required
    one — and it would be discovered in production, not here."""
    weights = sorted(STATE_WEIGHTS.values())
    gaps = [higher - lower for lower, higher in zip(weights, weights[1:], strict=False)]
    assert MAX_RECURRENCE < min(gaps)


def test_job_weight_falls_off_then_flattens() -> None:
    assert [job_weight(rank) for rank in range(1, 6)] == list(JOB_RANK_WEIGHTS)
    # Beyond rank 5 every job counts the same: a candidate's fifth and
    # fifteenth choices are both "jobs I would take".
    assert job_weight(6) == job_weight(20) == 1


# --- ordering ----------------------------------------------------------


def test_required_missing_outranks_preferred_missing() -> None:
    jobs = _jobs(1)
    scored = _rank(
        {
            _AWS: [JobDemand(jobs[0].saved_job_id, GapState.MISSING_REQUIRED)],
            _DOCKER: [JobDemand(jobs[0].saved_job_id, GapState.MISSING_PREFERRED)],
        },
        jobs,
    )
    assert [name for name, _ in scored] == ["AWS", "Docker"]


def test_preferred_missing_outranks_weak_evidence() -> None:
    jobs = _jobs(1)
    scored = _rank(
        {
            _DOCKER: [JobDemand(jobs[0].saved_job_id, GapState.MISSING_PREFERRED)],
            _PYTHON: [JobDemand(jobs[0].saved_job_id, GapState.WEAK_EVIDENCE)],
        },
        jobs,
    )
    assert [name for name, _ in scored] == ["Docker", "Python"]


@pytest.mark.parametrize("job_count", [5, 10, 50])
def test_bands_hold_under_recurrence_stress(job_count: int) -> None:
    """One required skill wanted by ONE job still outranks a preferred
    skill wanted by every job the candidate saved. Without the cap this
    inverts somewhere past ten jobs."""
    jobs = _jobs(job_count)
    scored = _rank(
        {
            # Wanted only by the LOWEST-priority job.
            _AWS: [JobDemand(jobs[-1].saved_job_id, GapState.MISSING_REQUIRED)],
            _DOCKER: [JobDemand(job.saved_job_id, GapState.MISSING_PREFERRED) for job in jobs],
        },
        jobs,
    )
    assert [name for name, _ in scored] == ["AWS", "Docker"]


def test_recurrence_orders_within_a_band() -> None:
    jobs = _jobs(3)
    scored = _rank(
        {
            _AWS: [JobDemand(job.saved_job_id, GapState.MISSING_REQUIRED) for job in jobs[:3]],
            _DOCKER: [JobDemand(jobs[2].saved_job_id, GapState.MISSING_REQUIRED)],
        },
        jobs,
    )
    assert [name for name, _ in scored] == ["AWS", "Docker"]
    assert scored[0][1] == STATE_WEIGHTS[GapState.MISSING_REQUIRED] + 5 + 4 + 3
    assert scored[1][1] == STATE_WEIGHTS[GapState.MISSING_REQUIRED] + 3


def test_user_priority_changes_the_aggregation() -> None:
    """THE PRIMARY SIGNAL. Same gaps, same jobs — only the user's
    ordering moved, and the roadmap moved with it."""
    original = _jobs(2)
    demands = {
        _AWS: [JobDemand(original[0].saved_job_id, GapState.MISSING_REQUIRED)],
        _DOCKER: [JobDemand(original[1].saved_job_id, GapState.MISSING_REQUIRED)],
    }
    assert [name for name, _ in _rank(demands, original)] == ["AWS", "Docker"]

    # The user drags their second job to the top.
    swapped = [
        SelectedJob(original[1].saved_job_id, original[1].title, original[1].company, rank=1),
        SelectedJob(original[0].saved_job_id, original[0].title, original[0].company, rank=2),
    ]
    assert [name for name, _ in _rank(demands, swapped)] == ["Docker", "AWS"]


def test_the_strongest_demand_wins_rather_than_an_average() -> None:
    """Required in one job and preferred in another is REQUIRED — the
    same select-don't-blend rule the rest of the codebase applies."""
    jobs = _jobs(2)
    items = rank_items(
        {
            _AWS: [
                JobDemand(jobs[0].saved_job_id, GapState.MISSING_PREFERRED),
                JobDemand(jobs[1].saved_job_id, GapState.MISSING_REQUIRED),
            ]
        },
        skill_names=_NAMES,
        selected_jobs=jobs,
    )
    assert items[0].state is GapState.MISSING_REQUIRED


def test_ranking_is_order_independent() -> None:
    jobs = _jobs(3)
    demands = {
        _AWS: [JobDemand(jobs[0].saved_job_id, GapState.MISSING_REQUIRED)],
        _DOCKER: [JobDemand(jobs[1].saved_job_id, GapState.MISSING_PREFERRED)],
        _PYTHON: [JobDemand(jobs[2].saved_job_id, GapState.WEAK_EVIDENCE)],
    }
    forward = _rank(demands, jobs)
    reversed_demands = dict(reversed(list(demands.items())))
    assert _rank(reversed_demands, list(reversed(jobs))) == forward


# --- Top-N exclusion ---------------------------------------------------


def test_a_demand_from_an_unselected_job_contributes_nothing() -> None:
    """Belt to facts.py's braces: even if an unselected job's demand
    reached the ranker, it could not score, and a skill wanted ONLY by
    an excluded job produces no item at all."""
    jobs = _jobs(2)
    outsider = uuid.UUID("99999999-9999-4999-8999-999999999999")

    items = rank_items(
        {
            _AWS: [
                JobDemand(jobs[0].saved_job_id, GapState.MISSING_REQUIRED),
                JobDemand(outsider, GapState.MISSING_REQUIRED),
            ],
            _DOCKER: [JobDemand(outsider, GapState.MISSING_REQUIRED)],
        },
        skill_names=_NAMES,
        selected_jobs=jobs,
    )

    assert [item.skill_name for item in items] == ["AWS"]
    assert items[0].score == STATE_WEIGHTS[GapState.MISSING_REQUIRED] + 5
    assert [job.saved_job_id for job in items[0].affected_jobs] == [jobs[0].saved_job_id]


# --- explanations ------------------------------------------------------


def test_every_item_explains_itself_without_a_model() -> None:
    jobs = _jobs(3)
    items = rank_items(
        {_AWS: [JobDemand(job.saved_job_id, GapState.MISSING_REQUIRED) for job in jobs]},
        skill_names=_NAMES,
        selected_jobs=jobs,
    )
    why = items[0].why
    assert "AWS is missing" in why
    assert "required by 3 of your 3 selected jobs" in why
    assert "#1 priority" in why


def test_weak_evidence_asks_for_evidence_not_learning() -> None:
    jobs = _jobs(1)
    items = rank_items(
        {_DOCKER: [JobDemand(jobs[0].saved_job_id, GapState.WEAK_EVIDENCE)]},
        skill_names=_NAMES,
        selected_jobs=jobs,
    )
    assert "have not reviewed yet" in items[0].why
    assert "missing" not in items[0].why


# --- time as depth, never order ----------------------------------------


def _many_items(count: int) -> list[Any]:
    jobs = _jobs(1)
    return rank_items(
        {
            uuid.UUID(int=index, version=4): [
                JobDemand(jobs[0].saved_job_id, GapState.MISSING_REQUIRED)
            ]
            for index in range(count)
        },
        skill_names={},
        selected_jobs=jobs,
    )


def test_time_changes_depth_and_never_the_top_item() -> None:
    jobs = _jobs(1)
    demands = {
        skill: [JobDemand(jobs[0].saved_job_id, GapState.MISSING_REQUIRED)]
        for skill in (_AWS, _DOCKER, _PYTHON)
    }
    ranked = rank_items(demands, skill_names=_NAMES, selected_jobs=jobs)

    lean = schedule_items([*ranked], duration_days=7, hours_per_day=0.5).items
    generous = schedule_items([*ranked], duration_days=56, hours_per_day=4).items

    assert len(lean) <= len(generous)
    # The same gap is first at three hours and at two hundred.
    # Truncation is from the BOTTOM; time never reorders.
    assert lean[0].skill_name == generous[0].skill_name == ranked[0].skill_name


def test_the_item_budget_is_bounded_at_both_ends() -> None:
    assert item_budget(0.5) == MIN_ITEMS
    assert item_budget(10_000) == MAX_ITEMS


# --- the schedule -------------------------------------------------------


@pytest.mark.parametrize("duration_days", [7, 14, 21, 28, 30, 45, 56])
@pytest.mark.parametrize("hours_per_day", [0.5, 1.0, 4.0, 16.0])
def test_day_spans_cover_the_window_exactly(duration_days: int, hours_per_day: float) -> None:
    """THE PROPERTY THE OLD PHASE LABELS ONLY PRETENDED TO HAVE. No
    overflow past what the candidate declared, and no unclaimed tail."""
    items = schedule_items(
        _many_items(MAX_ITEMS), duration_days=duration_days, hours_per_day=hours_per_day
    ).items

    assert items[0].start_day == 1
    assert items[-1].end_day == duration_days
    # Contiguous: every day belongs to exactly one item.
    for earlier, later in zip(items, items[1:], strict=False):
        assert later.start_day == earlier.end_day + 1
    assert sum(item.end_day - item.start_day + 1 for item in items) == duration_days


@pytest.mark.parametrize("hours_per_day", [0.5, 1.0, 16.0])
def test_estimated_hours_never_exceed_the_declared_budget(hours_per_day: float) -> None:
    items = schedule_items(
        _many_items(MAX_ITEMS), duration_days=28, hours_per_day=hours_per_day
    ).items

    assert sum(item.estimated_hours for item in items) == pytest.approx(28 * hours_per_day, abs=0.5)


def test_weeks_are_derived_from_the_duration() -> None:
    """Not a fixed four. Telling somebody with fourteen days that they
    have four weeks is what the previous labelling did."""
    assert week_count(28) == 4
    assert week_count(7) == 1
    assert week_count(14) == 2
    assert week_count(56) == 8
    # A part week still counts: 30 days is four weeks and two days.
    assert week_count(30) == 5


def test_an_item_never_gets_less_than_a_day() -> None:
    """A twelve-item budget over seven days would otherwise divide to a
    span of zero and stack every item on day one."""
    items = schedule_items(_many_items(MAX_ITEMS), duration_days=7, hours_per_day=16).items

    assert len(items) <= 7
    assert all(item.end_day >= item.start_day for item in items)


def test_the_remainder_goes_to_the_highest_ranked_items() -> None:
    """Somebody has to get the extra day when the division is uneven,
    and the top priority is the defensible direction."""
    items = schedule_items(_many_items(4), duration_days=30, hours_per_day=1).items
    spans = [item.end_day - item.start_day + 1 for item in items]

    assert sum(spans) == 30
    assert spans == sorted(spans, reverse=True)


def test_scheduling_does_not_re_rank() -> None:
    """THE BOUNDARY THIS REFINEMENT MUST NOT CROSS. Placement changed;
    priorities did not."""
    ranked = _many_items(MAX_ITEMS)
    before = [item.skill_id for item in ranked]

    scheduled = schedule_items([*ranked], duration_days=28, hours_per_day=1).items

    assert [item.skill_id for item in scheduled] == before[: len(scheduled)]


def test_every_item_lands_in_a_real_week() -> None:
    items = schedule_items(_many_items(8), duration_days=28, hours_per_day=1).items

    assert all(1 <= item.week <= week_count(28) for item in items)
    assert all(item.week == (item.start_day - 1) // DAYS_PER_WEEK + 1 for item in items)
    # Highest priority first: nothing outranked starts later.
    assert [item.start_day for item in items] == sorted(item.start_day for item in items)
    assert all(item.estimated_hours > 0 for item in items)


# =====================================================================
# 6.4b — the day-level schedule
#
# THE OBSERVED FAILURE. 28 days, two hours a day, five selected jobs
# produced exactly two items: AWS on days 1-14 and Kubernetes on days
# 15-28, rendered as "Week 1: AWS", "Week 2: nothing", "Week 3:
# Kubernetes", "Week 4: nothing".
#
# Two defects, and the first is not what it looks like. The empty weeks
# were an ATTRIBUTION artifact — real work was happening in weeks 2 and
# 4, but `week` came from the item's start day alone. The second is that
# a fourteen-day item was one undifferentiated instruction, so no prompt
# change could have produced a day-by-day journey: the schema had
# nowhere to put one.
# =====================================================================


def _states(count: int, state: GapState) -> list[Any]:
    jobs = _jobs(1)
    return rank_items(
        {
            uuid.UUID(int=index, version=4): [JobDemand(jobs[0].saved_job_id, state)]
            for index in range(count)
        },
        skill_names={},
        selected_jobs=jobs,
    )


def _all_steps(schedule: Any) -> list[Any]:
    return sorted(
        (step for item in schedule.items for step in item.steps),
        key=lambda step: step.start_day,
    )


def test_the_reported_case_now_fills_every_week() -> None:
    """THE REGRESSION, in the exact shape it was reported. Two items,
    28 days, two hours a day — and no week left claiming nothing is
    scheduled while the candidate is meant to be working."""
    schedule = schedule_items(_many_items(2), duration_days=28, hours_per_day=2)

    assert schedule.coverage == "full"
    assert schedule.unscheduled_days == 0
    # The two items still occupy the same fortnights: priorities and
    # placement are unchanged.
    assert [(item.start_day, item.end_day) for item in schedule.items] == [(1, 14), (15, 28)]
    # But the WEEKS are now derived from the work, not from a start day.
    assert sorted({step.week for step in _all_steps(schedule)}) == [1, 2, 3, 4]


def test_a_step_never_straddles_a_week() -> None:
    """What makes week attribution exact rather than approximated. The
    schedule splits on week boundaries BEFORE it splits on length."""
    for duration in (2, 3, 5, 7, 9, 14, 21, 28, 30, 45, 56):
        schedule = schedule_items(_many_items(3), duration_days=duration, hours_per_day=1)
        for step in _all_steps(schedule):
            start_week = (step.start_day - 1) // DAYS_PER_WEEK + 1
            end_week = (step.end_day - 1) // DAYS_PER_WEEK + 1
            assert start_week == end_week == step.week, (duration, step)


@pytest.mark.parametrize("duration_days", [2, 3, 5, 7, 14, 21, 28, 30, 45, 56])
@pytest.mark.parametrize("hours_per_day", [0.5, 1.0, 2.0, 4.0, 16.0])
def test_steps_tile_the_scheduled_window_exactly(duration_days: int, hours_per_day: float) -> None:
    """No overlap, no hole, and nothing outside the declared window."""
    schedule = schedule_items(
        _many_items(MAX_ITEMS), duration_days=duration_days, hours_per_day=hours_per_day
    )
    steps = _all_steps(schedule)

    assert steps[0].start_day == 1
    assert steps[-1].end_day == schedule.scheduled_days
    for earlier, later in zip(steps, steps[1:], strict=False):
        assert later.start_day == earlier.end_day + 1
    assert all(step.end_day <= duration_days for step in steps)
    assert all(step.end_day >= step.start_day for step in steps)
    # Each item's steps sum to that item's own span.
    for item in schedule.items:
        assert sum(step.end_day - step.start_day + 1 for step in item.steps) == (
            item.end_day - item.start_day + 1
        )


@pytest.mark.parametrize("duration_days", [2, 7, 28, 56])
@pytest.mark.parametrize("hours_per_day", [0.5, 2.0, 16.0])
def test_total_step_hours_never_exceed_the_declared_budget(
    duration_days: int, hours_per_day: float
) -> None:
    """THE HARD CONSTRAINT. Days are distributed and hours follow from
    the span, so the bound is arithmetic rather than a check."""
    schedule = schedule_items(
        _many_items(MAX_ITEMS), duration_days=duration_days, hours_per_day=hours_per_day
    )

    total = sum(step.estimated_hours for step in _all_steps(schedule))
    assert total <= duration_days * hours_per_day + 0.5


def test_a_priority_item_is_decomposed_into_several_steps() -> None:
    """A fortnight of work is a learning sequence, not one instruction."""
    schedule = schedule_items(_many_items(2), duration_days=28, hours_per_day=2)

    assert all(len(item.steps) > 1 for item in schedule.items)
    # And the sequence PROGRESSES rather than repeating a mode.
    for item in schedule.items:
        phases = [step.phase for step in item.steps]
        assert phases == list(dict.fromkeys(phases)), phases


def test_a_short_item_is_one_step() -> None:
    """Proportionate: two days is one focused piece of work, and the
    ladder's one-step rung is BUILD rather than LEARN — a single day
    spent reading produces nothing anybody can look at."""
    schedule = schedule_items(_many_items(2), duration_days=2, hours_per_day=2)

    assert [len(item.steps) for item in schedule.items] == [1, 1]
    assert [item.steps[0].phase for item in schedule.items] == [StepPhase.BUILD] * 2


def test_weak_evidence_gets_evidence_rungs_never_learning_ones() -> None:
    """WEAK IS NOT MISSING. The candidate may already have the skill, so
    a ladder starting at LEARN would tell them to learn what they can
    already do. Enforced in code rather than asked for in a prompt."""
    schedule = schedule_items(_states(2, GapState.WEAK_EVIDENCE), duration_days=28, hours_per_day=1)

    phases = {step.phase for item in schedule.items for step in item.steps}
    assert StepPhase.LEARN not in phases
    assert StepPhase.PRACTICE not in phases
    assert phases <= {StepPhase.DEMONSTRATE, StepPhase.DOCUMENT, StepPhase.PROVE}


def test_a_missing_skill_builds_capability_before_it_proves_it() -> None:
    schedule = schedule_items(
        _states(1, GapState.MISSING_REQUIRED), duration_days=14, hours_per_day=1
    )
    phases = [step.phase for step in schedule.items[0].steps]

    assert phases[0] is StepPhase.LEARN
    assert StepPhase.BUILD in phases
    assert phases.index(StepPhase.LEARN) < phases.index(StepPhase.BUILD)


def test_the_phase_ladder_returns_exactly_what_it_is_asked_for() -> None:
    for state in GapState:
        for count in range(1, max_steps_for(state) + 1):
            assert len(phases_for(state, count)) == count
        # Beyond the ladder it saturates rather than repeating a rung.
        beyond = phases_for(state, max_steps_for(state) + 3)
        assert len(beyond) == max_steps_for(state)


def test_no_single_skill_runs_past_a_fortnight() -> None:
    """A four-week block on one skill is the arithmetic running out of
    material and padding with time, not a plan a mentor would write."""
    for duration in (14, 28, 45, 56):
        schedule = schedule_items(_many_items(2), duration_days=duration, hours_per_day=1)
        for item in schedule.items:
            assert item.end_day - item.start_day + 1 <= MAX_DAYS_PER_ITEM


def test_insufficient_material_is_reported_rather_than_stretched() -> None:
    """Two gaps cannot honestly fill eight weeks. The remainder is a
    deterministic fact, not a failure and not something to pad."""
    schedule = schedule_items(_many_items(2), duration_days=56, hours_per_day=1)

    assert schedule.coverage == "partial"
    assert schedule.scheduled_days == 28
    assert schedule.unscheduled_days == 28
    assert schedule.scheduled_days + schedule.unscheduled_days == 56
    assert max(step.end_day for step in _all_steps(schedule)) == 28


def test_the_reported_case_is_still_full_coverage() -> None:
    """2 x 14 = 28: the cap must not turn the case we are fixing into a
    partial plan."""
    schedule = schedule_items(_many_items(2), duration_days=28, hours_per_day=2)

    assert schedule.coverage == "full"
    assert schedule.unscheduled_days == 0


def test_a_two_day_plan_is_valid_and_is_not_four_weeks() -> None:
    schedule = schedule_items(_many_items(4), duration_days=2, hours_per_day=3)

    assert week_count(2) == 1
    assert schedule.coverage == "full"
    assert {step.week for step in _all_steps(schedule)} == {1}
    assert max(step.end_day for step in _all_steps(schedule)) == 2


def test_decomposition_does_not_re_rank() -> None:
    """THE BOUNDARY THIS SLICE MUST NOT CROSS. Steps are a layout
    detail; priorities are not."""
    ranked = _many_items(MAX_ITEMS)
    before = [item.skill_id for item in ranked]

    schedule = schedule_items([*ranked], duration_days=28, hours_per_day=1)

    assert [item.skill_id for item in schedule.items] == before[: len(schedule.items)]
    assert [item.start_day for item in schedule.items] == sorted(
        item.start_day for item in schedule.items
    )


def test_step_ids_are_unique_and_derived_from_their_item() -> None:
    """The key a narrative writes prose against. Unique, so the
    validator's set-equality check means something, and prefixed with
    the item so a reader can see which skill a step belongs to."""
    schedule = schedule_items(_many_items(6), duration_days=28, hours_per_day=2)
    steps = _all_steps(schedule)

    assert len({step.step_id for step in steps}) == len(steps)
    for item in schedule.items:
        assert all(step.step_id.startswith(f"{item.item_id}:") for step in item.steps)


def test_the_plan_never_needs_more_steps_than_the_schema_allows() -> None:
    """MAX_STEPS bounds the response schema; the day arithmetic has to
    stay under it for every input the API accepts."""
    for duration in range(2, 57):
        for hours in (0.5, 1.0, 2.0, 4.0, 16.0):
            schedule = schedule_items(
                _many_items(MAX_ITEMS), duration_days=duration, hours_per_day=hours
            )
            assert len(_all_steps(schedule)) <= MAX_STEPS, (duration, hours)
