"""End-to-end job requirement extraction, reconciliation and ownership
(Prompt 4.2).

The load-bearing tests here, which should not be softened:

  * DESIRED-STATE reconciliation — an identical rerun writes nothing and
    preserves ids and created_at; an edit updates in place, inserts and
    deletes
  * editing a NON-description field leaves every requirement untouched
  * THE DATA BOUNDARY — saving a job full of skill names creates zero
    candidate_skills and zero skill_evidence
  * ownership is structural: requirements are reachable only through a
    saved job whose owner is checked
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.main import app
from app.models.candidate_skill import CandidateSkill
from app.models.job_requirement import JobSkillRequirement
from app.models.skill_evidence import SkillEvidence
from app.rate_limit import _request_log
from app.settings import get_settings
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_BASE = "/api/v1/saved-jobs"


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


def _token(client: TestClient) -> str:
    email = f"{uuid.uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return str(login.json()["access_token"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_job(client: TestClient, token: str, description: str, **overrides: Any) -> str:
    body = {
        "company": "Fictional Widgets Ltd",
        "title": "Backend Engineer",
        "description": description,
        **overrides,
    }
    response = client.post(_BASE, headers=_headers(token), json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _requirements(client: TestClient, token: str, job_id: str) -> list[dict[str, Any]]:
    response = client.get(f"{_BASE}/{job_id}/requirements", headers=_headers(token))
    assert response.status_code == 200, response.text
    return list(response.json())


def _levels(client: TestClient, token: str, job_id: str) -> dict[str, str]:
    return {
        row["skill_name"]: row["requirement_level"] for row in _requirements(client, token, job_id)
    }


def _patch(client: TestClient, token: str, job_id: str, **body: Any) -> Any:
    return client.patch(f"{_BASE}/{job_id}", headers=_headers(token), json=body)


def _rows(job_id: str) -> list[JobSkillRequirement]:
    async def _read(session: AsyncSession) -> list[JobSkillRequirement]:
        result = await session.scalars(
            select(JobSkillRequirement).where(JobSkillRequirement.saved_job_id == uuid.UUID(job_id))
        )
        return list(result.all())

    return _run(_read)


def _snapshot(job_id: str) -> set[tuple[Any, ...]]:
    """Ids AND timestamps — the strict form of "nothing churned"."""
    return {
        (r.id, r.skill_id, r.requirement_level, r.excerpt, r.created_at, r.updated_at)
        for r in _rows(job_id)
    }


# --- extraction on create ---------------------------------------------


def test_the_briefs_example_produces_the_expected_levels(client: TestClient) -> None:
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(
        client,
        token,
        "We require Python and PostgreSQL. Experience with FastAPI is preferred. Docker is a plus.",
    )

    assert _levels(client, token, job_id) == {
        "Python": "required",
        "PostgreSQL": "required",
        "FastAPI": "preferred",
        "Docker": "preferred",
    }


def test_a_requirement_explains_itself(client: TestClient) -> None:
    """Every field the Evidence-First rule requires: which skill, which
    spelling, what text, how strongly, how sure, what derived it."""
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Python is required for this role.")

    row = _requirements(client, token, job_id)[0]

    assert row["skill_name"] == "Python"
    assert row["skill_category"] == "language"
    assert row["requirement_level"] == "required"
    assert row["matched_term"] == "Python"
    assert row["excerpt"] == "Python is required for this role"
    assert row["confidence"] == 0.90
    assert row["extraction_method"] == "job_description_match"


def test_an_alias_match_carries_alias_confidence(client: TestClient) -> None:
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Strong k8s experience is required.")

    row = _requirements(client, token, job_id)[0]

    assert row["skill_name"] == "Kubernetes"
    assert row["matched_term"] == "k8s"
    # Straight from the existing matcher's CONFIDENCE_BY_KIND, unchanged.
    assert row["confidence"] == 0.75


def test_word_boundaries_prevent_false_matches(client: TestClient) -> None:
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "We build with JavaScript every day here.")

    names = set(_levels(client, token, job_id))

    assert "JavaScript" in names
    assert "Java" not in names


def test_a_description_with_no_taxonomy_skill_produces_nothing(client: TestClient) -> None:
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "We are hiring a friendly person to write poems.")

    assert _requirements(client, token, job_id) == []


def test_a_repeated_skill_takes_the_strongest_level(client: TestClient) -> None:
    """One row per skill, at the strongest level any occurrence
    justifies — a posting that calls Python required somewhere requires
    it, however casually it names it elsewhere."""
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(
        client,
        token,
        "Experience with Python. Later: Python is required. We also like Python.",
    )

    rows = [r for r in _requirements(client, token, job_id) if r["skill_name"] == "Python"]

    assert len(rows) == 1
    assert rows[0]["requirement_level"] == "required"


def test_negated_wording_does_not_become_required(client: TestClient) -> None:
    """The brief's explicit counter-examples, end to end."""
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Python is not required. No Docker experience necessary.")

    assert _levels(client, token, job_id) == {"Python": "mentioned", "Docker": "mentioned"}


