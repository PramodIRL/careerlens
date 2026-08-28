"""The roadmap endpoint, end to end (Prompt 6.3).

The load-bearing tests here, which should not be softened:

  * the USER's ordering drives the plan — not `skill_match_v1`
  * a job outside Top-N cannot reach the plan, the facts, or the model
  * a strongly matched skill never becomes a roadmap item
  * a rejected narrative costs the wording and none of the substance
  * `/match` and `/gaps` return byte-identical JSON either side of a
    roadmap request, and the roadmap writes no row
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Generator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.main import app
from app.models.candidate_skill import CandidateSkill
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.rate_limit import _request_log
from app.roadmap.priority import STATE_WEIGHTS, GapState
from app.settings import get_settings
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_JOBS = "/api/v1/saved-jobs"
_ROADMAP = "/api/v1/roadmap"

# Two postings with a deliberate overlap: Docker is required by both, so
# recurrence has something to add up.
_AWS_JOB = "We need AWS and Docker. Python is required. Kubernetes preferred."
_DOCKER_JOB = "Docker is required. Redis is required. PostgreSQL preferred."


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    _request_log.clear()


def _run[T](coro_factory: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def _inner() -> T:
        engine = create_async_engine(
            get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
        )
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                return await coro_factory(session)
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def _seed_taxonomy() -> None:
    _run(lambda session: seed_skill_taxonomy(session))


def _new_user(client: TestClient) -> tuple[str, str]:
    email = f"{uuid.uuid4()}@example.com"
    register = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return str(login.json()["access_token"]), str(register.json()["id"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_job(client: TestClient, token: str, description: str, company: str = "Acme") -> str:
    response = client.post(
        _JOBS,
        headers=_headers(token),
        json={"company": company, "title": "Engineer", "description": description},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _roadmap(client: TestClient, token: str, **params: Any) -> dict[str, Any]:
    response = client.get(_ROADMAP, headers=_headers(token), params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _roadmap_response(client: TestClient, token: str, **params: Any) -> Any:
    return client.get(_ROADMAP, headers=_headers(token), params=params)


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The DECIDED items, once each, in schedule order.

    An item is nested under every week it really occupies (6.4b), so a
    fortnight-long priority appears twice in the response — which is
    what stops a week that holds work rendering as empty. These tests
    are about the plan, not the layout, so the repeat is folded here.
    Use `_steps` for the day-level view.
    """
    seen: dict[str, dict[str, Any]] = {}
    for week in payload["weeks"]:
        for item in week["items"]:
            seen.setdefault(item["item_id"], item)
    return list(seen.values())


def _steps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every step in the plan, in day order."""
    return sorted(
        (step for week in payload["weeks"] for item in week["items"] for step in item["steps"]),
        key=lambda step: step["start_day"],
    )


def _names(payload: dict[str, Any]) -> list[str]:
    return [item["skill_name"] for item in _items(payload)]


def _give_skill(user_id: str, skill_name: str, status: str) -> None:
    def _write(session: AsyncSession) -> Awaitable[None]:
        async def _inner() -> None:
            skill = await session.scalar(select(Skill).where(Skill.slug == skill_name.casefold()))
            assert skill is not None, f"{skill_name!r} not in the seeded taxonomy"
            candidate = CandidateSkill(user_id=uuid.UUID(user_id), skill_id=skill.id, status=status)
            session.add(candidate)
            await session.flush()
            session.add(
                SkillEvidence(
                    candidate_skill_id=candidate.id,
                    source_type="resume",
                    source_identifier=str(uuid.uuid4()),
                    excerpt=f"Worked with {skill_name}",
                    extraction_method="resume_alias_match",
                    confidence=Decimal("0.90"),
                )
            )
            await session.commit()

        return _inner()

    _run(_write)


# --- the empty case -----------------------------------------------------


def test_no_saved_jobs_is_a_statement_about_the_input(client: TestClient) -> None:
    """ "You have not saved any jobs" is not "no gaps found" — the first
    is about the input, the second a claim about the person."""
    _seed_taxonomy()
    token, _ = _new_user(client)

    payload = _roadmap(client, token)

    assert payload["has_selected_jobs"] is False
    assert payload["selected_job_count"] == 0
    assert _items(payload) == []


# --- user priority ------------------------------------------------------


def test_user_priority_drives_the_plan(client: TestClient) -> None:
    """THE PRIMARY SIGNAL. Reordering the jobs reorders the roadmap,
    with no change to any skill, evidence or score."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    aws_job = _create_job(client, token, "AWS is required.", company="AwsCo")
    redis_job = _create_job(client, token, "Redis is required.", company="RedisCo")

    # Newest first by default, so the Redis job is currently #1.
    assert _names(_roadmap(client, token))[0] == "Redis"

    response = client.put(
        f"{_JOBS}/order", headers=_headers(token), json={"job_ids": [aws_job, redis_job]}
    )
    assert response.status_code == 200, response.text

    assert _names(_roadmap(client, token))[0] == "AWS"


