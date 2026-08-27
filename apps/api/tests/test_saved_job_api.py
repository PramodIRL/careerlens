"""API tests for saved job descriptions (Prompt 4.1).

The tests that matter most here are the ones that would catch the
feature quietly becoming something it must not be:

  * ownership is STRUCTURAL — another user's job is a 403 on read,
    update and delete, and a client cannot name an owner at all
  * `source_url` is stored and NEVER fetched; dangerous schemes are
    rejected outright
  * the description is stored VERBATIM, with internal formatting intact,
    because Prompt 4.2 will quote spans of it
  * nothing here writes candidate skills or skill evidence
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.main import app
from app.models.candidate_skill import CandidateSkill
from app.models.saved_job import SavedJob
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.rate_limit import _request_log
from app.schemas.saved_job import MAX_DESCRIPTION_LENGTH
from app.settings import get_settings
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_BASE = "/api/v1/saved-jobs"

_VALID = {
    "company": "Fictional Widgets Ltd",
    "title": "Junior Backend Engineer",
    "description": "Build and test internal web services.",
    "location": "Springfield, Fictionia",
    "employment_type": "full_time",
    "source_url": "https://example.com/jobs/42",
}


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


def _new_user(client: TestClient) -> tuple[str, str]:
    email = f"{uuid.uuid4()}@example.com"
    register = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    assert register.status_code == 201
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200
    return login.json()["access_token"], register.json()["id"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create(client: TestClient, token: str, **overrides: Any) -> Response:
    return client.post(_BASE, headers=_headers(token), json={**_VALID, **overrides})


def _created_id(client: TestClient, token: str, **overrides: Any) -> str:
    response = _create(client, token, **overrides)
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --- authentication ---------------------------------------------------


def test_every_route_requires_authentication(client: TestClient) -> None:
    job_id = str(uuid.uuid4())
    assert client.get(_BASE).status_code == 401
    assert client.post(_BASE, json=_VALID).status_code == 401
    assert client.get(f"{_BASE}/{job_id}").status_code == 401
    assert client.patch(f"{_BASE}/{job_id}", json={"title": "x"}).status_code == 401
    assert client.delete(f"{_BASE}/{job_id}").status_code == 401


# --- create -----------------------------------------------------------


def test_create_returns_201_and_the_stored_job(client: TestClient) -> None:
    token, _ = _new_user(client)

    response = _create(client, token)

    assert response.status_code == 201
    body = response.json()
    assert body["company"] == _VALID["company"]
    assert body["title"] == _VALID["title"]
    assert body["description"] == _VALID["description"]
    assert body["location"] == _VALID["location"]
    assert body["employment_type"] == "full_time"
    assert body["source_url"] == _VALID["source_url"]
    assert body["created_at"] and body["updated_at"]


def test_response_exposes_exactly_the_expected_fields(client: TestClient) -> None:
    """No user_id, no extracted skills, no match score — none of that
    exists in Prompt 4.1, so none of it can leak from here.

    `position` joined the response in Prompt 6.3: the candidate's own
    ordering of their list, which is theirs to read and to set.
    """
    token, _ = _new_user(client)

    body = _create(client, token).json()

    assert set(body.keys()) == {
        "id",
        "company",
        "title",
        "location",
        "employment_type",
        "source_url",
        "description",
        "position",
        "created_at",
        "updated_at",
    }


def test_optional_fields_may_be_omitted(client: TestClient) -> None:
    token, _ = _new_user(client)

    response = client.post(
        _BASE,
        headers=_headers(token),
        json={"company": "Acme", "title": "Engineer", "description": "Work here."},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["location"] is None
    assert body["employment_type"] is None
    assert body["source_url"] is None


def test_whitespace_is_trimmed_but_internal_formatting_is_verbatim(
    client: TestClient,
) -> None:
    """Prompt 4.2 will quote spans of this text as evidence, so an
    excerpt must be a real slice of what the user saved. Only the outer
    whitespace goes."""
    description = "Responsibilities:\n\n  - Build APIs\n  - Write tests\n"
    token, _ = _new_user(client)

    body = _create(client, token, company="  Acme  ", description=description).json()

    assert body["company"] == "Acme"
    assert body["description"] == description.strip()
    assert "\n\n  - Build APIs" in body["description"]


@pytest.mark.parametrize("field", ["company", "title", "description"])
def test_required_fields_are_required(client: TestClient, field: str) -> None:
    token, _ = _new_user(client)
    payload = {k: v for k, v in _VALID.items() if k != field}

    assert client.post(_BASE, headers=_headers(token), json=payload).status_code == 422


@pytest.mark.parametrize("field", ["company", "title", "description"])
def test_required_fields_reject_blank(client: TestClient, field: str) -> None:
    token, _ = _new_user(client)

    assert _create(client, token, **{field: "   "}).status_code == 422


@pytest.mark.parametrize("field", ["company", "title", "location"])
def test_text_fields_reject_over_length(client: TestClient, field: str) -> None:
    token, _ = _new_user(client)

    assert _create(client, token, **{field: "x" * 201}).status_code == 422


def test_description_accepts_the_maximum_length(client: TestClient) -> None:
    token, _ = _new_user(client)

    response = _create(client, token, description="x" * MAX_DESCRIPTION_LENGTH)

    assert response.status_code == 201
    assert len(response.json()["description"]) == MAX_DESCRIPTION_LENGTH


def test_description_rejects_one_character_over(client: TestClient) -> None:
    token, _ = _new_user(client)

    assert _create(client, token, description="x" * (MAX_DESCRIPTION_LENGTH + 1)).status_code == 422


def test_an_unknown_employment_type_is_rejected(client: TestClient) -> None:
    token, _ = _new_user(client)

    assert _create(client, token, employment_type="freelance-ish").status_code == 422


# --- source_url: validated, stored, NEVER fetched ---------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://example.com/posting",
        "data:text/html,<script>alert(1)</script>",
        "not-a-url",
        "//example.com/jobs/1",
    ],
)
def test_dangerous_or_malformed_source_urls_are_rejected(client: TestClient, url: str) -> None:
    """Restricting the scheme protects the USER's browser when they click
    the stored link. It is not what makes the value safe server-side —
    nothing in CareerLens ever fetches it."""
    token, _ = _new_user(client)

    assert _create(client, token, source_url=url).status_code == 422


@pytest.mark.parametrize("url", ["https://example.com/jobs/1", "http://example.com/jobs/1"])
def test_http_and_https_source_urls_are_accepted(client: TestClient, url: str) -> None:
    token, _ = _new_user(client)

    response = _create(client, token, source_url=url)

    assert response.status_code == 201
    # Stored exactly as typed — not normalized.
    assert response.json()["source_url"] == url


def test_an_empty_source_url_becomes_null(client: TestClient) -> None:
    """An empty box in a form means "no URL", not a validation error."""
    token, _ = _new_user(client)

    response = _create(client, token, source_url="")

    assert response.status_code == 201
    assert response.json()["source_url"] is None


def test_an_over_length_source_url_is_rejected(client: TestClient) -> None:
    token, _ = _new_user(client)
    long_url = "https://example.com/" + ("x" * 2100)

    assert _create(client, token, source_url=long_url).status_code == 422


# --- ownership is structural -----------------------------------------


def test_a_client_may_not_name_an_owner(client: TestClient) -> None:
    """extra="forbid" — a request that tries to set user_id is REJECTED,
    not silently ignored, so the attempt is visible rather than quiet."""
    token, _ = _new_user(client)
    _, victim_id = _new_user(client)

    response = client.post(_BASE, headers=_headers(token), json={**_VALID, "user_id": victim_id})

    assert response.status_code == 422


def test_unknown_fields_are_rejected(client: TestClient) -> None:
    token, _ = _new_user(client)

    assert _create(client, token, match_score=0.99).status_code == 422


def test_the_list_only_contains_the_callers_jobs(client: TestClient) -> None:
    token_a, _ = _new_user(client)
    token_b, _ = _new_user(client)
    _created_id(client, token_a, company="Mine")
    _created_id(client, token_b, company="Theirs")

    mine = client.get(_BASE, headers=_headers(token_a)).json()
    theirs = client.get(_BASE, headers=_headers(token_b)).json()

    assert [row["company"] for row in mine] == ["Mine"]
    assert [row["company"] for row in theirs] == ["Theirs"]


def test_another_user_cannot_read_a_job(client: TestClient) -> None:
    token_a, _ = _new_user(client)
    token_b, _ = _new_user(client)
    job_id = _created_id(client, token_a)

    assert client.get(f"{_BASE}/{job_id}", headers=_headers(token_b)).status_code == 403


def test_another_user_cannot_update_a_job(client: TestClient) -> None:
    token_a, _ = _new_user(client)
    token_b, _ = _new_user(client)
    job_id = _created_id(client, token_a)

    response = client.patch(
        f"{_BASE}/{job_id}", headers=_headers(token_b), json={"title": "Hijacked"}
    )

    assert response.status_code == 403
    # And the row is untouched.
    assert (
        client.get(f"{_BASE}/{job_id}", headers=_headers(token_a)).json()["title"]
        == (_VALID["title"])
    )


def test_another_user_cannot_delete_a_job(client: TestClient) -> None:
    token_a, _ = _new_user(client)
    token_b, _ = _new_user(client)
    job_id = _created_id(client, token_a)

    assert client.delete(f"{_BASE}/{job_id}", headers=_headers(token_b)).status_code == 403
    assert client.get(f"{_BASE}/{job_id}", headers=_headers(token_a)).status_code == 200


# --- read / list ------------------------------------------------------


def test_the_list_is_empty_rather_than_404_for_a_new_user(client: TestClient) -> None:
    token, _ = _new_user(client)

    response = client.get(_BASE, headers=_headers(token))

    assert response.status_code == 200
    assert response.json() == []


def test_the_list_is_newest_first(client: TestClient) -> None:
    token, _ = _new_user(client)
    _created_id(client, token, company="First")
    _created_id(client, token, company="Second")
    _created_id(client, token, company="Third")

    companies = [row["company"] for row in client.get(_BASE, headers=_headers(token)).json()]

    assert companies == ["Third", "Second", "First"]


def test_reading_a_single_job(client: TestClient) -> None:
    token, _ = _new_user(client)
    job_id = _created_id(client, token)

    response = client.get(f"{_BASE}/{job_id}", headers=_headers(token))

    assert response.status_code == 200
    assert response.json()["id"] == job_id


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_a_nonexistent_job_is_404(client: TestClient, method: str) -> None:
    token, _ = _new_user(client)
    url = f"{_BASE}/{uuid.uuid4()}"
    call = getattr(client, method)

    response = (
        call(url, headers=_headers(token), json={"title": "x"})
        if method == "patch"
        else (call(url, headers=_headers(token)))
    )

    assert response.status_code == 404


def test_a_malformed_id_is_422(client: TestClient) -> None:
    token, _ = _new_user(client)

    assert client.get(f"{_BASE}/not-a-uuid", headers=_headers(token)).status_code == 422


# --- update -----------------------------------------------------------


def test_patch_updates_only_what_was_sent(client: TestClient) -> None:
    token, _ = _new_user(client)
    job_id = _created_id(client, token)

    response = client.patch(
        f"{_BASE}/{job_id}", headers=_headers(token), json={"title": "Senior Engineer"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Senior Engineer"
    # Everything omitted is unchanged.
    assert body["company"] == _VALID["company"]
    assert body["description"] == _VALID["description"]
    assert body["location"] == _VALID["location"]


def test_patch_can_clear_an_optional_field(client: TestClient) -> None:
    token, _ = _new_user(client)
    job_id = _created_id(client, token)

    response = client.patch(
        f"{_BASE}/{job_id}",
        headers=_headers(token),
        json={"location": None, "source_url": None, "employment_type": None},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["location"] is None
    assert body["source_url"] is None
    assert body["employment_type"] is None


@pytest.mark.parametrize("field", ["company", "title", "description"])
def test_patch_cannot_null_a_required_field(client: TestClient, field: str) -> None:
    """There is no request shape that can blank a row's identity."""
    token, _ = _new_user(client)
    job_id = _created_id(client, token)

    response = client.patch(f"{_BASE}/{job_id}", headers=_headers(token), json={field: None})

    assert response.status_code == 422


