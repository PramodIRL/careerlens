"""Tests for the candidate profile vertical slice (Prompt 1.3): reading
and updating full name, headline, city/country, experience level,
target roles, and declared target skills — validation, PATCH's
partial-update semantics, target-list replace/dedupe behavior,
canonical skill reuse across profiles, and ownership enforcement (a
user can never read or write another user's profile)."""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.db import get_db
from app.main import app
from app.rate_limit import _request_log
from tests.conftest import isolated_schema_override

_EMAIL_A = "alice@example.com"
_EMAIL_B = "bob@example.com"
_PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    # Every test registers/logs in at least one user via the shared
    # register/login/refresh rate-limit budget (app/rate_limit.py) —
    # without resetting between tests, earlier tests' requests would
    # count against later ones and eventually 429. Same fixture as
    # tests/test_auth.py.
    _request_log.clear()


def _register_and_login(
    client: TestClient, email: str, password: str = _PASSWORD
) -> tuple[str, str]:
    """Registers and logs in a fresh user. Returns (access_token, user_id)."""
    register = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert register.status_code == 201
    user_id = register.json()["id"]

    login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return login.json()["access_token"], user_id


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _get_profile(client: TestClient, token: str, user_id: str) -> Response:
    return client.get(f"/api/v1/profiles/{user_id}", headers=_auth_headers(token))


def _patch_profile(
    client: TestClient, token: str, user_id: str, payload: dict[str, object]
) -> Response:
    return client.patch(f"/api/v1/profiles/{user_id}", json=payload, headers=_auth_headers(token))


def test_get_profile_requires_authentication(client: TestClient) -> None:
    response = client.get(f"/api/v1/profiles/{uuid.uuid4()}")
    assert response.status_code == 401


def test_patch_profile_requires_authentication(client: TestClient) -> None:
    response = client.patch(f"/api/v1/profiles/{uuid.uuid4()}", json={"full_name": "x"})
    assert response.status_code == 401


def test_get_own_profile_is_lazily_created_with_empty_defaults(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)

    response = _get_profile(client, token, user_id)

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == user_id
    assert body["full_name"] is None
    assert body["headline"] is None
    assert body["city"] is None
    assert body["country"] is None
    assert body["experience_level"] is None
    assert body["target_roles"] == []
    assert body["target_skills"] == []


def test_patch_updates_scalar_fields_and_persists(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)
    payload = {
        "full_name": "Alice Example",
        "headline": "Backend engineer",
        "city": "Toronto",
        "country": "Canada",
        "experience_level": "junior",
    }

    patch_response = _patch_profile(client, token, user_id, payload)

    assert patch_response.status_code == 200
    body = patch_response.json()
    for key, value in payload.items():
        assert body[key] == value

    # Persisted, not just echoed back.
    get_response = _get_profile(client, token, user_id)
    assert get_response.json() == body


def test_patch_is_partial_other_fields_are_untouched(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)
    _patch_profile(client, token, user_id, {"full_name": "Alice Example", "city": "Toronto"})

    response = _patch_profile(client, token, user_id, {"headline": "Backend engineer"})

    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "Alice Example"
    assert body["city"] == "Toronto"
    assert body["headline"] == "Backend engineer"


def test_patch_can_explicitly_clear_a_scalar_field_with_null(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)
    _patch_profile(client, token, user_id, {"headline": "Backend engineer"})

    response = _patch_profile(client, token, user_id, {"headline": None})

    assert response.status_code == 200
    assert response.json()["headline"] is None


def test_patch_empty_body_is_a_no_op(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)
    _patch_profile(client, token, user_id, {"full_name": "Alice Example"})

    response = _patch_profile(client, token, user_id, {})

    assert response.status_code == 200
    assert response.json()["full_name"] == "Alice Example"


def test_patch_rejects_blank_full_name(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)

    response = _patch_profile(client, token, user_id, {"full_name": "   "})

    assert response.status_code == 422


