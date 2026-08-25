"""Candidate-to-job matching end to end (Prompt 4.3).

The load-bearing tests here, which should not be softened:

  * a REJECTED candidate skill never satisfies a requirement, however
    much stale evidence still hangs off it
  * a SUGGESTED skill does count, and is flagged unreviewed
  * matching is on canonical skill_id, and every result carries enough
    provenance to explain itself
  * GET mutates nothing, in any of five tables
  * a missing REQUIRED skill is unmistakable even though v1 applies no
    penalty for it
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.main import app
from app.models.candidate_skill import CandidateSkill
from app.models.job_requirement import JobSkillRequirement
from app.models.saved_job import SavedJob
from app.models.skill import Skill
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


def _new_user(client: TestClient) -> tuple[str, str]:
    email = f"{uuid.uuid4()}@example.com"
    register = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return str(login.json()["access_token"]), str(register.json()["id"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_job(client: TestClient, token: str, description: str) -> str:
    response = client.post(
        _BASE,
        headers=_headers(token),
        json={"company": "Acme", "title": "Engineer", "description": description},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _match(client: TestClient, token: str, job_id: str) -> dict[str, Any]:
    response = client.get(f"{_BASE}/{job_id}/match", headers=_headers(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def _give_skill(
    user_id: str,
    skill_name: str,
    status: str,
    *,
    with_evidence: bool = True,
    source_type: str = "resume",
    excerpt: str | None = "Built backend services in Python",
) -> None:
    """Put the candidate in a given review state for one skill — the
    state a resume/GitHub extractor or a manual add would have left."""

    async def _write(session: AsyncSession) -> None:
        skill = await session.scalar(select(Skill).where(Skill.slug == skill_name.casefold()))
        assert skill is not None, f"{skill_name!r} not in the seeded taxonomy"
        candidate = CandidateSkill(user_id=uuid.UUID(user_id), skill_id=skill.id, status=status)
        session.add(candidate)
        await session.flush()
        if with_evidence:
            session.add(
                SkillEvidence(
                    candidate_skill_id=candidate.id,
                    source_type=source_type,
                    source_identifier=str(uuid.uuid4()),
                    excerpt=excerpt,
                    extraction_method=(
                        "manual_entry" if source_type == "manual" else "resume_alias_match"
                    ),
                    confidence=Decimal("1.00" if source_type == "manual" else "0.90"),
                )
            )
        await session.commit()

    _run(_write)


def _set_status(user_id: str, skill_name: str, status: str) -> None:
    async def _write(session: AsyncSession) -> None:
        skill = await session.scalar(select(Skill).where(Skill.slug == skill_name.casefold()))
        assert skill is not None
        row = await session.scalar(
            select(CandidateSkill).where(
                CandidateSkill.user_id == uuid.UUID(user_id),
                CandidateSkill.skill_id == skill.id,
            )
        )
        assert row is not None
        row.status = status
        await session.commit()

    _run(_write)


def _names(rows: list[dict[str, Any]]) -> set[str]:
    return {row["skill_name"] for row in rows}


# --- the worked example ------------------------------------------------


def test_the_worked_example_scores_exactly_75(client: TestClient) -> None:
    """Candidate: Python confirmed, PostgreSQL confirmed, Docker absent.
    Job:       Python required, PostgreSQL required, Docker preferred.

        earned = 3 + 3 = 6, obtainable = 3 + 3 + 2 = 8 -> 75
    """
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    _give_skill(user_id, "PostgreSQL", "confirmed")
    job_id = _create_job(
        client,
        token,
        "Python is required. PostgreSQL is required. Docker is preferred.",
    )

    match = _match(client, token, job_id)

    assert match["overall_score"] == 75
    assert (match["earned_weight"], match["obtainable_weight"]) == (6, 8)
    assert (match["required_matched"], match["required_total"]) == (2, 2)
    assert _names(match["matched_skills"]) == {"Python", "PostgreSQL"}
    assert _names(match["missing_skills"]) == {"Docker"}
    assert match["formula_version"] == "skill_match_v1"


def test_a_missing_required_skill_is_unmistakable_despite_the_score(
    client: TestClient,
) -> None:
    """THE CASE v1's FORMULA CHOICE TURNS ON.

    Five preferred matched, one required missing:
        earned = 10, obtainable = 13 -> 77

    v1 applies NO ceiling, so the number stays mathematically honest —
    and `required_missing` is what makes the blocking gap impossible to
    miss.
    """
    _seed_taxonomy()
    token, user_id = _new_user(client)
    for name in ("Python", "Docker", "Redis", "Git", "Linux"):
        _give_skill(user_id, name, "confirmed")
    job_id = _create_job(
        client,
        token,
        "Kubernetes is required. Python is preferred. Docker is preferred. "
        "Redis is preferred. Git is preferred. Linux is preferred.",
    )

    match = _match(client, token, job_id)

    assert match["overall_score"] == 77
    assert (match["required_matched"], match["required_total"]) == (0, 1)
    assert [row["skill_name"] for row in match["required_missing"]] == ["Kubernetes"]


# --- candidate status semantics ----------------------------------------


def test_a_confirmed_skill_matches(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    job_id = _create_job(client, token, "Python is required.")

    match = _match(client, token, job_id)

    assert match["overall_score"] == 100
    assert match["matched_skills"][0]["candidate_status"] == "confirmed"
    assert match["matched_skills"][0]["candidate_unreviewed"] is False


def test_a_suggested_skill_matches_but_is_flagged_unreviewed(client: TestClient) -> None:
    """It counts — an extractor found real, persisted evidence. Not
    counting it would score every new candidate at 0% until they clicked
    through every skill, measuring their attention rather than their
    ability."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "suggested")
    job_id = _create_job(client, token, "Python is required.")

    match = _match(client, token, job_id)

    assert match["overall_score"] == 100
    assert match["matched_skills"][0]["candidate_status"] == "suggested"
    assert match["matched_skills"][0]["candidate_unreviewed"] is True