def test_requirements_are_ordered_strongest_first(client: TestClient) -> None:
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Docker is a plus. Python is required. We also use Redis.")

    levels = [row["requirement_level"] for row in _requirements(client, token, job_id)]

    assert levels == ["required", "preferred", "mentioned"]


# --- reconciliation ----------------------------------------------------


def test_an_identical_rerun_changes_absolutely_nothing(client: TestClient) -> None:
    """Re-sending the SAME description must not churn: same ids, same
    created_at, same updated_at."""
    _seed_taxonomy()
    token = _token(client)
    description = "Python is required. Docker is preferred."
    job_id = _create_job(client, token, description)
    before = _snapshot(job_id)

    assert _patch(client, token, job_id, description=description).status_code == 200

    assert _snapshot(job_id) == before


def test_editing_a_non_description_field_leaves_requirements_untouched(
    client: TestClient,
) -> None:
    """Nothing about the text moved, so nothing about the requirements
    may move — including their timestamps."""
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Python is required.")
    before = _snapshot(job_id)

    assert _patch(client, token, job_id, company="Renamed Ltd").status_code == 200
    assert _patch(client, token, job_id, location="Remote").status_code == 200

    assert _snapshot(job_id) == before


def test_the_briefs_reconciliation_example(client: TestClient) -> None:
    """The worked example from the brief:

        "Python is required. Docker is preferred."
     -> "Python is preferred. PostgreSQL is required."

    Python UPDATES IN PLACE, PostgreSQL inserts, Docker is removed.
    """
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Python is required. Docker is preferred.")
    assert _levels(client, token, job_id) == {"Python": "required", "Docker": "preferred"}
    python_before = next(r for r in _rows(job_id) if r.requirement_level == "required")

    assert (
        _patch(
            client, token, job_id, description="Python is preferred. PostgreSQL is required."
        ).status_code
        == 200
    )

    assert _levels(client, token, job_id) == {
        "Python": "preferred",
        "PostgreSQL": "required",
    }
    # Updated in place: same row, not a delete-and-reinsert.
    python_after = next(r for r in _rows(job_id) if r.skill_id == python_before.skill_id)
    assert python_after.id == python_before.id
    assert python_after.created_at == python_before.created_at


def test_a_skill_removed_from_the_description_disappears(client: TestClient) -> None:
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Python and Docker are required.")

    _patch(client, token, job_id, description="Python is required.")

    assert set(_levels(client, token, job_id)) == {"Python"}


def test_editing_down_to_no_skills_clears_every_requirement(client: TestClient) -> None:
    """An empty desired set is legitimate — the previous run's rows must
    not survive as leftovers."""
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Python is required.")

    _patch(client, token, job_id, description="We are hiring a poet with a kind heart.")

    assert _requirements(client, token, job_id) == []


def test_reconciliation_never_duplicates_a_skill(client: TestClient) -> None:
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Python is required.")

    for text in ("Python is preferred.", "Python is required.", "Python is a plus."):
        _patch(client, token, job_id, description=text)

    assert len(_rows(job_id)) == 1


# --- ownership ---------------------------------------------------------


def test_requirements_require_authentication(client: TestClient) -> None:
    assert client.get(f"{_BASE}/{uuid.uuid4()}/requirements").status_code == 401


def test_another_user_cannot_read_requirements(client: TestClient) -> None:
    _seed_taxonomy()
    owner = _token(client)
    other = _token(client)
    job_id = _create_job(client, owner, "Python is required.")

    response = client.get(f"{_BASE}/{job_id}/requirements", headers=_headers(other))

    assert response.status_code == 403