def test_patch_validates_the_source_url(client: TestClient) -> None:
    token, _ = _new_user(client)
    job_id = _created_id(client, token)

    response = client.patch(
        f"{_BASE}/{job_id}", headers=_headers(token), json={"source_url": "javascript:alert(1)"}
    )

    assert response.status_code == 422


def test_patch_rejects_unknown_fields(client: TestClient) -> None:
    token, _ = _new_user(client)
    job_id = _created_id(client, token)

    response = client.patch(f"{_BASE}/{job_id}", headers=_headers(token), json={"user_id": "x"})

    assert response.status_code == 422


def test_patch_advances_updated_at(client: TestClient) -> None:
    token, _ = _new_user(client)
    created = _create(client, token).json()

    updated = client.patch(
        f"{_BASE}/{created['id']}", headers=_headers(token), json={"title": "Changed"}
    ).json()

    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]


# --- delete -----------------------------------------------------------


def test_delete_returns_204_and_the_job_is_gone(client: TestClient) -> None:
    token, _ = _new_user(client)
    job_id = _created_id(client, token)

    assert client.delete(f"{_BASE}/{job_id}", headers=_headers(token)).status_code == 204
    assert client.get(f"{_BASE}/{job_id}", headers=_headers(token)).status_code == 404
    assert client.get(_BASE, headers=_headers(token)).json() == []