def test_patch_rejects_oversized_headline(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)

    response = _patch_profile(client, token, user_id, {"headline": "x" * 201})

    assert response.status_code == 422


def test_patch_rejects_invalid_experience_level(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)

    response = _patch_profile(client, token, user_id, {"experience_level": "expert"})

    assert response.status_code == 422


def test_patch_rejects_too_many_target_roles(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)

    response = _patch_profile(
        client, token, user_id, {"target_roles": [f"role-{i}" for i in range(21)]}
    )

    assert response.status_code == 422


def test_patch_target_roles_replaces_the_whole_list(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)
    _patch_profile(client, token, user_id, {"target_roles": ["Backend Engineer", "SRE"]})

    response = _patch_profile(client, token, user_id, {"target_roles": ["Data Analyst"]})

    assert response.status_code == 200
    assert response.json()["target_roles"] == ["Data Analyst"]


def test_patch_target_roles_dedupes_case_insensitively_keeping_first_casing(
    client: TestClient,
) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)

    response = _patch_profile(
        client,
        token,
        user_id,
        {"target_roles": ["Backend Engineer", "backend engineer", " SRE "]},
    )

    assert response.status_code == 200
    assert response.json()["target_roles"] == ["Backend Engineer", "SRE"]


def test_patch_target_roles_empty_list_clears_all_roles(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)
    _patch_profile(client, token, user_id, {"target_roles": ["Backend Engineer"]})

    response = _patch_profile(client, token, user_id, {"target_roles": []})

    assert response.status_code == 200
    assert response.json()["target_roles"] == []


def test_patch_target_skills_replaces_the_whole_list(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)
    _patch_profile(client, token, user_id, {"target_skills": ["Python", "SQL"]})

    response = _patch_profile(client, token, user_id, {"target_skills": ["Go"]})

    assert response.status_code == 200
    assert response.json()["target_skills"] == ["Go"]


def test_patch_target_skills_dedupes_case_insensitively(client: TestClient) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)

    response = _patch_profile(
        client, token, user_id, {"target_skills": ["Python", "python", "PYTHON"]}
    )

    assert response.status_code == 200
    assert response.json()["target_skills"] == ["Python"]


def test_patch_target_skills_reuses_the_same_canonical_skill_row_across_profiles(
    client: TestClient,
) -> None:
    token_a, user_a = _register_and_login(client, _EMAIL_A)
    token_b, user_b = _register_and_login(client, _EMAIL_B)
    _patch_profile(client, token_a, user_a, {"target_skills": ["Python"]})

    response_b = _patch_profile(client, token_b, user_b, {"target_skills": ["python"]})

    assert response_b.status_code == 200
    # Both profiles render the same display name for the skill they
    # declared, in different casing — proving they resolved to one
    # shared `skills` row rather than two duplicate rows (see
    # app/models/skill.py and docs/decisions.md).
    assert response_b.json()["target_skills"] == ["Python"]


def test_cannot_get_another_users_profile(client: TestClient) -> None:
    token_a, user_a = _register_and_login(client, _EMAIL_A)
    token_b, _user_b = _register_and_login(client, _EMAIL_B)
    _patch_profile(client, token_a, user_a, {"full_name": "Alice Example"})

    response = _get_profile(client, token_b, user_a)

    assert response.status_code == 403
    assert "Alice" not in response.text


def test_cannot_patch_another_users_profile(client: TestClient) -> None:
    token_a, user_a = _register_and_login(client, _EMAIL_A)
    token_b, _user_b = _register_and_login(client, _EMAIL_B)
    _patch_profile(client, token_a, user_a, {"full_name": "Alice Example"})

    response = _patch_profile(client, token_b, user_a, {"full_name": "Hijacked"})

    assert response.status_code == 403
    # Alice's profile must be completely unaffected by Bob's attempt.
    still_alice = _get_profile(client, token_a, user_a)
    assert still_alice.json()["full_name"] == "Alice Example"