def test_a_job_outside_top_n_cannot_reach_the_plan(client: TestClient) -> None:
    """Excluded BEFORE aggregation: the skill it alone demands is
    absent, and no item cites it as a cause."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    kept = _create_job(client, token, "AWS is required.", company="KeptCo")
    dropped = _create_job(client, token, "Kubernetes is required.", company="DroppedCo")

    client.put(f"{_JOBS}/order", headers=_headers(token), json={"job_ids": [kept, dropped]})
    payload = _roadmap(client, token, top_n=1)

    assert payload["selected_job_count"] == 1
    assert "Kubernetes" not in _names(payload)
    cited = {job["saved_job_id"] for item in _items(payload) for job in item["affected_jobs"]}
    assert dropped not in cited
    # And the excluded company is nowhere in the rendered plan at all.
    assert "DroppedCo" not in json.dumps(payload)


def test_recurring_gaps_rise_within_their_band(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)
    _create_job(client, token, _DOCKER_JOB)

    items = {item["skill_name"]: item for item in _items(_roadmap(client, token))}

    # Docker is required by both jobs; Redis by one.
    assert items["Docker"]["score"] > items["Redis"]["score"]
    assert len(items["Docker"]["affected_jobs"]) == 2
    assert items["Docker"]["recurrence"] > items["Redis"]["recurrence"]


# --- states -------------------------------------------------------------


def test_required_outranks_preferred_outranks_weak(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _create_job(client, token, "Redis is required. Kubernetes preferred. Docker preferred.")
    # Suggested = evidence exists, unreviewed. skill_gap_v1 calls that
    # needs_confirmation; the roadmap calls it weak evidence.
    _give_skill(user_id, "Docker", "suggested")

    items = _items(_roadmap(client, token))
    states = [item["state"] for item in items]

    assert states == sorted(states, key=lambda state: -STATE_WEIGHTS[GapState(state)])
    assert items[0]["state"] == "missing_required"


def test_a_confirmed_skill_is_not_a_roadmap_item(client: TestClient) -> None:
    """A strongly matched skill is not a gap. Nothing has to say "skip
    matched skills" — a satisfied requirement produces no demand."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _create_job(client, token, "Redis is required. AWS is required.")
    _give_skill(user_id, "Redis", "confirmed")

    names = _names(_roadmap(client, token))

    assert "Redis" not in names
    assert "AWS" in names