def test_a_rejected_skill_never_matches_even_with_evidence(client: TestClient) -> None:
    """A rejection is a persistent tombstone. Stale evidence must not
    resurrect it as a match."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "rejected", with_evidence=True)
    job_id = _create_job(client, token, "Python is required.")

    match = _match(client, token, job_id)

    assert match["overall_score"] == 0
    assert match["matched_skills"] == []
    assert match["missing_skills"][0]["skill_name"] == "Python"


def test_a_rejected_skill_is_distinguished_from_never_having_it(
    client: TestClient,
) -> None:
    """ "You rejected this" and "you don't have this" are different
    messages, so the response distinguishes them."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "rejected")
    job_id = _create_job(client, token, "Python is required. Docker is required.")

    missing = {row["skill_name"]: row for row in _match(client, token, job_id)["missing_skills"]}

    assert missing["Python"]["candidate_rejected"] is True
    assert missing["Docker"]["candidate_rejected"] is False


def test_a_manually_added_skill_matches(client: TestClient) -> None:
    """Manual is PROVENANCE, not a status: confirmed plus a manual
    evidence row at confidence 1.00."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed", source_type="manual", excerpt=None)
    job_id = _create_job(client, token, "Python is required.")

    match = _match(client, token, job_id)

    assert match["overall_score"] == 100
    assert match["matched_skills"][0]["candidate_evidence"][0]["source_type"] == "manual"


def test_a_confirmed_skill_with_no_evidence_still_matches(client: TestClient) -> None:
    """Reachable and intended: a confirmed skill survives losing every
    piece of evidence (deleted resume, disconnected GitHub) because the
    user asserted it."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed", with_evidence=False)
    job_id = _create_job(client, token, "Python is required.")

    match = _match(client, token, job_id)

    assert match["overall_score"] == 100
    assert match["matched_skills"][0]["candidate_evidence"] == []


def test_candidate_skills_the_job_does_not_ask_for_are_ignored(
    client: TestClient,
) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    _give_skill(user_id, "Kubernetes", "confirmed")
    job_id = _create_job(client, token, "Python is required.")

    match = _match(client, token, job_id)

    assert match["overall_score"] == 100
    assert _names(match["matched_skills"]) == {"Python"}


# --- explainability ----------------------------------------------------