def test_requirements_for_a_nonexistent_job_are_404(client: TestClient) -> None:
    _seed_taxonomy()
    token = _token(client)

    response = client.get(f"{_BASE}/{uuid.uuid4()}/requirements", headers=_headers(token))

    assert response.status_code == 404


def test_there_is_no_direct_requirement_mutation_route(client: TestClient) -> None:
    """Requirements are DERIVED. The way to change them is to edit the
    description; a mutation endpoint would let stored rows drift from
    the text that justifies them."""
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Python is required.")
    requirement_id = _requirements(client, token, job_id)[0]["id"]

    # No route addresses a requirement by its own id, in any shape.
    for url in (
        f"/api/v1/job-requirements/{requirement_id}",
        f"{_BASE}/{job_id}/requirements/{requirement_id}",
    ):
        assert client.get(url, headers=_headers(token)).status_code in (404, 405)
        assert client.delete(url, headers=_headers(token)).status_code in (404, 405)


def test_one_users_job_requirements_are_invisible_to_another(client: TestClient) -> None:
    _seed_taxonomy()
    owner = _token(client)
    other = _token(client)
    _create_job(client, owner, "Python is required.")
    other_job = _create_job(client, other, "Docker is required.")

    assert set(_levels(client, other, other_job)) == {"Docker"}


# --- the data boundary -------------------------------------------------


def test_saving_a_job_creates_no_candidate_skills_or_evidence(client: TestClient) -> None:
    """THE EXPLICIT BOUNDARY TEST FROM THE BRIEF.

    A job requirement is a fact about a POSTING. Connecting it to a
    person is Prompt 4.3's business — asserted rather than assumed,
    because it would be very easy to add "just a little" candidate
    matching here and quietly move the line.
    """
    _seed_taxonomy()
    token = _token(client)

    job_id = _create_job(client, token, "Python, Docker, PostgreSQL and FastAPI are required.")

    # Requirements WERE created...
    assert len(_requirements(client, token, job_id)) == 4

    # ...and nothing on the candidate side moved.
    async def _counts(session: AsyncSession) -> tuple[int, int]:
        return (
            (await session.scalar(select(func.count()).select_from(CandidateSkill))) or 0,
            (await session.scalar(select(func.count()).select_from(SkillEvidence))) or 0,
        )

    assert _run(_counts) == (0, 0)


def test_editing_a_description_creates_no_candidate_side_rows(client: TestClient) -> None:
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Python is required.")

    _patch(client, token, job_id, description="Docker and Redis are required.")

    async def _counts(session: AsyncSession) -> tuple[int, int]:
        return (
            (await session.scalar(select(func.count()).select_from(CandidateSkill))) or 0,
            (await session.scalar(select(func.count()).select_from(SkillEvidence))) or 0,
        )

    assert _run(_counts) == (0, 0)


def test_deleting_a_job_cascades_to_its_requirements(client: TestClient) -> None:
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Python is required.")
    assert len(_rows(job_id)) == 1

    assert client.delete(f"{_BASE}/{job_id}", headers=_headers(token)).status_code == 204

    assert _rows(job_id) == []


def test_extraction_does_not_break_a_job_with_an_unseeded_taxonomy(
    client: TestClient,
) -> None:
    """No taxonomy seeded: saving must still succeed, with no
    requirements — extraction is part of the save, so a failure here
    would take the whole job with it."""
    token = _token(client)

    job_id = _create_job(client, token, "Python and Docker are required.")

    assert _requirements(client, token, job_id) == []


def test_the_real_world_capability_case_end_to_end(client: TestClient) -> None:
    """The exact browser string that exposed the 4.2 gap.

    "Should be able to write queries" states an expected capability, so
    SQL must be REQUIRED rather than merely mentioned — under-classifying
    it distorts the 4.3 match score downstream.
    """
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(client, token, "Databases (SQL) Should be able to write queries")

    assert _levels(client, token, job_id) == {"SQL": "required"}


def test_a_mixed_posting_keeps_every_level_distinct(client: TestClient) -> None:
    """Regression over all four outcomes at once: capability phrasing
    must not bleed into the neighbouring clauses, and negation must
    still win."""
    _seed_taxonomy()
    token = _token(client)
    job_id = _create_job(
        client,
        token,
        "Should be able to write SQL. Docker is a plus. We also use Redis. Java is not required.",
    )

    assert _levels(client, token, job_id) == {
        "SQL": "required",
        "Docker": "preferred",
        "Redis": "mentioned",
        "Java": "mentioned",
    }
