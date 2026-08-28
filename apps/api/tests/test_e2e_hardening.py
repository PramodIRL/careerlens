"""End-to-end hardening: the guarantees that only hold ACROSS features
(Prompt 7.1, slice a).

Every other file in this suite proves one slice. These three tests prove
things that no single slice owns, and that a per-slice suite therefore
cannot notice breaking:

  * THE CENTRAL INVARIANT, AT THE HTTP BOUNDARY. If the optional LLM is
    unreachable, the deterministic product must still be served. Prompt
    6.2's tests prove this against `explain()` and `narrate()` directly,
    and the two route-level tests that exist inject MALFORMED JSON —
    a response that arrived. The likeliest real failure is the opposite
    one: `ollama serve` is not running, nothing arrives at all, and the
    transport error is translated in app/explanation/ollama_provider.py
    rather than in a validator. That path had no test through a route.

  * ONE OWNERSHIP SWEEP OVER EVERY PER-JOB READ ROUTE, discovered from
    the router rather than listed by hand. The existing sweeps are
    per-feature: tests/test_mvp_acceptance.py covers `/`, `/requirements`,
    `/match` and `/gaps`; tests/test_job_eligibility_api.py covers its
    two; tests/test_explanation_api.py covers `/explanation`. `/semantic`
    was in none of them and had no route-level test of any kind — its
    check is correct, and nothing proved it. Reading the paths off the
    app's own OpenAPI document is what stops the NEXT route from being
    missed the same way.

  * THE SCHEDULE BOUND OVER THE WHOLE DECLARED PARAMETER SURFACE, on a
    plan whose gaps span all three states. tests/test_roadmap_api.py
    checks the same invariants at fixed points — (30 days, 1 h),
    (28 days, 2 h), and a duration loop pinned at one hour a day — and
    always on a single-state fixture. `hours_per_day` is never swept
    there, and mixed states are what a real candidate actually has:
    the phase ladders differ in LENGTH by state, so mixing them is the
    case where the step arithmetic and the day arithmetic have to agree
    about a plan neither was tuned on.

WHAT THESE TESTS DO NOT DO. They add no fixture world of their own: the
helpers come from tests/test_roadmap_api.py, which already builds
exactly this world (a seeded taxonomy, a registered candidate, saved
jobs with overlapping requirements). Importing them rather than copying
them is deliberate — a second copy is how two files quietly stop
agreeing about what "a candidate with a required gap" means, and
tests/test_resume_deletion_evidence.py already sets the precedent for
sharing a fixture across test modules.
"""

import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.explanation.prompt import ExplanationRequest
from app.explanation.provider import ExplanationUnavailable
from app.main import app
from app.rate_limit import _request_log
from app.roadmap.priority import week_count
from app.roadmap.schema import MAX_STEPS, MAX_WEEKS
from tests.conftest import isolated_schema_override
from tests.test_roadmap_api import (
    _create_job,
    _give_skill,
    _headers,
    _items,
    _new_user,
    _roadmap,
    _seed_taxonomy,
    _steps,
)

_JOBS = "/api/v1/saved-jobs"

# One posting per gap KIND, so a plan built from both spans all three
# states of `roadmap_priority_v1`:
#
#   missing_required   AWS, Redis  — asked for, candidate has neither
#   missing_preferred  Kubernetes  — preferred, candidate does not have it
#   weak_evidence      Python      — the candidate HAS it, as `suggested`,
#                                    so it satisfies the requirement and
#                                    still lands in `needs_confirmation`
#
# Docker is required by both, which is what gives recurrence something
# to add up — the same overlap tests/test_roadmap_api.py relies on.
_FIRST_JOB = "We need AWS and Docker. Python is required. Kubernetes preferred."
_SECOND_JOB = "Docker is required. Redis is required. PostgreSQL preferred."


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    _request_log.clear()


class _UnreachableProvider:
    """A provider that cannot be reached at all.

    NOT a malformed response — that is the case the existing route tests
    already cover, and it exercises the validator. This raises what
    app/explanation/ollama_provider.py's `_translate` raises when the
    daemon is down, which is the failure a demo actually hits and the
    only one that reaches app/explanation/runtime.py's transport branch.

    Names itself "mock" so the response's `provider` field is unchanged
    and the comparisons below isolate exactly one variable: whether the
    model answered. Same choice the stubs in tests/test_roadmap_api.py
    and tests/test_explanation_api.py make.
    """

    @property
    def name(self) -> str:
        return "mock"

    async def complete(self, request: ExplanationRequest) -> str:
        raise ExplanationUnavailable("ollama unreachable (ConnectError)")


