"""Job eligibility requirements and the derived verdict (Prompt 5.1a).

The load-bearing tests here, which should not be softened:

  * `/match` and `/gaps` are byte-identical to what they were before
    this domain existed — eligibility never touches `skill_match_v1` or
    `skill_gap_v1`
  * a candidate who has declared nothing is UNKNOWN everywhere, never
    NOT_ELIGIBLE
  * an unstated CGPA scale never becomes a verdict
  * requirements reconcile as desired state, and a re-run writes nothing
  * every requirement quotes a verbatim slice of the description
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
from app.models.candidate_qualification import CandidateQualification
from app.models.job_eligibility import JobEligibilityRequirement, JobEligibilityRequirementValue
from app.models.skill_evidence import SkillEvidence
from app.rate_limit import _request_log
from app.settings import get_settings
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_JOBS = "/api/v1/saved-jobs"
_QUALIFICATIONS = "/api/v1/qualifications"

# Every level and every parseable requirement type in one posting, so a
# single fixture exercises the whole domain.
_DESCRIPTION = (
    "Backend Engineer at Nowhere Systems (fictional).\n\n"
    "Minimum CGPA 7.5/10. At least 75% in Class 10 and Class 12. "
    "B.Tech in Computer Science or related field. "
    "2+ years of experience. Graduating in 2026.\n\n"
    "Python is required. Docker is preferred."
)


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


def _new_user(client: TestClient) -> str:
    email = f"{uuid.uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return str(login.json()["access_token"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_job(client: TestClient, token: str, description: str = _DESCRIPTION) -> str:
    response = client.post(
        _JOBS,
        headers=_headers(token),
        json={
            "company": "Nowhere Systems",
            "title": "Backend Engineer",
            "description": description,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _requirements(client: TestClient, token: str, job_id: str) -> list[dict[str, Any]]:
    response = client.get(f"{_JOBS}/{job_id}/eligibility-requirements", headers=_headers(token))
    assert response.status_code == 200, response.text
    return list(response.json())


def _eligibility(client: TestClient, token: str, job_id: str) -> dict[str, Any]:
    response = client.get(f"{_JOBS}/{job_id}/eligibility", headers=_headers(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def _declare(client: TestClient, token: str, body: dict[str, Any]) -> None:
    response = client.patch(_QUALIFICATIONS, headers=_headers(token), json=body)
    assert response.status_code == 200, response.text


def _states(payload: dict[str, Any]) -> dict[str, str]:
    return {row["requirement_type"]: row["state"] for row in payload["requirements"]}


# --- extraction and persistence -----------------------------------------


def test_saving_a_job_extracts_its_eligibility_bars(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)

    rows = {row["requirement_type"]: row for row in _requirements(client, token, job_id)}
    assert set(rows) == {
        "cgpa",
        "class_10_percentage",
        "class_12_percentage",
        "highest_degree",
        "field_of_study",
        "years_experience",
        "graduation_year",
    }
    assert float(rows["cgpa"]["numeric_value"]) == 7.5
    assert float(rows["cgpa"]["value_scale"]) == 10
    assert float(rows["class_10_percentage"]["numeric_value"]) == 75
    assert rows["highest_degree"]["accepted_values"] == ["btech"]
    assert rows["field_of_study"]["open_ended"] is True
    assert rows["graduation_year"]["comparator"] == "eq"


def test_every_requirement_quotes_the_description_verbatim(client: TestClient) -> None:
    """Evidence-First on the job side: a reader can find each excerpt on
    the page by eye. Nothing is generated prose."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)
    for row in _requirements(client, token, job_id):
        collapsed = " ".join(_DESCRIPTION.split())
        assert row["excerpt"] in collapsed, row["excerpt"]
        assert row["matched_term"]
        assert 0 < row["confidence"] <= 1
        assert row["extraction_method"] == "job_description_match"


