"""Candidate qualification facts over HTTP (Prompt 5.1a).

The load-bearing tests here, which should not be softened:

  * unknown is `null` and stays `null` — nothing defaults to zero
  * a CGPA can be cleared, and clearing takes its scale with it
  * a scale is never accepted without a value to attach it to
  * ownership is structural: no user id is accepted anywhere
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
from app.rate_limit import _request_log
from app.settings import get_settings
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_URL = "/api/v1/qualifications"


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


def _new_user(client: TestClient) -> str:
    email = f"{uuid.uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return str(login.json()["access_token"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _patch(client: TestClient, token: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.patch(_URL, headers=_headers(token), json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _get(client: TestClient, token: str) -> dict[str, Any]:
    response = client.get(_URL, headers=_headers(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- the empty state ----------------------------------------------------


def test_a_new_candidate_has_every_fact_null(client: TestClient) -> None:
    """All-null rather than a 404: "I have not filled this in" is the
    normal starting state, not an error."""
    token = _new_user(client)
    payload = _get(client, token)
    for field in (
        "cgpa",
        "cgpa_scale",
        "class_10_percentage",
        "class_12_percentage",
        "highest_degree",
        "field_of_study",
        "graduation_year",
        "years_experience",
    ):
        assert payload[field] is None, field


def test_reading_an_empty_profile_writes_no_rows(client: TestClient) -> None:
    """A GET is a GET. Nothing is lazily created, so an untouched
    candidate leaves no trace in the table."""
    token = _new_user(client)
    _get(client, token)
    count = _run(lambda s: s.scalar(select(func.count()).select_from(CandidateQualification)))
    assert count == 0


# --- declaring facts ----------------------------------------------------


def test_declared_facts_round_trip(client: TestClient) -> None:
    token = _new_user(client)
    _patch(
        client,
        token,
        {
            "cgpa": "8.20",
            "cgpa_scale": "10.00",
            "class_10_percentage": "92.00",
            "class_12_percentage": "88.50",
            "highest_degree": "btech",
            "field_of_study": "computer_science",
            "graduation_year": 2026,
            "years_experience": "1.50",
        },
    )
    payload = _get(client, token)
    assert float(payload["cgpa"]) == 8.20
    assert float(payload["cgpa_scale"]) == 10.00
    assert float(payload["class_12_percentage"]) == 88.50
    assert payload["highest_degree"] == "btech"
    assert payload["field_of_study"] == "computer_science"
    # An integer to every reader, not "2026.00".
    assert payload["graduation_year"] == 2026


def test_an_omitted_field_is_left_unchanged(client: TestClient) -> None:
    """The whole point of a PATCH. Sending only the degree must not
    silently wipe a CGPA typed earlier."""
    token = _new_user(client)
    _patch(client, token, {"cgpa": "8.20", "cgpa_scale": "10.00"})
    _patch(client, token, {"highest_degree": "btech"})
    payload = _get(client, token)
    assert float(payload["cgpa"]) == 8.20
    assert payload["highest_degree"] == "btech"


def test_an_explicit_null_clears_a_fact(client: TestClient) -> None:
    """A candidate who mistyped must be able to remove the value
    entirely, not just overwrite it — a wrong number is what a job
    reports them as failing on."""
    token = _new_user(client)
    _patch(client, token, {"class_12_percentage": "88.00"})
    _patch(client, token, {"class_12_percentage": None})
    assert _get(client, token)["class_12_percentage"] is None


def test_clearing_a_cgpa_takes_its_scale_with_it(client: TestClient) -> None:
    """A scale on its own describes nothing, so it must not survive the
    value it belonged to."""
    token = _new_user(client)
    _patch(client, token, {"cgpa": "8.20", "cgpa_scale": "10.00"})
    _patch(client, token, {"cgpa": None})
    payload = _get(client, token)
    assert payload["cgpa"] is None
    assert payload["cgpa_scale"] is None


def test_clearing_a_fact_deletes_its_row(client: TestClient) -> None:
    """Unknown has exactly ONE representation — the absence of a row —
    so the resolver can decide it with a dictionary lookup."""
    token = _new_user(client)
    _patch(client, token, {"years_experience": "3.00"})
    _patch(client, token, {"years_experience": None})
    count = _run(lambda s: s.scalar(select(func.count()).select_from(CandidateQualification)))
    assert count == 0


def test_a_cgpa_scale_can_be_added_to_an_existing_value(client: TestClient) -> None:
    token = _new_user(client)
    _patch(client, token, {"cgpa": "8.20"})
    assert _get(client, token)["cgpa_scale"] is None
    _patch(client, token, {"cgpa_scale": "10.00"})
    payload = _get(client, token)
    assert float(payload["cgpa"]) == 8.20
    assert float(payload["cgpa_scale"]) == 10.00


# --- validation ---------------------------------------------------------


def test_a_cgpa_with_no_scale_is_accepted(client: TestClient) -> None:
    """Deliberately allowed. Typing a CGPA without its scale is a normal
    thing to do; the honest response is to store it and report
    UNDETERMINED against a threshold — not to refuse the input, and
    certainly not to pick a scale on the candidate's behalf."""
    token = _new_user(client)
    payload = _patch(client, token, {"cgpa": "8.20"})
    assert float(payload["cgpa"]) == 8.20
    assert payload["cgpa_scale"] is None


