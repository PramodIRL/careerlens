"""User-controlled saved-job priority (Prompt 6.3).

The load-bearing tests here:

  * the order the user sets is the order they get back
  * a PARTIAL permutation is refused rather than interpreted — the
    friendly reading silently reshuffles priorities nobody touched
  * another user's job id is refused the same way, and the message does
    not reveal which kind of refusal it was
  * ordering is the only thing this changes: no score, no requirement,
    no evidence
"""

import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.rate_limit import _request_log
from tests.conftest import isolated_schema_override

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


def _new_user(client: TestClient) -> str:
    email = f"{uuid.uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return str(login.json()["access_token"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_job(client: TestClient, token: str, company: str) -> str:
    response = client.post(
        _BASE,
        headers=_headers(token),
        json={"company": company, "title": "Engineer", "description": "Python is required."},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _companies(client: TestClient, token: str) -> list[str]:
    response = client.get(_BASE, headers=_headers(token))
    assert response.status_code == 200, response.text
    return [job["company"] for job in response.json()]


def _reorder(client: TestClient, token: str, job_ids: list[str]) -> Any:
    return client.put(f"{_BASE}/order", headers=_headers(token), json={"job_ids": job_ids})


def test_a_new_job_lands_at_the_top(client: TestClient) -> None:
    """Newest-first was the behaviour before this prompt and stays the
    default; what changed is that it is now reorderable."""
    token = _new_user(client)
    _create_job(client, token, "First")
    _create_job(client, token, "Second")

    assert _companies(client, token) == ["Second", "First"]


def test_the_user_order_is_what_comes_back(client: TestClient) -> None:
    token = _new_user(client)
    first = _create_job(client, token, "First")
    second = _create_job(client, token, "Second")
    third = _create_job(client, token, "Third")

    response = _reorder(client, token, [first, third, second])

    assert response.status_code == 200, response.text
    assert [job["company"] for job in response.json()] == ["First", "Third", "Second"]
    assert _companies(client, token) == ["First", "Third", "Second"]


def test_reordering_is_stable_across_requests(client: TestClient) -> None:
    token = _new_user(client)
    ids = [_create_job(client, token, f"Company {index}") for index in range(4)]

    _reorder(client, token, list(reversed(ids)))

    assert _companies(client, token) == [f"Company {index}" for index in range(3, -1, -1)]
    assert _companies(client, token) == [f"Company {index}" for index in range(3, -1, -1)]


def test_a_partial_permutation_is_refused(client: TestClient) -> None:
    """Ambiguous about where the omitted jobs go, and the friendly
    reading drops somebody's job to the bottom of their own list."""
    token = _new_user(client)
    first = _create_job(client, token, "First")
    _create_job(client, token, "Second")

    response = _reorder(client, token, [first])

    assert response.status_code == 422
    assert _companies(client, token) == ["Second", "First"]


def test_an_unknown_id_is_refused(client: TestClient) -> None:
    token = _new_user(client)
    first = _create_job(client, token, "First")

    response = _reorder(client, token, [first, str(uuid.uuid4())])

    assert response.status_code == 422


def test_another_users_job_is_refused_without_confirming_it_exists(client: TestClient) -> None:
    """The same 422 and the same message as an id that does not exist —
    distinguishing them would confirm whether a job is on somebody
    else's account."""
    owner_token = _new_user(client)
    owned = _create_job(client, owner_token, "Owned")

    stranger_token = _new_user(client)
    mine = _create_job(client, stranger_token, "Mine")

    foreign = _reorder(client, stranger_token, [mine, owned])
    imaginary = _reorder(client, stranger_token, [mine, str(uuid.uuid4())])

    assert foreign.status_code == imaginary.status_code == 422
    assert foreign.json()["detail"] == imaginary.json()["detail"]
    # And the owner's list is untouched.
    assert _companies(client, owner_token) == ["Owned"]


def test_duplicates_are_refused_by_the_schema(client: TestClient) -> None:
    token = _new_user(client)
    first = _create_job(client, token, "First")
    _create_job(client, token, "Second")

    assert _reorder(client, token, [first, first]).status_code == 422


def test_an_empty_list_is_refused(client: TestClient) -> None:
    token = _new_user(client)
    _create_job(client, token, "First")

    assert _reorder(client, token, []).status_code == 422


def test_a_user_id_in_the_body_is_refused(client: TestClient) -> None:
    """Structural ownership: naming an owner is a 422, not a field that
    gets silently dropped."""
    token = _new_user(client)
    first = _create_job(client, token, "First")

    response = client.put(
        f"{_BASE}/order",
        headers=_headers(token),
        json={"job_ids": [first], "user_id": str(uuid.uuid4())},
    )

    assert response.status_code == 422


def test_reordering_changes_nothing_but_order(client: TestClient) -> None:
    token = _new_user(client)
    first = _create_job(client, token, "First")
    second = _create_job(client, token, "Second")

    before = client.get(f"{_BASE}/{first}/match", headers=_headers(token)).json()
    _reorder(client, token, [first, second])
    after = client.get(f"{_BASE}/{first}/match", headers=_headers(token)).json()

    assert after == before


def test_reordering_requires_authentication(client: TestClient) -> None:
    response = client.put(f"{_BASE}/order", json={"job_ids": [str(uuid.uuid4())]})
    assert response.status_code == 401