def test_a_job_stating_no_bars_has_no_requirements(client: TestClient) -> None:
    """The common case. Most postings say nothing about CGPA, and that
    is not an error."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token, "Python is required. We use Docker.")
    assert _requirements(client, token, job_id) == []


def test_skills_and_eligibility_are_stored_in_separate_domains(client: TestClient) -> None:
    """The architectural boundary, asserted. A CGPA floor must never
    appear as a skill requirement, and a skill must never appear as an
    eligibility bar."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)

    skills = client.get(f"{_JOBS}/{job_id}/requirements", headers=_headers(token)).json()
    skill_names = {row["skill_name"].casefold() for row in skills}
    assert "python" in skill_names
    for forbidden in ("cgpa", "class 10", "class 12", "b.tech"):
        assert forbidden not in skill_names

    eligibility_types = {row["requirement_type"] for row in _requirements(client, token, job_id)}
    assert "python" not in eligibility_types


# --- reconciliation and idempotency -------------------------------------


def test_editing_the_description_reconciles_the_bars(client: TestClient) -> None:
    """Desired state: the lowered CGPA is updated IN PLACE and the
    dropped requirements are removed, not left behind to keep reporting
    a candidate as ineligible."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)
    before = {row["requirement_type"]: row for row in _requirements(client, token, job_id)}

    response = client.patch(
        f"{_JOBS}/{job_id}",
        headers=_headers(token),
        json={"description": "Minimum CGPA 7.0/10. Python is required."},
    )
    assert response.status_code == 200, response.text

    after = {row["requirement_type"]: row for row in _requirements(client, token, job_id)}
    assert set(after) == {"cgpa"}
    assert float(after["cgpa"]["numeric_value"]) == 7.0
    # Updated in place — same row, same created_at.
    assert after["cgpa"]["id"] == before["cgpa"]["id"]
    assert after["cgpa"]["created_at"] == before["cgpa"]["created_at"]


def test_a_dropped_requirements_child_values_go_with_it(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)
    assert _run(
        lambda s: s.scalar(select(func.count()).select_from(JobEligibilityRequirementValue))
    )

    client.patch(
        f"{_JOBS}/{job_id}",
        headers=_headers(token),
        json={"description": "Minimum CGPA 7.0/10."},
    )
    remaining = _run(
        lambda s: s.scalar(select(func.count()).select_from(JobEligibilityRequirementValue))
    )
    assert remaining == 0


def test_re_sending_an_identical_description_writes_nothing(client: TestClient) -> None:
    """Idempotency, asserted on TIMESTAMPS rather than row counts: a
    count-only check passes while every run silently bumps updated_at."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)

    async def _snapshot(session: AsyncSession) -> set[tuple[Any, ...]]:
        rows = (await session.scalars(select(JobEligibilityRequirement))).all()
        return {
            (row.id, row.requirement_type, row.numeric_value, row.created_at, row.updated_at)
            for row in rows
        }

    before = _run(_snapshot)
    assert before

    for _ in range(3):
        response = client.patch(
            f"{_JOBS}/{job_id}", headers=_headers(token), json={"description": _DESCRIPTION}
        )
        assert response.status_code == 200

    assert _run(_snapshot) == before


def test_an_unrelated_edit_leaves_the_bars_untouched(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)
    before = _requirements(client, token, job_id)

    client.patch(f"{_JOBS}/{job_id}", headers=_headers(token), json={"company": "Elsewhere Ltd"})
    assert _requirements(client, token, job_id) == before


def test_deleting_a_job_removes_its_bars(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)
    assert client.delete(f"{_JOBS}/{job_id}", headers=_headers(token)).status_code == 204

    count = _run(lambda s: s.scalar(select(func.count()).select_from(JobEligibilityRequirement)))
    assert count == 0


# --- the derived verdict ------------------------------------------------