def test_deleting_one_job_leaves_the_others(client: TestClient) -> None:
    token, _ = _new_user(client)
    keep = _created_id(client, token, company="Keep")
    drop = _created_id(client, token, company="Drop")

    assert client.delete(f"{_BASE}/{drop}", headers=_headers(token)).status_code == 204

    remaining = client.get(_BASE, headers=_headers(token)).json()
    assert [row["id"] for row in remaining] == [keep]


# --- boundaries with the rest of the system ---------------------------


def test_saving_a_job_writes_no_skills_or_evidence(client: TestClient) -> None:
    """Prompt 4.1 stores a posting inert. Extracting skills from it is
    4.2 and scoring it is 4.3 — asserted rather than assumed, because it
    would be easy to add "just a little" matching here and quietly move
    the boundary."""
    token, _ = _new_user(client)
    _created_id(
        client,
        token,
        description="We need Python, Django, Docker, PostgreSQL and pytest experience.",
    )

    async def _counts(session: AsyncSession) -> tuple[int, int]:
        return (
            (await session.scalar(select(func.count()).select_from(CandidateSkill))) or 0,
            (await session.scalar(select(func.count()).select_from(SkillEvidence))) or 0,
        )

    assert _run(_counts) == (0, 0)


def test_deleting_a_user_cascades_to_their_saved_jobs(client: TestClient) -> None:
    token, user_id = _new_user(client)
    _created_id(client, token)

    async def _delete_user_and_count(session: AsyncSession) -> int:
        user = await session.get(User, uuid.UUID(user_id))
        assert user is not None
        await session.delete(user)
        await session.commit()
        return (
            await session.scalar(
                select(func.count())
                .select_from(SavedJob)
                .where(SavedJob.user_id == uuid.UUID(user_id))
            )
        ) or 0

    assert _run(_delete_user_and_count) == 0
