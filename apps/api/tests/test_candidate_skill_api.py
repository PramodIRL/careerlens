"""API tests for the candidate-skill review endpoints (Prompt 2.4):
listing with evidence, confirm/reject, manual add, and — the part most
worth getting wrong quietly — ownership.

The manual-add contract is the other focus here: a candidate skill may
only ever resolve to a CURATED taxonomy skill or one of its aliases.
A skill a user coined through Prompt 1.3's free-text target skills is
deliberately NOT valid, and that backdoor has its own regression test
below.
"""

import asyncio
import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_db
from app.main import app
from app.models.candidate_skill import CandidateSkill
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.rate_limit import _request_log
from app.schemas.skill import CandidateSkillStatus, EvidenceSourceType, ExtractionMethod
from app.settings import get_settings
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_BASE = "/api/v1/candidate-skills"


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    _request_log.clear()


def _engine():
    return create_async_engine(get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS)


def _run(coro_factory) -> object:
    """Run one async DB helper on its own engine and loop — the same
    asyncio.run-per-call pattern tests/conftest.py uses, so these sync
    TestClient tests can still set up database state."""

    async def _inner() -> object:
        engine = _engine()
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                return await coro_factory(session)
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def _seed_taxonomy() -> None:
    _run(lambda session: seed_skill_taxonomy(session))