def test_a_candidate_who_has_declared_nothing_is_unknown_not_ineligible(
    client: TestClient,
) -> None:
    """THE RULE THIS DOMAIN RESTS ON. An empty profile is not a
    rejection."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)

    payload = _eligibility(client, token, job_id)
    assert payload["flag"] == "unknown"
    assert payload["has_requirements"] is True
    assert payload["totals"]["not_satisfied"] == 0
    assert payload["totals"]["unknown"] + payload["totals"]["undetermined"] == 7
    assert all(row["candidate_numeric"] is None for row in payload["requirements"])


def test_a_fully_qualified_candidate_is_eligible(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)
    _declare(
        client,
        token,
        {
            "cgpa": "8.20",
            "cgpa_scale": "10.00",
            "class_10_percentage": "92.00",
            "class_12_percentage": "88.00",
            "highest_degree": "btech",
            "field_of_study": "computer_science",
            "graduation_year": 2026,
            "years_experience": "3.00",
        },
    )
    payload = _eligibility(client, token, job_id)
    assert payload["flag"] == "eligible"
    assert payload["totals"]["satisfied"] == 7
    assert payload["formula_version"] == "eligibility_v1"


def test_one_failed_hard_bar_makes_the_candidate_ineligible(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)
    _declare(
        client,
        token,
        {
            "cgpa": "8.20",
            "cgpa_scale": "10.00",
            "class_10_percentage": "92.00",
            "class_12_percentage": "68.00",
            "highest_degree": "btech",
            "field_of_study": "computer_science",
            "graduation_year": 2026,
            "years_experience": "3.00",
        },
    )
    payload = _eligibility(client, token, job_id)
    assert payload["flag"] == "not_eligible"
    assert _states(payload)["class_12_percentage"] == "not_satisfied"
    assert _states(payload)["class_10_percentage"] == "satisfied"
    entry = next(
        row for row in payload["requirements"] if row["requirement_type"] == "class_12_percentage"
    )
    assert entry["reason"] == "below_threshold"
    # The candidate's own value comes back, so the UI can show the
    # comparison rather than a bare verdict.
    assert float(entry["candidate_numeric"]) == 68.0
    assert float(entry["requirement_numeric"]) == 75.0


def test_an_unstated_requirement_scale_defaults_to_ten(client: TestClient) -> None:
    """The posting wrote "minimum CGPA 7.5" with no scale. The product
    rule supplies 10, and the STORED row says so — so the response reads
    "7.5/10" and the assumption is visible rather than hidden."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token, "Minimum CGPA 7.5.")
    _declare(client, token, {"cgpa": "9.00", "cgpa_scale": "10.00"})

    stored = _requirements(client, token, job_id)
    assert float(stored[0]["value_scale"]) == 10.0

    payload = _eligibility(client, token, job_id)
    assert _states(payload)["cgpa"] == "satisfied"
    assert payload["flag"] == "eligible"


def test_an_unstated_candidate_scale_defaults_to_ten(client: TestClient) -> None:
    """A candidate who types 8.2 and leaves the scale box alone is read
    as 8.2/10, the same default the job side gets."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token, "Minimum CGPA 7.5/10.")
    _declare(client, token, {"cgpa": "8.20"})

    payload = _eligibility(client, token, job_id)
    assert _states(payload)["cgpa"] == "satisfied"


def test_an_explicit_candidate_scale_is_never_overridden(client: TestClient) -> None:
    """2.8/4 is 0.70 and would sail past a 7.5 bar if the stated 4 were
    ignored in favour of the default."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token, "Minimum CGPA 7.5/10.")
    _declare(client, token, {"cgpa": "2.80", "cgpa_scale": "4.00"})

    payload = _eligibility(client, token, job_id)
    assert _states(payload)["cgpa"] == "not_satisfied"
    assert payload["requirements"][0]["reason"] == "below_threshold"


def test_defaulting_the_scale_never_invents_a_cgpa(client: TestClient) -> None:
    """The default supplies a SCALE, never a VALUE. A candidate who has
    declared nothing is still UNKNOWN."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token, "Minimum CGPA 7.5.")

    payload = _eligibility(client, token, job_id)
    assert _states(payload)["cgpa"] == "unknown"
    assert payload["requirements"][0]["candidate_numeric"] is None
    assert payload["flag"] == "unknown"


def test_an_open_ended_field_mismatch_is_undetermined(client: TestClient) -> None:
    """ "or related field" — rejecting a mechanical engineer is not a
    call this product is entitled to make."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token, "B.Tech in Computer Science or related field.")
    _declare(client, token, {"highest_degree": "btech", "field_of_study": "mechanical"})

    payload = _eligibility(client, token, job_id)
    assert _states(payload)["field_of_study"] == "undetermined"
    assert _states(payload)["highest_degree"] == "satisfied"
    assert payload["flag"] == "unknown"