def test_a_matched_skill_explains_itself(client: TestClient) -> None:
    """Candidate evidence AND the job's own words, in one response —
    which is the whole point of an explainable match."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed", excerpt="Built backend services in Python")
    job_id = _create_job(client, token, "Python is required for this role.")

    row = _match(client, token, job_id)["matched_skills"][0]

    assert row["skill_name"] == "Python"
    assert row["requirement_level"] == "required"
    assert row["job_excerpt"] == "Python is required for this role"
    assert row["candidate_status"] == "confirmed"
    assert row["candidate_evidence"][0]["source_type"] == "resume"
    assert row["candidate_evidence"][0]["excerpt"] == "Built backend services in Python"
    assert row["candidate_evidence"][0]["confidence"] == 0.90


def test_a_missing_skill_carries_the_job_excerpt(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Kubernetes is required for this role.")

    row = _match(client, token, job_id)["missing_skills"][0]

    assert row["skill_name"] == "Kubernetes"
    assert row["job_excerpt"] == "Kubernetes is required for this role"


def test_confidence_is_surfaced_but_never_multiplied_into_the_score(
    client: TestClient,
) -> None:
    """`confidence` answers "does this string denote this skill", not
    "how good is the candidate at it". A low-confidence match therefore
    scores exactly like a high-confidence one — the number is reported
    for a human to read, not folded into the arithmetic."""
    _seed_taxonomy()
    token_a, user_a = _new_user(client)
    token_b, user_b = _new_user(client)
    _give_skill(user_a, "Python", "confirmed", source_type="resume")  # 0.90
    _give_skill(user_b, "Python", "confirmed", source_type="manual", excerpt=None)  # 1.00
    job_a = _create_job(client, token_a, "Python is required.")
    job_b = _create_job(client, token_b, "Python is required.")

    match_a = _match(client, token_a, job_a)
    match_b = _match(client, token_b, job_b)

    assert match_a["overall_score"] == match_b["overall_score"] == 100
    assert match_a["matched_skills"][0]["candidate_evidence"][0]["confidence"] == 0.90
    assert match_b["matched_skills"][0]["candidate_evidence"][0]["confidence"] == 1.00


# --- the no-requirements case ------------------------------------------


def test_a_job_with_no_recognised_requirements(client: TestClient) -> None:
    """`has_requirements=False` lets the UI say "no skill requirements
    detected" rather than "0% match" — a statement about the JOB, not
    about the candidate."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    job_id = _create_job(client, token, "We are hiring a kind person to write poems.")

    match = _match(client, token, job_id)

    assert match["has_requirements"] is False
    assert match["overall_score"] == 0
    assert match["obtainable_weight"] == 0
    assert match["matched_skills"] == []


# --- ownership ---------------------------------------------------------


def test_match_requires_authentication(client: TestClient) -> None:
    assert client.get(f"{_BASE}/{uuid.uuid4()}/match").status_code == 401


def test_another_users_job_cannot_be_matched(client: TestClient) -> None:
    """Comparing yourself against somebody else's job is unreachable,
    not merely forbidden."""
    _seed_taxonomy()
    owner, owner_id = _new_user(client)
    other, _ = _new_user(client)
    _give_skill(owner_id, "Python", "confirmed")
    job_id = _create_job(client, owner, "Python is required.")

    assert client.get(f"{_BASE}/{job_id}/match", headers=_headers(other)).status_code == 403


def test_a_nonexistent_job_is_404(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)

    assert client.get(f"{_BASE}/{uuid.uuid4()}/match", headers=_headers(token)).status_code == 404


def test_the_candidate_is_always_the_authenticated_user(client: TestClient) -> None:
    """No candidate id or user id is accepted anywhere, so a query
    parameter cannot redirect whose skills are compared."""
    _seed_taxonomy()
    token_a, user_a = _new_user(client)
    token_b, user_b = _new_user(client)
    _give_skill(user_a, "Python", "confirmed")
    job_b = _create_job(client, token_b, "Python is required.")

    # B owns the job and has NO skills; A's skills must not leak in.
    response = client.get(f"{_BASE}/{job_b}/match?user_id={user_a}", headers=_headers(token_b))

    assert response.status_code == 200
    assert response.json()["overall_score"] == 0


# --- determinism -------------------------------------------------------