def _candidate_with_every_gap_kind(client: TestClient) -> tuple[str, str, str]:
    """A candidate whose plan holds a required, a preferred and a weak
    item, plus two saved jobs. Returns (token, first job, second job)."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    # `suggested`, not `confirmed`: it satisfies the requirement AND
    # stays in `needs_confirmation`, which is the only way to produce a
    # WEAK_EVIDENCE roadmap item.
    _give_skill(user_id, "Python", "suggested")
    first = _create_job(client, token, _FIRST_JOB, company="Acme")
    second = _create_job(client, token, _SECOND_JOB, company="Globex")
    return token, first, second


def _get(client: TestClient, token: str, job_id: str, suffix: str) -> dict[str, Any]:
    response = client.get(f"{_JOBS}/{job_id}/{suffix}", headers=_headers(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- 1. the deterministic answer survives an unreachable provider ------

# What the model supplies, and therefore the ONLY thing a provider
# failure is allowed to cost. Everything else in either response is
# computed before a provider is called and must be byte-identical
# either way.
_GENERATED_EXPLANATION_FIELDS = ("summary", "strengths", "gaps", "next_steps", "cited_evidence")


def _without_generated(payload: dict[str, Any]) -> dict[str, Any]:
    """An explanation response with the written half removed."""
    return {
        key: value
        for key, value in payload.items()
        if key not in {*_GENERATED_EXPLANATION_FIELDS, "status", "reason"}
    }


def _without_narrative(payload: dict[str, Any]) -> dict[str, Any]:
    """A roadmap response with every written field removed.

    Strips rather than compares field by field: a NEW written field
    added later without being listed here shows up as a difference,
    which is the failure mode worth catching — a model quietly gaining
    the ability to change something deterministic.
    """
    plan = {
        key: value
        for key, value in payload.items()
        if key not in {"narrative_status", "reason", "overview"}
    }
    plan["weeks"] = [
        {
            **{key: value for key, value in week.items() if key not in {"focus", "checkpoint"}},
            "items": [
                {
                    **{
                        key: value
                        for key, value in item.items()
                        if key not in {"task", "outcome", "success_criteria"}
                    },
                    "steps": [
                        {
                            key: value
                            for key, value in step.items()
                            if key not in {"task", "done_when"}
                        }
                        for step in item["steps"]
                    ],
                }
                for item in week["items"]
            ],
        }
        for week in payload["weeks"]
    ]
    return plan


def test_the_deterministic_answer_survives_an_unreachable_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE CENTRAL INVARIANT, through the routes a browser calls.

    Asserted as EQUALITY against a working run rather than as "the
    fields are present": a plan that survives but silently reorders, or
    a score that survives but changes, would pass a presence check and
    is exactly the failure this invariant exists to forbid.
    """
    token, first, second = _candidate_with_every_gap_kind(client)

    # A working run first, so the comparison is against this candidate's
    # real deterministic answer rather than a hand-written expectation.
    good_explanation = _get(client, token, first, "explanation")
    good_match = _get(client, token, first, "match")
    good_gaps = _get(client, token, first, "gaps")
    good_roadmap = _roadmap(client, token, duration_days=28, hours_per_day=1)

    assert good_explanation["status"] == "generated"
    assert good_roadmap["narrative_status"] == "generated"
    # The fixture really does span all three states, or the rest of this
    # test proves less than it appears to.
    assert {item["state"] for item in _items(good_roadmap)} == {
        "missing_required",
        "missing_preferred",
        "weak_evidence",
    }

    monkeypatch.setattr(
        "app.api.v1.saved_job.get_explanation_provider",
        lambda *args, **kwargs: _UnreachableProvider(),
    )
    monkeypatch.setattr(
        "app.api.v1.roadmap.get_roadmap_provider",
        lambda *args, **kwargs: _UnreachableProvider(),
    )

    # --- the explanation ------------------------------------------------
    # A 200, not a 502: the deterministic answer is intact and worth
    # serving. The model failing to explain it is not an error in the
    # match.
    dead_explanation = _get(client, token, first, "explanation")

    assert dead_explanation["status"] == "rejected"
    assert dead_explanation["reason"] == "provider_unavailable"
    # NOTHING generated comes back — not a partial, not a placeholder.
    assert dead_explanation["summary"] is None
    assert dead_explanation["strengths"] == []
    assert dead_explanation["gaps"] == []
    assert dead_explanation["next_steps"] == []
    assert dead_explanation["cited_evidence"] == []
    # And every echoed deterministic field is untouched.
    assert _without_generated(dead_explanation) == _without_generated(good_explanation)

    # --- the roadmap ----------------------------------------------------
    dead_roadmap = _roadmap(client, token, duration_days=28, hours_per_day=1)

    assert dead_roadmap["narrative_status"] == "rejected"
    assert dead_roadmap["reason"] == "provider_unavailable"
    assert dead_roadmap["overview"] is None
    # THE WHOLE PLAN, not a sample of it: same items, same order, same
    # days, same steps, same phases, same affected jobs, same `why`.
    assert _without_narrative(dead_roadmap) == _without_narrative(good_roadmap)
    # `why` is generated deterministically in app/roadmap/priority.py, so
    # it is the field that proves a rejected plan is still READABLE
    # rather than merely structurally intact.
    for item in _items(dead_roadmap):
        assert item["why"]
        assert item["affected_jobs"]
        assert item["steps"]
        assert item["task"] is None

    # --- and the failure wrote nothing ----------------------------------
    # A rejected explanation must not have disturbed the two endpoints
    # that were never asking a model anything.
    assert _get(client, token, first, "match") == good_match
    assert _get(client, token, first, "gaps") == good_gaps
    assert _get(client, token, second, "match")["formula_version"] == "skill_match_v1"