def test_a_closed_list_mismatch_is_a_real_failure(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token, "B.Tech required.")
    _declare(client, token, {"highest_degree": "bsc"})

    payload = _eligibility(client, token, job_id)
    assert _states(payload)["highest_degree"] == "not_satisfied"
    assert payload["flag"] == "not_eligible"


def test_a_job_with_no_bars_reports_unknown_not_eligible(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token, "Python is required.")

    payload = _eligibility(client, token, job_id)
    assert payload["has_requirements"] is False
    assert payload["flag"] == "unknown"
    assert payload["requirements"] == []


def test_declaring_a_fact_updates_every_jobs_verdict(client: TestClient) -> None:
    """One candidate edit changes the answer for every saved job at
    once — which is exactly why no verdict is stored."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token, "B.Tech required.")
    assert _eligibility(client, token, job_id)["flag"] == "unknown"

    _declare(client, token, {"highest_degree": "btech"})
    assert _eligibility(client, token, job_id)["flag"] == "eligible"

    _declare(client, token, {"highest_degree": None})
    assert _eligibility(client, token, job_id)["flag"] == "unknown"


def test_two_identical_requests_return_identical_json(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)
    _declare(client, token, {"cgpa": "8.20", "cgpa_scale": "10.00"})

    first = client.get(f"{_JOBS}/{job_id}/eligibility", headers=_headers(token))
    second = client.get(f"{_JOBS}/{job_id}/eligibility", headers=_headers(token))
    assert first.content == second.content


# --- read-only ----------------------------------------------------------


def test_reading_eligibility_invents_no_candidate_facts(client: TestClient) -> None:
    """The single most important guard in this file. Resolving a bar
    must never write a fact about a person — not a default, not a zero,
    not a placeholder row."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)

    for _ in range(3):
        _eligibility(client, token, job_id)

    count = _run(lambda s: s.scalar(select(func.count()).select_from(CandidateQualification)))
    assert count == 0