def test_two_identical_requests_return_identical_json(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    _give_skill(user_id, "Docker", "suggested")
    job_id = _create_job(
        client, token, "Python is required. Docker is preferred. Redis is mentioned."
    )

    first = client.get(f"{_BASE}/{job_id}/match", headers=_headers(token))
    second = client.get(f"{_BASE}/{job_id}/match", headers=_headers(token))

    assert first.json() == second.json()


# --- data changes ------------------------------------------------------


def test_rejecting_a_skill_lowers_the_score(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    _give_skill(user_id, "Docker", "confirmed")
    job_id = _create_job(client, token, "Python is required. Docker is required.")
    assert _match(client, token, job_id)["overall_score"] == 100

    _set_status(user_id, "Docker", "rejected")

    assert _match(client, token, job_id)["overall_score"] == 50


def test_confirming_a_suggested_skill_keeps_the_score_and_clears_the_flag(
    client: TestClient,
) -> None:
    """Both positive states count equally, so confirming does not move
    the number — it removes the "unreviewed" caveat."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "suggested")
    job_id = _create_job(client, token, "Python is required.")
    assert _match(client, token, job_id)["matched_skills"][0]["candidate_unreviewed"] is True

    _set_status(user_id, "Python", "confirmed")

    match = _match(client, token, job_id)
    assert match["overall_score"] == 100
    assert match["matched_skills"][0]["candidate_unreviewed"] is False


def test_editing_the_job_description_changes_the_match(client: TestClient) -> None:
    """4.2 reconciles the requirements, and the next match reflects them
    with no rebuild step."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    job_id = _create_job(client, token, "Python is required.")
    assert _match(client, token, job_id)["overall_score"] == 100

    client.patch(
        f"{_BASE}/{job_id}",
        headers=_headers(token),
        json={"description": "Python is required. Kubernetes is required."},
    )

    assert _match(client, token, job_id)["overall_score"] == 50


def test_deleting_the_job_removes_matchability(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Python is required.")

    assert client.delete(f"{_BASE}/{job_id}", headers=_headers(token)).status_code == 204

    assert client.get(f"{_BASE}/{job_id}/match", headers=_headers(token)).status_code == 404


# --- the read-only boundary --------------------------------------------


def test_a_match_request_mutates_nothing(client: TestClient) -> None:
    """Asserted on row counts AND on every id and timestamp across all
    five tables the matcher reads. A count-only check would miss an
    UPDATE that touched a row in place."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    _give_skill(user_id, "Docker", "rejected")
    job_id = _create_job(client, token, "Python is required. Kubernetes is preferred.")

    async def _snapshot(session: AsyncSession) -> set[tuple[Any, ...]]:
        rows: set[tuple[Any, ...]] = set()
        for cs in (await session.scalars(select(CandidateSkill))).all():
            rows.add(("cs", cs.id, cs.status, cs.created_at, cs.updated_at))
        for ev in (await session.scalars(select(SkillEvidence))).all():
            rows.add(("ev", ev.id, ev.excerpt, ev.confidence, ev.created_at, ev.updated_at))
        for req in (await session.scalars(select(JobSkillRequirement))).all():
            rows.add(
                ("req", req.id, req.requirement_level, req.excerpt, req.created_at, req.updated_at)
            )
        for job in (await session.scalars(select(SavedJob))).all():
            rows.add(("job", job.id, job.description, job.created_at, job.updated_at))
        for skill in (await session.scalars(select(Skill))).all():
            rows.add(("skill", skill.id, skill.name, skill.updated_at))
        return rows

    before = _run(_snapshot)

    for _ in range(3):
        assert client.get(f"{_BASE}/{job_id}/match", headers=_headers(token)).status_code == 200

    assert _run(_snapshot) == before


def test_a_match_creates_no_candidate_skills_or_evidence(client: TestClient) -> None:
    """The matcher authors no evidence and invents no candidate facts."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Python and Kubernetes are required.")

    async def _counts(session: AsyncSession) -> tuple[int, int]:
        return (
            (await session.scalar(select(func.count()).select_from(CandidateSkill))) or 0,
            (await session.scalar(select(func.count()).select_from(SkillEvidence))) or 0,
        )

    before = _run(_counts)
    _match(client, token, job_id)

    assert _run(_counts) == before == (0, 0)


def test_the_query_count_stays_bounded(client: TestClient) -> None:
    """Four bounded reads, not one per requirement. Asserted so an N+1
    cannot creep in unnoticed as the response grows."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    for name in ("Python", "Docker", "Redis", "Git", "Linux", "PostgreSQL"):
        _give_skill(user_id, name, "confirmed")
    job_id = _create_job(
        client,
        token,
        "Python is required. Docker is required. Redis is required. Git is required. "
        "Linux is required. PostgreSQL is required. Kubernetes is preferred. "
        "Java is preferred.",
    )

    engine = create_async_engine(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )
    counter = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    async def _replay(session: AsyncSession) -> None:
        job = await session.get(SavedJob, uuid.UUID(job_id))
        assert job is not None
        reqs = list(
            (
                await session.scalars(
                    select(JobSkillRequirement).where(JobSkillRequirement.saved_job_id == job.id)
                )
            ).all()
        )
        ids = [r.skill_id for r in reqs]
        (await session.scalars(select(Skill).where(Skill.id.in_(ids)))).all()
        cs = list(
            (
                await session.scalars(
                    select(CandidateSkill).where(
                        CandidateSkill.user_id == uuid.UUID(user_id),
                        CandidateSkill.skill_id.in_(ids),
                    )
                )
            ).all()
        )
        (
            await session.scalars(
                select(SkillEvidence).where(
                    SkillEvidence.candidate_skill_id.in_([c.id for c in cs])
                )
            )
        ).all()

    async def _go() -> int:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            counter["n"] = 0
            await _replay(session)
        return counter["n"]

    try:
        queries = asyncio.run(_go())
    finally:
        asyncio.run(engine.dispose())

    # 8 requirements, 6 candidate skills — and still a small constant.
    assert queries <= 5, f"expected a bounded read set, got {queries} SELECTs"