def _register_and_login(client: TestClient, email: str) -> tuple[str, str]:
    register = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    assert register.status_code == 201
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200
    return login.json()["access_token"], register.json()["id"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _add(client: TestClient, token: str, name: str) -> Response:
    return client.post(_BASE, headers=_headers(token), json={"name": name})


def _list(client: TestClient, token: str) -> Response:
    return client.get(_BASE, headers=_headers(token))


def _patch(client: TestClient, token: str, candidate_skill_id: str, status: str) -> Response:
    return client.patch(
        f"{_BASE}/{candidate_skill_id}", headers=_headers(token), json={"status": status}
    )


# --- authentication ---------------------------------------------------


def test_list_requires_authentication(client: TestClient) -> None:
    assert client.get(_BASE).status_code == 401


def test_add_requires_authentication(client: TestClient) -> None:
    assert client.post(_BASE, json={"name": "Python"}).status_code == 401


def test_patch_requires_authentication(client: TestClient) -> None:
    response = client.patch(f"{_BASE}/{uuid.uuid4()}", json={"status": "confirmed"})
    assert response.status_code == 401


# --- manual add: the curated-taxonomy contract ------------------------


def test_adding_a_canonical_skill_creates_a_confirmed_skill_with_manual_evidence(
    client: TestClient,
) -> None:
    _seed_taxonomy()
    token, user_id = _register_and_login(client, "alice@example.com")

    response = _add(client, token, "python")

    assert response.status_code == 201
    body = response.json()
    assert body["skill_name"] == "Python"  # canonical display name, not the input
    assert body["skill_category"] == "language"
    assert body["status"] == CandidateSkillStatus.CONFIRMED.value
    assert len(body["evidence"]) == 1
    evidence = body["evidence"][0]
    assert evidence["source_type"] == EvidenceSourceType.MANUAL.value
    assert evidence["extraction_method"] == ExtractionMethod.MANUAL_ENTRY.value
    assert evidence["confidence"] == 1.0
    assert evidence["excerpt"] is None
    # source_identifier comes from the authenticated user, never the body.
    assert evidence["source_identifier"] == user_id


def test_adding_by_alias_resolves_to_the_canonical_skill(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")

    response = _add(client, token, "PY")

    assert response.status_code == 201
    assert response.json()["skill_name"] == "Python"


@pytest.mark.parametrize("spelling", ["python", "Python", "  PYTHON  "])
def test_case_and_whitespace_variants_reach_the_same_skill(
    client: TestClient, spelling: str
) -> None:
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")

    assert _add(client, token, spelling).json()["skill_name"] == "Python"


def test_all_spellings_converge_on_one_candidate_skill(client: TestClient) -> None:
    """ "python", "PY" and "Python" are the same claim — adding all three
    must not produce three rows or three evidence records."""
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")

    first = _add(client, token, "python")
    second = _add(client, token, "PY")
    third = _add(client, token, "Python")

    assert first.status_code == 201
    assert second.status_code == 200  # updated, not created
    assert third.status_code == 200
    listed = _list(client, token).json()
    assert len(listed) == 1
    assert len(listed[0]["evidence"]) == 1


def test_adding_a_skill_outside_the_taxonomy_is_rejected(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")

    response = _add(client, token, "Rust")

    assert response.status_code == 422
    assert "taxonomy" in response.json()["detail"].lower()
    assert _list(client, token).json() == []


def test_a_rejected_add_writes_nothing_at_all(client: TestClient) -> None:
    """422 must leave the database untouched — in particular it must not
    coin a new `skills` row the way Prompt 1.3's target skills do."""
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")

    async def _counts(session) -> tuple[int, int]:
        skills = len((await session.scalars(select(Skill))).all())
        candidate_skills = len((await session.scalars(select(CandidateSkill))).all())
        return skills, candidate_skills

    before = _run(_counts)

    assert _add(client, token, "Rust").status_code == 422

    assert _run(_counts) == before


def test_a_user_coined_target_skill_is_not_a_valid_candidate_skill(client: TestClient) -> None:
    """The Prompt 1.3 backdoor, closed deliberately.

    Setting a free-text target skill DOES create a `skills` row — but
    with no category, marking it as un-curated. That row must not become
    addable as an evidence-backed candidate skill.
    """
    _seed_taxonomy()
    token, user_id = _register_and_login(client, "alice@example.com")
    patched = client.patch(
        f"/api/v1/profiles/{user_id}",
        headers=_headers(token),
        json={"target_skills": ["Rust"]},
    )
    assert patched.status_code == 200
    assert patched.json()["target_skills"] == ["Rust"]  # still a valid target skill

    response = _add(client, token, "Rust")

    assert response.status_code == 422
    assert _list(client, token).json() == []


@pytest.mark.parametrize("name", ["", "   "])
def test_a_blank_name_is_rejected(client: TestClient, name: str) -> None:
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")

    assert _add(client, token, name).status_code == 422


# --- confirm / reject -------------------------------------------------


def test_confirming_and_rejecting_a_candidate_skill(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")
    created = _add(client, token, "Python").json()

    rejected = _patch(client, token, created["id"], "rejected")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == CandidateSkillStatus.REJECTED.value

    confirmed = _patch(client, token, created["id"], "confirmed")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == CandidateSkillStatus.CONFIRMED.value


def test_re_adding_a_rejected_skill_confirms_it_again(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")
    created = _add(client, token, "Python").json()
    _patch(client, token, created["id"], "rejected")

    response = _add(client, token, "python")

    assert response.status_code == 200
    assert response.json()["status"] == CandidateSkillStatus.CONFIRMED.value
    assert response.json()["id"] == created["id"]  # same row, not a new one


def test_a_rejected_skill_still_appears_in_the_list(client: TestClient) -> None:
    """Rejection is a tombstone, not a delete — the row must remain
    visible so the UI can offer to restore it."""
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")
    created = _add(client, token, "Python").json()
    _patch(client, token, created["id"], "rejected")

    listed = _list(client, token).json()

    assert [row["status"] for row in listed] == [CandidateSkillStatus.REJECTED.value]


def test_an_invalid_status_is_rejected(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")
    created = _add(client, token, "Python").json()

    assert _patch(client, token, created["id"], "suggested").status_code == 422
    assert _patch(client, token, created["id"], "banana").status_code == 422


def test_patching_an_unknown_candidate_skill_returns_404(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")

    assert _patch(client, token, str(uuid.uuid4()), "confirmed").status_code == 404


# --- listing and ownership --------------------------------------------


def test_list_is_sorted_and_includes_evidence(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")
    _add(client, token, "Redis")
    _add(client, token, "Docker")

    listed = _list(client, token).json()

    assert [row["skill_name"] for row in listed] == ["Docker", "Redis"]
    assert all(len(row["evidence"]) == 1 for row in listed)


def test_list_returns_only_the_callers_own_skills(client: TestClient) -> None:
    _seed_taxonomy()
    alice_token, _ = _register_and_login(client, "alice@example.com")
    bob_token, _ = _register_and_login(client, "bob@example.com")
    _add(client, alice_token, "Python")
    _add(client, bob_token, "Docker")

    assert [r["skill_name"] for r in _list(client, alice_token).json()] == ["Python"]
    assert [r["skill_name"] for r in _list(client, bob_token).json()] == ["Docker"]


def test_cannot_patch_another_users_candidate_skill(client: TestClient) -> None:
    _seed_taxonomy()
    alice_token, _ = _register_and_login(client, "alice@example.com")
    bob_token, _ = _register_and_login(client, "bob@example.com")
    alices = _add(client, alice_token, "Python").json()

    response = _patch(client, bob_token, alices["id"], "rejected")

    assert response.status_code == 403
    # Alice's skill is completely unaffected.
    assert _list(client, alice_token).json()[0]["status"] == CandidateSkillStatus.CONFIRMED.value


def test_two_users_may_hold_the_same_skill_independently(client: TestClient) -> None:
    _seed_taxonomy()
    alice_token, _ = _register_and_login(client, "alice@example.com")
    bob_token, _ = _register_and_login(client, "bob@example.com")
    alices = _add(client, alice_token, "Python").json()
    _add(client, bob_token, "Python")

    _patch(client, alice_token, alices["id"], "rejected")

    assert _list(client, bob_token).json()[0]["status"] == CandidateSkillStatus.CONFIRMED.value


# --- provenance labels on the review endpoint (Prompt 3.4) ------------


def test_manual_evidence_exposes_a_null_source_label(client: TestClient) -> None:
    """`source_label` is ADDITIVE — the review endpoint's existing
    contract is unchanged, it simply now names what each piece of
    evidence cites. A manual assertion has no external source to name."""
    _seed_taxonomy()
    token, _ = _register_and_login(client, "alice@example.com")
    _add(client, token, "Python")

    evidence = _list(client, token).json()[0]["evidence"][0]

    assert "source_label" in evidence
    assert evidence["source_label"] is None


def test_github_evidence_is_labelled_with_the_repository(client: TestClient) -> None:
    """Closes the Prompt 3.3 gap where twenty repositories all rendered
    as the same four words, "From GitHub"."""
    _seed_taxonomy()
    token, user_id = _register_and_login(client, "alice@example.com")

    async def _write(session) -> None:  # type: ignore[no-untyped-def]
        skill = await session.scalar(select(Skill).where(Skill.slug == "docker"))
        assert skill is not None
        candidate_skill = CandidateSkill(
            user_id=uuid.UUID(user_id), skill_id=skill.id, status="suggested"
        )
        session.add(candidate_skill)
        await session.flush()
        session.add(
            SkillEvidence(
                candidate_skill_id=candidate_skill.id,
                source_type=EvidenceSourceType.GITHUB.value,
                source_identifier="ada/scheduler",
                excerpt="docker",
                extraction_method=ExtractionMethod.GITHUB_TOPIC_MATCH.value,
                confidence=Decimal("0.75"),
            )
        )
        await session.commit()

    _run(_write)

    evidence = _list(client, token).json()[0]["evidence"][0]

    assert evidence["source_label"] == "ada/scheduler"