def test_a_rejected_skill_is_not_resurrected(client: TestClient) -> None:
    """The user disowned it. Putting it back in a plan would override
    their own decision."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _create_job(client, token, "Redis is required. AWS is required.")
    _give_skill(user_id, "Redis", "rejected")

    assert "Redis" not in _names(_roadmap(client, token))


def test_weak_evidence_asks_for_evidence(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _create_job(client, token, "Docker preferred.")
    _give_skill(user_id, "Docker", "suggested")

    item = _items(_roadmap(client, token))[0]

    assert item["state"] == "weak_evidence"
    assert "have not reviewed yet" in item["why"]
    # The mock asks for documentation, never for relearning.
    assert "Document" in (item["task"] or "")


# --- time ---------------------------------------------------------------


def test_time_changes_depth_not_the_top_item(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)
    _create_job(client, token, _DOCKER_JOB)

    lean = _roadmap(client, token, duration_days=7, hours_per_day=0.5)
    generous = _roadmap(client, token, duration_days=56, hours_per_day=4)

    assert len(_items(lean)) <= len(_items(generous))
    assert _names(lean)[0] == _names(generous)[0]
    assert lean["total_hours"] == 3.5
    assert generous["total_hours"] == 224.0
    # Weeks follow the duration rather than a fixed four.
    assert len(lean["weeks"]) == 1
    assert len(generous["weeks"]) == 8


def test_the_plan_covers_the_scheduled_days_exactly(client: TestClient) -> None:
    """The plan really occupies the days it claims: contiguous spans, no
    overflow past the window and no unclaimed tail inside it.

    `scheduled_days` rather than `duration_days` since 6.4b: no single
    skill may run past a fortnight, so a short list of gaps leaves an
    honest remainder rather than being stretched to fill the window.
    """
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)
    _create_job(client, token, _DOCKER_JOB)

    payload = _roadmap(client, token, duration_days=30, hours_per_day=1)
    items = _items(payload)

    assert items[0]["start_day"] == 1
    assert items[-1]["end_day"] == payload["scheduled_days"]
    for earlier, later in zip(items, items[1:], strict=False):
        assert later["start_day"] == earlier["end_day"] + 1

    # And the STEPS tile the same window, with nothing overlapping and
    # nothing beyond it.
    steps = _steps(payload)
    assert steps[0]["start_day"] == 1
    assert steps[-1]["end_day"] == payload["scheduled_days"]
    for earlier, later in zip(steps, steps[1:], strict=False):
        assert later["start_day"] == earlier["end_day"] + 1
    assert all(step["end_day"] <= payload["duration_days"] for step in steps)

    # NEVER MORE WORK THAN THE CANDIDATE DECLARED. The bound holds over
    # steps, which is where the hours now live.
    assert sum(step["estimated_hours"] for step in steps) <= payload["total_hours"] + 0.5
    assert payload["scheduled_days"] + payload["unscheduled_days"] == payload["duration_days"]


def test_every_week_of_the_window_is_rendered(client: TestClient) -> None:
    """Including any that hold no work — a plan that silently skips
    week 3 looks like a bug."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    payload = _roadmap(client, token, duration_days=28, hours_per_day=1)

    assert [week["week"] for week in payload["weeks"]] == [1, 2, 3, 4]
    assert payload["weeks"][0]["label"] == "Week 1 · Days 1–7"
    assert payload["weeks"][3]["end_day"] == 28