# --- 2. ownership, over every per-job read route -----------------------

# DISCOVERED, NOT LISTED. Reading the paths off the app is what makes
# this sweep cover a route somebody adds next month without them having
# to remember this file exists — which is precisely how `/semantic` came
# to have no ownership test at all.
#
# FROM THE OPENAPI DOCUMENT, NOT `app.routes`. This FastAPI version keeps
# an included router as a single opaque entry rather than flattening its
# routes onto `app.routes`, so walking that attribute finds NOTHING here
# and the sweep would silently parametrise to zero cases — a test that
# passes by testing nothing, which is worse than the gap it was written
# to close. The generated document is the app's own published contract
# and does not depend on that internal layout.
_PER_JOB_READ_PATHS = sorted(
    path
    for path, operations in app.openapi()["paths"].items()
    if "get" in operations and path.startswith(f"{_JOBS}/{{saved_job_id}}")
)


def test_the_ownership_sweep_covers_every_per_job_read_route() -> None:
    """The guard on the sweep below.

    Without this, a router change that dropped a path would make the
    parametrised test pass by testing less. Named individually so the
    failure says WHICH route stopped being swept.
    """
    discovered = {path.rsplit("}", 1)[-1].lstrip("/") for path in _PER_JOB_READ_PATHS}

    # An empty sweep is the failure this guard exists for: a
    # parametrisation over nothing reports as skipped, not as failed.
    assert _PER_JOB_READ_PATHS
    assert discovered >= {
        "",
        "requirements",
        "match",
        "gaps",
        "eligibility",
        "eligibility-requirements",
        # The one no route-level test reached before Prompt 7.1.
        "semantic",
        "explanation",
    }


@pytest.mark.parametrize("path", _PER_JOB_READ_PATHS)
def test_every_per_job_read_route_enforces_ownership(client: TestClient, path: str) -> None:
    """401 without a token, 403 for somebody else's job, 404 for one
    that does not exist — and 200 for the owner, so a route that is
    simply broken cannot pass by rejecting everything.

    403 rather than 404 for another user's job is the existing, deliberate
    choice across this API: saved-job ids are random UUIDs rather than
    enumerable integers, so naming the resource is not a guessing
    surface. See app/api/v1/saved_job.py's `_get_owned_job`.
    """
    token, job_id, _ = _candidate_with_every_gap_kind(client)
    intruder_token, _ = _new_user(client)

    owned = path.format(saved_job_id=job_id)
    missing = path.format(saved_job_id=uuid.uuid4())

    assert client.get(owned).status_code == 401, f"{path}: unauthenticated"
    assert client.get(owned, headers=_headers(intruder_token)).status_code == 403, (
        f"{path}: another user"
    )
    assert client.get(missing, headers=_headers(token)).status_code == 404, f"{path}: unknown id"
    assert client.get(owned, headers=_headers(token)).status_code == 200, f"{path}: the owner"


