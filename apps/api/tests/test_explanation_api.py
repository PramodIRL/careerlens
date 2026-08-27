"""The explanation endpoint end to end (Prompt 6.1).

The load-bearing tests here, which should not be softened:

  * `/match` and `/gaps` are BYTE-IDENTICAL before and after an
    explanation is requested — the LLM slice changes no score and no
    fact
  * GET /explanation mutates nothing, in any table
  * every cited excerpt is a stored `skill_evidence` row, and the ids
    are real
  * a rejected explanation is a 200 carrying the deterministic score
    and NO generated content
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.explanation.prompt import ExplanationRequest
from app.main import app
from app.models.candidate_skill import CandidateSkill
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.rate_limit import _request_log
from app.settings import get_settings
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_BASE = "/api/v1/saved-jobs"
_DESCRIPTION = "Python is required. PostgreSQL is required. Docker is preferred."


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


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _new_user(client: TestClient) -> tuple[str, str]:
    email = f"{uuid.uuid4()}@example.com"
    register = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return str(login.json()["access_token"]), str(register.json()["id"])


def _create_job(client: TestClient, token: str, description: str = _DESCRIPTION) -> str:
    response = client.post(
        _BASE,
        headers=_headers(token),
        json={"company": "Acme", "title": "Engineer", "description": description},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _give_skill(user_id: str, skill_name: str, status: str, excerpt: str) -> None:
    async def _write(session: AsyncSession) -> None:
        skill = await session.scalar(select(Skill).where(Skill.slug == skill_name.casefold()))
        assert skill is not None
        candidate = CandidateSkill(user_id=uuid.UUID(user_id), skill_id=skill.id, status=status)
        session.add(candidate)
        await session.flush()
        session.add(
            SkillEvidence(
                candidate_skill_id=candidate.id,
                source_type="resume",
                source_identifier=str(uuid.uuid4()),
                excerpt=excerpt,
                extraction_method="resume_alias_match",
                confidence=Decimal("0.90"),
            )
        )
        await session.commit()

    _run(_write)


def _seeded_candidate(client: TestClient) -> tuple[str, str]:
    """The worked example: Python + PostgreSQL confirmed, Docker absent."""
    _run(lambda session: seed_skill_taxonomy(session))
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed", "Built backend services in Python")
    _give_skill(user_id, "PostgreSQL", "confirmed", "Designed the PostgreSQL schema")
    return token, user_id


async def _stored_excerpts(session: AsyncSession) -> list[str | None]:
    return list((await session.scalars(select(SkillEvidence.excerpt))).all())


def _get(client: TestClient, token: str, job_id: str, suffix: str) -> dict[str, Any]:
    response = client.get(f"{_BASE}/{job_id}/{suffix}", headers=_headers(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- the happy path ----------------------------------------------------


def test_an_explanation_is_grounded_in_stored_evidence(client: TestClient) -> None:
    token, _ = _seeded_candidate(client)
    job_id = _create_job(client, token)

    body = _get(client, token, job_id, "explanation")

    assert body["status"] == "generated"
    assert body["reason"] is None
    assert body["schema_version"] == "match_explanation_v1"
    assert body["provider"] == "mock"
    assert body["summary"]
    assert body["strengths"]

    # Every cited id is a real evidence row, and its excerpt is the
    # stored one — the model chose which rows to point at and supplied
    # none of their text.
    excerpts = set(_run(_stored_excerpts))
    cited_ids = {row["evidence_id"] for row in body["cited_evidence"]}
    assert cited_ids
    for row in body["cited_evidence"]:
        assert row["excerpt"] in excerpts
    for claim in body["strengths"]:
        assert claim["evidence_ids"]
        assert set(claim["evidence_ids"]) <= cited_ids


def test_the_score_is_echoed_not_produced_by_the_model(client: TestClient) -> None:
    """75 comes from skill_match_v1 and appears in the explanation
    response unchanged — the LLM neither computed nor adjusted it."""
    token, _ = _seeded_candidate(client)
    job_id = _create_job(client, token)

    match = _get(client, token, job_id, "match")
    explanation = _get(client, token, job_id, "explanation")

    assert match["overall_score"] == 75
    assert explanation["overall_score"] == match["overall_score"]
    assert explanation["match_formula_version"] == "skill_match_v1"
    assert explanation["gap_formula_version"] == "skill_gap_v1"
    assert explanation["semantic_formula_version"] == "semantic_fit_v1"


def test_two_identical_requests_return_identical_json(client: TestClient) -> None:
    token, _ = _seeded_candidate(client)
    job_id = _create_job(client, token)

    first = client.get(f"{_BASE}/{job_id}/explanation", headers=_headers(token))
    second = client.get(f"{_BASE}/{job_id}/explanation", headers=_headers(token))

    assert first.json() == second.json()


# --- the guarantees this slice owes the rest of the app ----------------


def test_match_and_gaps_are_byte_identical_around_an_explanation(
    client: TestClient,
) -> None:
    """THE CENTRAL PROMISE OF 6.1. Requesting an explanation changes
    nothing about the deterministic answers, before or after."""
    token, _ = _seeded_candidate(client)
    job_id = _create_job(client, token)

    match_before = client.get(f"{_BASE}/{job_id}/match", headers=_headers(token)).content
    gaps_before = client.get(f"{_BASE}/{job_id}/gaps", headers=_headers(token)).content

    assert client.get(f"{_BASE}/{job_id}/explanation", headers=_headers(token)).status_code == 200

    assert client.get(f"{_BASE}/{job_id}/match", headers=_headers(token)).content == match_before
    assert client.get(f"{_BASE}/{job_id}/gaps", headers=_headers(token)).content == gaps_before


def test_the_endpoint_writes_nothing(client: TestClient) -> None:
    """No explanation row, no evidence row, no candidate skill — an
    explanation is derived on read and persisted nowhere."""
    token, _ = _seeded_candidate(client)
    job_id = _create_job(client, token)

    def _counts(session: AsyncSession) -> Awaitable[Any]:
        return session.execute(
            select(
                select(func.count()).select_from(SkillEvidence).scalar_subquery(),
                select(func.count()).select_from(CandidateSkill).scalar_subquery(),
            )
        )

    before = _run(_counts).one()
    assert client.get(f"{_BASE}/{job_id}/explanation", headers=_headers(token)).status_code == 200
    assert _run(_counts).one() == before


def test_a_job_with_no_recognised_requirements_explains_that(client: TestClient) -> None:
    """ "This posting asks for nothing we recognise" is a statement about
    the JOB, and must not read as a 0% candidate."""
    token, _ = _seeded_candidate(client)
    job_id = _create_job(client, token, "We are looking for a passionate team player.")

    body = _get(client, token, job_id, "explanation")

    assert body["status"] == "generated"
    assert body["has_requirements"] is False
    assert body["overall_score"] == 0
    assert body["strengths"] == []


# --- rejection ---------------------------------------------------------


def test_a_rejected_explanation_is_a_200_with_no_generated_content(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deterministic answer is intact and worth serving; the model
    failing to explain it is not an error in the match."""

    class _BadProvider:
        @property
        def name(self) -> str:
            return "mock"

        async def complete(self, request: ExplanationRequest) -> str:
            return '{"schema_version": "match_explanation_v1", "summary": '

    monkeypatch.setattr(
        "app.api.v1.saved_job.get_explanation_provider", lambda *args, **kwargs: _BadProvider()
    )

    token, _ = _seeded_candidate(client)
    job_id = _create_job(client, token)
    body = _get(client, token, job_id, "explanation")

    assert body["status"] == "rejected"
    assert body["reason"] == "malformed_json"
    # The score survives a rejected explanation untouched.
    assert body["overall_score"] == 75
    # And NOTHING generated comes back.
    assert body["summary"] is None
    assert body["strengths"] == []
    assert body["gaps"] == []
    assert body["next_steps"] == []
    assert body["cited_evidence"] == []


# --- ownership ---------------------------------------------------------


def test_another_users_job_cannot_be_explained(client: TestClient) -> None:
    token, _ = _seeded_candidate(client)
    job_id = _create_job(client, token)
    other_token, _ = _new_user(client)

    response = client.get(f"{_BASE}/{job_id}/explanation", headers=_headers(other_token))

    assert response.status_code == 403