def test_a_scale_without_any_cgpa_is_rejected(client: TestClient) -> None:
    """Silently dropping it would show the candidate a form that forgets
    what they typed."""
    token = _new_user(client)
    response = client.patch(_URL, headers=_headers(token), json={"cgpa_scale": "10.00"})
    assert response.status_code == 422, response.text


def test_a_cgpa_above_its_own_scale_is_rejected(client: TestClient) -> None:
    token = _new_user(client)
    response = client.patch(
        _URL, headers=_headers(token), json={"cgpa": "11.00", "cgpa_scale": "10.00"}
    )
    assert response.status_code == 422, response.text


def test_a_rejected_request_changes_nothing(client: TestClient) -> None:
    """Validated before anything is mutated."""
    token = _new_user(client)
    _patch(client, token, {"cgpa": "8.20", "cgpa_scale": "10.00"})
    client.patch(_URL, headers=_headers(token), json={"cgpa": "50.00", "cgpa_scale": "10.00"})
    payload = _get(client, token)
    assert float(payload["cgpa"]) == 8.20
    assert float(payload["cgpa_scale"]) == 10.00


@pytest.mark.parametrize(
    "body",
    [
        {"class_12_percentage": "150.00"},
        {"graduation_year": 1800},
        {"highest_degree": "not-a-degree"},
        {"field_of_study": "underwater-basket-weaving"},
        {"years_experience": "-1.00"},
    ],
)
def test_out_of_range_and_unknown_vocabulary_are_rejected(
    client: TestClient, body: dict[str, Any]
) -> None:
    token = _new_user(client)
    assert client.patch(_URL, headers=_headers(token), json=body).status_code == 422


def test_an_unknown_field_is_rejected_not_ignored(client: TestClient) -> None:
    """`extra="forbid"`. The difference between "rejected" and
    "ignored" matters: "ignored" is one refactor away from "honoured"."""
    token = _new_user(client)
    response = client.patch(_URL, headers=_headers(token), json={"cgpaa": "8.2"})
    assert response.status_code == 422


# --- ownership ----------------------------------------------------------


def test_qualifications_require_authentication(client: TestClient) -> None:
    assert client.get(_URL).status_code == 401
    assert client.patch(_URL, json={"cgpa": "8.20"}).status_code == 401


def test_one_candidate_never_sees_anothers_facts(client: TestClient) -> None:
    """Academic records. There is no id in the path, so there is nothing
    to enumerate — a second candidate simply sees their own empty
    profile."""
    first = _new_user(client)
    _patch(client, first, {"cgpa": "8.20", "cgpa_scale": "10.00", "highest_degree": "btech"})

    second = _new_user(client)
    payload = _get(client, second)
    assert payload["cgpa"] is None
    assert payload["highest_degree"] is None


def test_a_user_id_in_the_body_is_rejected(client: TestClient) -> None:
    """No spoofing path: `user_id` comes only from the JWT."""
    token = _new_user(client)
    victim = str(uuid.uuid4())
    response = client.patch(_URL, headers=_headers(token), json={"cgpa": "8.20", "user_id": victim})
    assert response.status_code == 422, response.text


def test_a_user_id_query_parameter_changes_nothing(client: TestClient) -> None:
    first = _new_user(client)
    _patch(client, first, {"cgpa": "8.20", "cgpa_scale": "10.00"})
    second = _new_user(client)
    response = client.get(f"{_URL}?user_id=whoever", headers=_headers(second))
    assert response.status_code == 200
    assert response.json()["cgpa"] is None