# --- 3. the schedule stays inside the declared window ------------------

# The DECLARED surface, sampled at its corners and a few interior
# points: the API accepts 2-56 days and any hours-per-day up to 16.
# Looped inside one test rather than parametrised, so the taxonomy and
# the fixture world are built once — the same shape
# tests/test_roadmap_api.py's own duration loops use.
_DURATIONS = (2, 3, 7, 14, 28, 56)
_HOURS_PER_DAY = (0.5, 1.0, 2.5, 16.0)


def test_the_schedule_stays_inside_the_declared_window(client: TestClient) -> None:
    """FIVE INVARIANTS, ON ONE PLAN, ACROSS THE WHOLE SURFACE.

    Asserted together rather than in five tests because the interesting
    failure is a change that satisfies one by breaking another — days
    that tile perfectly because an item was allowed to run past the
    window, or hours that fit because a step lost its days.

    The fixture spans all three gap states, so the per-state phase
    ladders (five rungs for a missing skill, three for a weak one) are
    all in play. Every existing test of these properties uses items of a
    single state.
    """
    token, _, _ = _candidate_with_every_gap_kind(client)

    for duration in _DURATIONS:
        for hours in _HOURS_PER_DAY:
            payload = _roadmap(client, token, duration_days=duration, hours_per_day=hours)
            where = f"{duration} days at {hours} h/day"

            items = _items(payload)
            steps = _steps(payload)
            assert items, where
            assert steps, where

            # (a) SCHEDULED HOURS NEVER EXCEED AVAILABLE HOURS. The
            # tolerance is rounding, not slack: each step's hours are
            # rounded to one decimal independently, so a plan of n steps
            # can drift by up to n/20 of an hour from the exact product.
            assert payload["total_hours"] == pytest.approx(duration * hours, abs=0.05), where
            assert sum(step["estimated_hours"] for step in steps) <= payload["total_hours"] + 0.5, (
                where
            )

            # (b) STEPS TILE THEIR ITEM'S SPAN — contiguous, no overlap,
            # no gap, and summing to the span the item claims.
            #
            # GATHERED ACROSS WEEKS, not read off one nested copy. An
            # item is nested under EVERY week it occupies and carries
            # only that week's steps (app/api/v1/roadmap.py), while its
            # own `start_day`/`end_day` describe the whole span — so a
            # fortnight-long item appears twice with half its steps
            # each, and checking one copy against the full span compares
            # two different things.
            steps_by_item: dict[str, list[dict[str, Any]]] = {}
            for week in payload["weeks"]:
                for nested in week["items"]:
                    steps_by_item.setdefault(nested["item_id"], []).extend(nested["steps"])

            for item in items:
                item_steps = sorted(
                    steps_by_item[item["item_id"]], key=lambda step: step["start_day"]
                )
                assert item_steps[0]["start_day"] == item["start_day"], where
                assert item_steps[-1]["end_day"] == item["end_day"], where
                for earlier, later in zip(item_steps, item_steps[1:], strict=False):
                    assert later["start_day"] == earlier["end_day"] + 1, where
                assert sum(step["end_day"] - step["start_day"] + 1 for step in item_steps) == (
                    item["end_day"] - item["start_day"] + 1
                ), where

            # (c) ITEM SPANS TILE THE SCHEDULED DAYS, and the unscheduled
            # remainder is declared rather than absorbed.
            assert items[0]["start_day"] == 1, where
            assert items[-1]["end_day"] == payload["scheduled_days"], where
            for earlier, later in zip(items, items[1:], strict=False):
                assert later["start_day"] == earlier["end_day"] + 1, where
            assert (
                payload["scheduled_days"] + payload["unscheduled_days"] == payload["duration_days"]
            ), where
            assert payload["scheduled_days"] <= duration, where

            # (d) TOTAL STEPS STAY UNDER WHAT THE RESPONSE SCHEMA
            # DECLARES. app/roadmap/schema.py bounds `steps` at
            # MAX_STEPS; a plan that exceeded it could never be narrated.
            assert len(steps) <= MAX_STEPS, where

            # (e) WEEKS STAY INSIDE MAX_WEEKS, and inside this window's
            # own week count — the tighter of the two.
            weeks = [week["week"] for week in payload["weeks"]]
            assert weeks == list(range(1, week_count(duration) + 1)), where
            assert max(weeks) <= MAX_WEEKS, where
            assert all(1 <= step["week"] <= week_count(duration) for step in steps), where