def test_reading_eligibility_mutates_nothing(client: TestClient) -> None:
    """Asserted on ids AND timestamps — a count-only check would miss an
    in-place UPDATE."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)
    _declare(client, token, {"cgpa": "8.20", "cgpa_scale": "10.00"})

    async def _snapshot(session: AsyncSession) -> set[tuple[Any, ...]]:
        rows: set[tuple[Any, ...]] = set()
        for req in (await session.scalars(select(JobEligibilityRequirement))).all():
            rows.add(("req", req.id, req.numeric_value, req.created_at, req.updated_at))
        for fact in (await session.scalars(select(CandidateQualification))).all():
            rows.add(("fact", fact.id, fact.value_numeric, fact.created_at, fact.updated_at))
        return rows

    before = _run(_snapshot)
    for _ in range(3):
        _eligibility(client, token, job_id)
        _requirements(client, token, job_id)
    assert _run(_snapshot) == before


def test_no_eligibility_verdict_table_exists(client: TestClient) -> None:
    """Verdicts are derived on read — a stored one would go stale the
    moment the candidate edited a single field."""

    async def _tables(session: AsyncSession) -> list[str]:
        from sqlalchemy import text

        rows = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name LIKE '%eligibility_result%'"
            ),
            {"schema": TEST_SCHEMA},
        )
        return [row[0] for row in rows]

    assert _run(_tables) == []


# --- the boundary with Phase 4 ------------------------------------------


def test_eligibility_does_not_change_the_skill_match_or_gaps(client: TestClient) -> None:
    """THE PROMISE THIS SLICE MAKES TO PHASE 4.

    A job whose description carries heavy eligibility bars must produce
    exactly the same `/match` and `/gaps` responses as the same posting
    with those sentences removed. Eligibility is never an input to
    `skill_match_v1` or `skill_gap_v1` — no extra field, no cap, no
    multiplier.
    """
    _seed_taxonomy()
    token = _new_user(client)
    skills_only = "Python is required. Docker is preferred."
    with_bars = (
        "Minimum CGPA 9.9/10. 99% in Class 10 and Class 12. "
        "B.Tech in Computer Science. 10+ years of experience. " + skills_only
    )

    plain_job = _create_job(client, token, skills_only)
    loaded_job = _create_job(client, token, with_bars)

    for path in ("match", "gaps"):
        plain = client.get(f"{_JOBS}/{plain_job}/{path}", headers=_headers(token)).json()
        loaded = client.get(f"{_JOBS}/{loaded_job}/{path}", headers=_headers(token)).json()
        assert plain == loaded, path

    # ...and the bars really were extracted, so this is not a vacuous
    # comparison of two identical postings.
    assert {row["requirement_type"] for row in _requirements(client, token, loaded_job)} == {
        "cgpa",
        "class_10_percentage",
        "class_12_percentage",
        "highest_degree",
        "field_of_study",
        "years_experience",
    }
    assert _eligibility(client, token, loaded_job)["has_requirements"] is True


def test_the_match_response_carries_no_eligibility_field(client: TestClient) -> None:
    """A shipped contract. Adding a key here would break the Phase 4
    acceptance test's exact-equality assertions, which is precisely why
    eligibility is a separate endpoint."""
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)

    match = client.get(f"{_JOBS}/{job_id}/match", headers=_headers(token)).json()
    gaps = client.get(f"{_JOBS}/{job_id}/gaps", headers=_headers(token)).json()
    for payload in (match, gaps):
        assert not any("eligib" in key for key in payload), sorted(payload)
    assert match["formula_version"] == "skill_match_v1"
    assert gaps["formula_version"] == "skill_gap_v1"


def test_extracting_eligibility_writes_no_skill_evidence(client: TestClient) -> None:
    """The job side never authors a claim about a person."""
    _seed_taxonomy()
    token = _new_user(client)
    _create_job(client, token)
    count = _run(lambda s: s.scalar(select(func.count()).select_from(SkillEvidence)))
    assert count == 0


# --- ownership ----------------------------------------------------------


def test_eligibility_endpoints_require_authentication(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    job_id = _create_job(client, token)
    for path in ("eligibility", "eligibility-requirements"):
        assert client.get(f"{_JOBS}/{job_id}/{path}").status_code == 401, path


def test_another_users_job_eligibility_is_forbidden(client: TestClient) -> None:
    _seed_taxonomy()
    owner = _new_user(client)
    job_id = _create_job(client, owner)

    intruder = _new_user(client)
    for path in ("eligibility", "eligibility-requirements"):
        response = client.get(f"{_JOBS}/{job_id}/{path}", headers=_headers(intruder))
        assert response.status_code == 403, path


def test_a_nonexistent_job_is_404(client: TestClient) -> None:
    _seed_taxonomy()
    token = _new_user(client)
    missing = uuid.uuid4()
    for path in ("eligibility", "eligibility-requirements"):
        response = client.get(f"{_JOBS}/{missing}/{path}", headers=_headers(token))
        assert response.status_code == 404, path


def test_eligibility_is_resolved_against_the_callers_own_facts(client: TestClient) -> None:
    """Two candidates, one posting, two different answers — and neither
    can see the other's academic record."""
    _seed_taxonomy()
    first = _new_user(client)
    first_job = _create_job(client, first, "B.Tech required.")
    _declare(client, first, {"highest_degree": "btech"})

    second = _new_user(client)
    second_job = _create_job(client, second, "B.Tech required.")
    _declare(client, second, {"highest_degree": "bsc"})

    assert _eligibility(client, first, first_job)["flag"] == "eligible"
    assert _eligibility(client, second, second_job)["flag"] == "not_eligible"
