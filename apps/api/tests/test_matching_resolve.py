"""The shared candidate-vs-requirement resolver (Prompt 4.4).

Pure — no database. This is the ONE place that decides whether a
candidate satisfies a job requirement, and both `/match` and `/gaps`
call it, so these tests pin the policy both endpoints inherit.
"""

import uuid

import pytest

from app.matching.resolve import (
    ResolutionState,
    resolve_requirements,
    resolve_state,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("confirmed", ResolutionState.SATISFIED),
        ("suggested", ResolutionState.NEEDS_CONFIRMATION),
        ("rejected", ResolutionState.REJECTED),
        (None, ResolutionState.MISSING),
    ],
)
def test_each_candidate_status_maps_to_one_state(status: str | None, expected: str) -> None:
    assert resolve_state(status) is expected


def test_an_unrecognised_status_fails_closed_to_missing() -> None:
    """The status vocabulary is stored as plain text so it can grow. A
    value this deployment has not learned must never be assumed to
    satisfy a requirement."""
    assert resolve_state("provisional") is ResolutionState.MISSING


@pytest.mark.parametrize(
    ("status", "satisfied"),
    [("confirmed", True), ("suggested", True), ("rejected", False), (None, False)],
)
def test_only_confirmed_and_suggested_satisfy(status: str | None, satisfied: bool) -> None:
    """The single definition of "counts" that Prompt 4.3's score and
    Prompt 4.4's buckets both build on — which is what stops them
    drifting apart."""
    skill_id = uuid.uuid4()
    statuses = {skill_id: status} if status else {}

    resolved = resolve_requirements([(skill_id, "Python", "required", "excerpt")], statuses)

    assert resolved[0].satisfied is satisfied


@pytest.mark.parametrize(
    ("status", "is_gap"),
    [("confirmed", False), ("suggested", False), ("rejected", False), (None, True)],
)
def test_only_a_genuinely_absent_skill_is_an_ordinary_gap(status: str | None, is_gap: bool) -> None:
    """`suggested` has evidence and `rejected` is the user's decision —
    both get their own bucket rather than being reported as an absence."""
    skill_id = uuid.uuid4()
    statuses = {skill_id: status} if status else {}

    resolved = resolve_requirements([(skill_id, "Python", "required", "excerpt")], statuses)

    assert resolved[0].is_gap is is_gap


def test_resolution_preserves_the_requirement_level_and_excerpt() -> None:
    """A rejected REQUIRED skill must still be visibly required."""
    skill_id = uuid.uuid4()

    resolved = resolve_requirements(
        [(skill_id, "Docker", "required", "Docker is required")], {skill_id: "rejected"}
    )

    assert resolved[0].requirement_level == "required"
    assert resolved[0].job_excerpt == "Docker is required"
    assert resolved[0].candidate_status == "rejected"


def test_a_candidate_skill_the_job_does_not_ask_for_is_ignored() -> None:
    wanted, unrelated = uuid.uuid4(), uuid.uuid4()

    resolved = resolve_requirements(
        [(wanted, "Python", "required", "excerpt")], {unrelated: "confirmed"}
    )

    assert len(resolved) == 1
    assert resolved[0].state is ResolutionState.MISSING


def test_input_order_is_preserved_for_the_caller_to_sort() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()

    resolved = resolve_requirements(
        [(a, "Zulu", "required", "x"), (b, "Alpha", "preferred", "y")], {}
    )

    assert [r.skill_name for r in resolved] == ["Zulu", "Alpha"]


def test_an_empty_requirement_list_resolves_to_nothing() -> None:
    assert resolve_requirements([], {uuid.uuid4(): "confirmed"}) == []