def test_each_week_with_work_carries_a_checkpoint(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    payload = _roadmap(client, token)

    for week in payload["weeks"]:
        if week["items"]:
            assert week["checkpoint"], week["week"]
            assert week["focus"]


def test_every_item_says_what_to_do_and_what_comes_out(client: TestClient) -> None:
    """The four questions a roadmap item has to answer, present on every
    one of them."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    for item in _items(_roadmap(client, token)):
        assert item["skill_name"]  # what to learn
        assert item["task"]  # what to do
        assert item["outcome"]  # what comes out
        assert item["success_criteria"]  # what you can then do
        assert item["estimated_hours"] > 0
        assert item["affected_jobs"]


# --- grounding ----------------------------------------------------------


def test_every_item_references_real_jobs_and_a_real_state(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    first = _create_job(client, token, _AWS_JOB)
    second = _create_job(client, token, _DOCKER_JOB)
    owned = {first, second}

    payload = _roadmap(client, token)
    assert _items(payload)

    for item in _items(payload):
        assert item["affected_jobs"], item["skill_name"]
        assert {job["saved_job_id"] for job in item["affected_jobs"]} <= owned
        assert item["state"] in {state.value for state in GapState}
        # Score is reproducible by hand from the two echoed terms.
        assert item["score"] == item["state_weight"] + item["recurrence"]
        assert item["task"] and item["success_criteria"]
        assert item["why"]


def test_the_plan_survives_a_rejected_narrative(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAIL CLOSED, STILL USEFUL. Losing the wording must not cost the
    user the priorities."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    good = _roadmap(client, token)

    class _BrokenProvider:
        @property
        def name(self) -> str:
            return "mock"

        async def complete(self, request: object) -> str:
            return "{ not json"

    monkeypatch.setattr("app.api.v1.roadmap.get_roadmap_provider", lambda: _BrokenProvider())
    rejected = _roadmap(client, token)

    assert rejected["narrative_status"] == "rejected"
    assert rejected["reason"] == "malformed_json"
    assert rejected["overview"] is None
    # Every deterministic field survived, unchanged.
    assert _names(rejected) == _names(good)
    for item in _items(rejected):
        assert item["task"] is None
        assert item["success_criteria"] is None
        assert item["why"] and item["affected_jobs"] and item["score"]


def test_a_narrative_naming_an_unknown_item_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model cannot introduce a skill: the item_id set must match
    exactly, so an invented entry has nowhere to belong."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    class _InventingProvider:
        @property
        def name(self) -> str:
            return "mock"

        async def complete(self, request: object) -> str:
            return json.dumps(
                {
                    "schema_version": "roadmap_narrative_v1",
                    "overview": "A plan.",
                    "weeks": [],
                    "items": [
                        {
                            "item_id": str(uuid.uuid4()),
                            "task": "Learn something nobody asked for.",
                            "outcome": "Something nobody planned.",
                            "success_criteria": "It is done.",
                        }
                    ],
                }
            )

    monkeypatch.setattr("app.api.v1.roadmap.get_roadmap_provider", lambda: _InventingProvider())
    payload = _roadmap(client, token)

    assert payload["narrative_status"] == "rejected"
    assert payload["reason"] == "unknown_evidence_id"
    assert all(item["task"] is None for item in _items(payload))


# --- dynamic bounds -----------------------------------------------------


def test_top_n_may_not_exceed_the_saved_job_count(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)
    _create_job(client, token, _DOCKER_JOB)

    response = _roadmap_response(client, token, top_n=3)

    assert response.status_code == 422
    # The message names the real ceiling, so a client can correct itself.
    assert "2 saved jobs" in response.json()["detail"]


def test_top_n_equal_to_the_count_means_all(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)
    _create_job(client, token, _DOCKER_JOB)

    assert _roadmap(client, token, top_n=2)["selected_job_count"] == 2


def test_no_saved_jobs_is_never_a_validation_error(client: TestClient) -> None:
    """Any top_n exceeds a count of zero, but "you have not saved any
    jobs yet" is an empty state, not a client mistake."""
    _seed_taxonomy()
    token, _ = _new_user(client)

    response = _roadmap_response(client, token, top_n=5)

    assert response.status_code == 200
    assert response.json()["has_selected_jobs"] is False


def test_top_n_below_one_is_refused(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    assert _roadmap_response(client, token, top_n=0).status_code == 422


def test_hours_per_day_is_capped_at_sixteen(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    assert _roadmap_response(client, token, hours_per_day=17).status_code == 422
    assert _roadmap_response(client, token, hours_per_day=16).status_code == 200
    assert _roadmap_response(client, token, hours_per_day=0).status_code == 422


def test_the_response_reports_the_saved_job_count(client: TestClient) -> None:
    """So the client's "N of X" control re-bounds itself after a job is
    added or deleted, without a second request."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)
    _create_job(client, token, _DOCKER_JOB)

    payload = _roadmap(client, token, top_n=1)

    assert payload["saved_job_count"] == 2
    assert payload["selected_job_count"] == 1


def test_a_narrative_missing_a_week_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping a week silently loses whatever was scheduled in it,
    which is the quieter failure and the more damaging one."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    class _WeeklessProvider:
        @property
        def name(self) -> str:
            return "mock"

        async def complete(self, request: Any) -> str:
            payload = json.loads(request.data_json)["untrusted_data"]
            return json.dumps(
                {
                    "schema_version": "roadmap_narrative_v1",
                    "overview": "A plan.",
                    "weeks": [],
                    "items": [
                        {
                            "item_id": item["item_id"],
                            "task": "Build something.",
                            "outcome": "A thing.",
                            "success_criteria": "It works.",
                        }
                        for item in payload["items"]
                    ],
                }
            )

    monkeypatch.setattr("app.api.v1.roadmap.get_roadmap_provider", lambda: _WeeklessProvider())
    payload = _roadmap(client, token)

    assert payload["narrative_status"] == "rejected"
    assert payload["reason"] == "ungrounded_claim"
    # And the schedule survived it.
    assert _items(payload)
    assert all(item["start_day"] > 0 for item in _items(payload))


# --- the guarantee about everything else --------------------------------


def test_the_roadmap_changes_no_score_and_writes_no_row(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    job_id = _create_job(client, token, _AWS_JOB)
    _give_skill(user_id, "Python", "confirmed")

    before_match = client.get(f"{_JOBS}/{job_id}/match", headers=_headers(token)).json()
    before_gaps = client.get(f"{_JOBS}/{job_id}/gaps", headers=_headers(token)).json()
    before_rows = _run(
        lambda session: session.scalar(
            select(SkillEvidence).where(SkillEvidence.id.is_not(None)).limit(1)
        )
    )

    _roadmap(client, token)
    _roadmap(client, token, top_n=1, duration_days=14, hours_per_day=2)

    after_match = client.get(f"{_JOBS}/{job_id}/match", headers=_headers(token)).json()
    after_gaps = client.get(f"{_JOBS}/{job_id}/gaps", headers=_headers(token)).json()

    assert after_match == before_match
    assert after_gaps == before_gaps
    after_rows = _run(
        lambda session: session.scalar(
            select(SkillEvidence).where(SkillEvidence.id.is_not(None)).limit(1)
        )
    )
    assert (after_rows is None) == (before_rows is None)


def test_two_identical_requests_return_identical_json(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)
    _create_job(client, token, _DOCKER_JOB)

    first = client.get(_ROADMAP, headers=_headers(token)).text
    second = client.get(_ROADMAP, headers=_headers(token)).text

    assert first == second


def test_the_roadmap_is_scoped_to_the_caller(client: TestClient) -> None:
    _seed_taxonomy()
    owner_token, _ = _new_user(client)
    _create_job(client, owner_token, _AWS_JOB, company="OwnerCo")

    stranger_token, _ = _new_user(client)
    payload = _roadmap(client, stranger_token)

    assert payload["has_selected_jobs"] is False
    assert "OwnerCo" not in json.dumps(payload)


# =====================================================================
# 6.4b — day-level steps, honest coverage, and short plans
# =====================================================================


def test_a_two_day_plan_is_accepted(client: TestClient) -> None:
    """An interview on Thursday is a legitimate plan. The floor was a
    week only because the plan used to be laid out in weeks; it is laid
    out in days now, and weeks are a presentation grouping."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    payload = _roadmap(client, token, duration_days=2, hours_per_day=2)

    assert payload["duration_days"] == 2
    assert _items(payload)
    assert _steps(payload)


def test_a_one_day_plan_is_refused(client: TestClient) -> None:
    """A plan with no second day is a task list — there is nothing to
    sequence."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    assert _roadmap_response(client, token, duration_days=1).status_code == 422
    assert _roadmap_response(client, token, duration_days=0).status_code == 422
    assert _roadmap_response(client, token, duration_days=57).status_code == 422


@pytest.mark.parametrize(("duration_days", "weeks"), [(2, 1), (3, 1), (5, 1), (7, 1), (9, 2)])
def test_a_short_plan_does_not_assume_four_weeks(
    client: TestClient, duration_days: int, weeks: int
) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    payload = _roadmap(client, token, duration_days=duration_days, hours_per_day=2)

    assert len(payload["weeks"]) == weeks
    assert payload["weeks"][-1]["end_day"] == duration_days


def test_work_reaches_every_week_it_really_occupies(client: TestClient) -> None:
    """THE REPORTED FAILURE. Two items over 28 days used to render as
    "Week 1: AWS", "Week 2: nothing", "Week 3: Kubernetes", "Week 4:
    nothing" — while the candidate was in fact meant to be working
    through weeks 2 and 4."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, "AWS is required. Kubernetes is required.")

    payload = _roadmap(client, token, duration_days=28, hours_per_day=2)

    assert payload["coverage"] == "full"
    for week in payload["weeks"]:
        assert week["items"], f"week {week['week']} holds no work"
        assert all(item["steps"] for item in week["items"])
    # And a week only ever shows the steps that belong to it.
    for week in payload["weeks"]:
        for item in week["items"]:
            assert all(step["week"] == week["week"] for step in item["steps"])
            assert all(
                week["start_day"] <= step["start_day"] <= step["end_day"] <= week["end_day"]
                for step in item["steps"]
            )


def test_no_work_is_scheduled_outside_the_declared_window(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)
    _create_job(client, token, _DOCKER_JOB)

    for duration in (2, 5, 14, 28, 56):
        payload = _roadmap(client, token, duration_days=duration, hours_per_day=1)
        steps = _steps(payload)
        assert steps
        assert all(1 <= step["start_day"] <= step["end_day"] <= duration for step in steps)
        assert sum(step["estimated_hours"] for step in steps) <= payload["total_hours"] + 0.5


def test_a_priority_item_is_broken_into_educational_steps(client: TestClient) -> None:
    """The mentoring half: an item is a skill, its steps are the days,
    and each step says what to do and how you know you finished it."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, "AWS is required. Kubernetes is required.")

    payload = _roadmap(client, token, duration_days=28, hours_per_day=2)

    assert payload["narrative_status"] == "generated", payload.get("reason")
    steps = _steps(payload)
    assert len(steps) > len(_items(payload))
    for step in steps:
        # The deterministic half is always present...
        assert step["phase"]
        assert step["estimated_hours"] > 0
        # ...and the written half came back with it.
        assert step["task"]
        assert step["done_when"]


def test_insufficient_material_is_reported_not_padded(client: TestClient) -> None:
    """When the candidate's own jobs do not justify the window they
    asked for, the remainder is stated rather than filled with invented
    learning."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, "AWS is required.")

    payload = _roadmap(client, token, duration_days=56, hours_per_day=1)

    assert payload["coverage"] == "partial"
    assert payload["unscheduled_days"] > 0
    assert payload["scheduled_days"] + payload["unscheduled_days"] == 56
    assert max(step["end_day"] for step in _steps(payload)) == payload["scheduled_days"]


def test_the_steps_survive_a_rejected_narrative(client: TestClient) -> None:
    """The days and the phases are deterministic, so a rejection costs
    the wording and none of the schedule."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    class _BrokenProvider:
        @property
        def name(self) -> str:
            return "mock"

        async def complete(self, request: object) -> str:
            return "{ not json"

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.api.v1.roadmap.get_roadmap_provider", lambda: _BrokenProvider())
        payload = _roadmap(client, token, duration_days=28, hours_per_day=2)

    assert payload["narrative_status"] == "rejected"
    steps = _steps(payload)
    assert steps
    for step in steps:
        assert step["phase"] and step["estimated_hours"] > 0
        assert step["task"] is None and step["done_when"] is None


def test_a_narrative_inventing_a_step_is_rejected(client: TestClient) -> None:
    """The model cannot add a block of days. Every step carries hours,
    so an invented one is work outside the budget the candidate
    declared."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    from app.roadmap.provider import MockRoadmapProvider

    class _InventingProvider(MockRoadmapProvider):
        def complete_sync(self, request: object) -> str:
            payload = json.loads(super().complete_sync(request))
            payload["steps"].append(
                {
                    "step_id": "not-a-real-step:9",
                    "task": "Do something nobody scheduled.",
                    "done_when": "Never.",
                }
            )
            return json.dumps(payload)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.api.v1.roadmap.get_roadmap_provider", lambda: _InventingProvider())
        payload = _roadmap(client, token, duration_days=28, hours_per_day=2)

    assert payload["narrative_status"] == "rejected"
    assert payload["reason"] == "unknown_evidence_id"
    # And the schedule is intact regardless.
    assert _steps(payload)


def test_a_narrative_dropping_a_step_is_rejected(client: TestClient) -> None:
    """The quieter failure: a hole in the calendar the UI would render
    as a day with nothing in it."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)

    from app.roadmap.provider import MockRoadmapProvider

    class _DroppingProvider(MockRoadmapProvider):
        def complete_sync(self, request: object) -> str:
            payload = json.loads(super().complete_sync(request))
            payload["steps"].pop()
            return json.dumps(payload)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.api.v1.roadmap.get_roadmap_provider", lambda: _DroppingProvider())
        payload = _roadmap(client, token, duration_days=28, hours_per_day=2)

    assert payload["narrative_status"] == "rejected"
    assert payload["reason"] == "ungrounded_claim"


def test_the_narrative_cannot_change_the_deterministic_priority(client: TestClient) -> None:
    """THE BOUNDARY. The narrative is a MAP keyed by decided ids, so
    reordering it changes nothing: the plan comes back in
    `roadmap_priority_v1`'s order either way."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    _create_job(client, token, _AWS_JOB)
    _create_job(client, token, _DOCKER_JOB)

    from app.roadmap.provider import MockRoadmapProvider

    class _ReorderingProvider(MockRoadmapProvider):
        def complete_sync(self, request: object) -> str:
            payload = json.loads(super().complete_sync(request))
            payload["items"].reverse()
            payload["steps"].reverse()
            return json.dumps(payload)

    straight = _roadmap(client, token, duration_days=28, hours_per_day=2)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.api.v1.roadmap.get_roadmap_provider", lambda: _ReorderingProvider())
        shuffled = _roadmap(client, token, duration_days=28, hours_per_day=2)

    assert shuffled["narrative_status"] == "generated"
    assert _names(shuffled) == _names(straight)
    assert [item["score"] for item in _items(shuffled)] == [
        item["score"] for item in _items(straight)
    ]
    assert [step["step_id"] for step in _steps(shuffled)] == [
        step["step_id"] for step in _steps(straight)
    ]


def test_a_matched_skill_never_becomes_a_step(client: TestClient) -> None:
    """A skill the candidate satisfies with reviewed evidence produces
    no demand, so it reaches neither the item list nor the calendar."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _create_job(client, token, "AWS is required. Docker is required.")
    _give_skill(user_id, "AWS", "confirmed")

    payload = _roadmap(client, token, duration_days=28, hours_per_day=2)

    assert "AWS" not in _names(payload)
    assert all(not step["step_id"].startswith("AWS") for step in _steps(payload))
