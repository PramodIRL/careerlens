"""Explainable skill gaps (Prompt 4.4).

The load-bearing tests here, which should not be softened:

  * the THREE non-satisfying states never collapse into one "missing"
    list — genuinely absent, user-rejected, and awaiting review are
    different things to tell a person
  * `/match` and `/gaps` agree on every requirement, by construction
  * GET mutates nothing and persists no gap rows
  * evidence is real stored rows, never generated text
  * the read set is a fixed size — the same number of SELECTs for a
    twelve-requirement job as for a one-requirement job
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

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

_BUCKETS = (
    "required_gaps",
    "preferred_gaps",
    "informational_gaps",
    "needs_confirmation",
    "rejected_requirements",
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


def _gaps(client: TestClient, token: str, job_id: str) -> dict[str, Any]:
    response = client.get(f"{_BASE}/{job_id}/gaps", headers=_headers(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def _match(client: TestClient, token: str, job_id: str) -> dict[str, Any]:
    response = client.get(f"{_BASE}/{job_id}/match", headers=_headers(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def _names(payload: dict[str, Any], bucket: str) -> list[str]:
    return [row["skill_name"] for row in payload[bucket]]


def _give_skill(
    user_id: str,
    skill_name: str,
    status: str,
    *,
    with_evidence: bool = True,
    source_type: str = "resume",
    excerpt: str | None = "Built backend services in Python",
) -> None:
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


# --- core ---------------------------------------------------------------


def test_a_missing_required_skill_is_a_required_gap(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Kubernetes is required for deployment.")

    gaps = _gaps(client, token, job_id)

    assert _names(gaps, "required_gaps") == ["Kubernetes"]
    assert gaps["required_gaps"][0]["job_excerpt"] == "Kubernetes is required for deployment"
    assert gaps["required_gaps"][0]["candidate_status"] is None
    # Never a manufactured "no evidence found" sentence.
    assert gaps["required_gaps"][0]["candidate_evidence"] == []


def test_a_missing_preferred_skill_is_a_preferred_gap(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Docker experience is preferred.")

    assert _names(_gaps(client, token, job_id), "preferred_gaps") == ["Docker"]


def test_a_missing_mentioned_skill_is_informational(client: TestClient) -> None:
    """A passing mention is not a failure — its own neutral bucket."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "We also use Redis here.")

    assert _names(_gaps(client, token, job_id), "informational_gaps") == ["Redis"]


def test_all_requirements_satisfied_produces_no_gaps(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    _give_skill(user_id, "Docker", "confirmed")
    job_id = _create_job(client, token, "Python is required. Docker is preferred.")

    gaps = _gaps(client, token, job_id)

    assert all(gaps[bucket] == [] for bucket in _BUCKETS)
    assert gaps["totals"]["satisfied"] == 2
    assert gaps["totals"]["total_requirements"] == 2


def test_a_job_with_no_requirements_is_empty_not_a_perfect_match(
    client: TestClient,
) -> None:
    """Distinct from "you match everything" — the UI must not
    congratulate a candidate for a job that asks for nothing."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "We are hiring a kind person to write poems.")

    gaps = _gaps(client, token, job_id)

    assert all(gaps[bucket] == [] for bucket in _BUCKETS)
    assert gaps["totals"]["total_requirements"] == 0
    assert gaps["totals"]["satisfied"] == 0


def test_the_formula_version_is_separate_from_the_match_version(
    client: TestClient,
) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Python is required.")

    assert _gaps(client, token, job_id)["formula_version"] == "skill_gap_v1"
    assert _match(client, token, job_id)["formula_version"] == "skill_match_v1"


# --- candidate status semantics -----------------------------------------


def test_a_confirmed_skill_satisfies_and_appears_in_no_bucket(
    client: TestClient,
) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    job_id = _create_job(client, token, "Python is required.")

    gaps = _gaps(client, token, job_id)

    assert all(gaps[bucket] == [] for bucket in _BUCKETS)
    assert gaps["totals"]["satisfied"] == 1


def test_a_confirmed_skill_with_no_evidence_still_satisfies(client: TestClient) -> None:
    """Reachable and intended: confirmed survives losing every piece of
    evidence, because the user asserted it."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed", with_evidence=False)
    job_id = _create_job(client, token, "Python is required.")

    assert _gaps(client, token, job_id)["totals"]["satisfied"] == 1


def test_a_suggested_skill_needs_confirmation_and_is_not_an_ordinary_gap(
    client: TestClient,
) -> None:
    """Evidence exists — calling it a gap would tell the user to go learn
    something they have already demonstrated."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "suggested")
    job_id = _create_job(client, token, "Python is required.")

    gaps = _gaps(client, token, job_id)

    assert _names(gaps, "needs_confirmation") == ["Python"]
    assert gaps["required_gaps"] == []
    assert gaps["needs_confirmation"][0]["candidate_status"] == "suggested"
    # ...and the evidence that justified the suggestion is shown.
    assert gaps["needs_confirmation"][0]["candidate_evidence"][0]["source_type"] == "resume"


def test_a_rejected_skill_is_its_own_bucket_not_ordinary_missing(
    client: TestClient,
) -> None:
    """Reporting a skill the user deliberately rejected as though the
    system simply failed to find it is the exact confusion this bucket
    exists to prevent."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Docker", "rejected")
    job_id = _create_job(client, token, "Docker experience is preferred.")

    gaps = _gaps(client, token, job_id)

    assert _names(gaps, "rejected_requirements") == ["Docker"]
    assert gaps["preferred_gaps"] == []
    assert gaps["rejected_requirements"][0]["candidate_status"] == "rejected"


def test_a_rejected_requirement_shows_the_evidence_the_user_disowned(
    client: TestClient,
) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Docker", "rejected", excerpt="Containerised the service with Docker")
    job_id = _create_job(client, token, "Docker is preferred.")

    entry = _gaps(client, token, job_id)["rejected_requirements"][0]

    assert entry["candidate_evidence"][0]["excerpt"] == "Containerised the service with Docker"
    assert entry["candidate_evidence"][0]["confidence"] == 0.90


def test_a_rejected_required_skill_keeps_its_required_level(client: TestClient) -> None:
    """The level is preserved so a rejected REQUIRED skill is still
    visibly required."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Docker", "rejected")
    job_id = _create_job(client, token, "Docker is required.")

    entry = _gaps(client, token, job_id)["rejected_requirements"][0]

    assert entry["requirement_level"] == "required"


def test_reading_gaps_never_mutates_a_candidate_status(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Docker", "rejected")
    job_id = _create_job(client, token, "Docker is required.")

    for _ in range(3):
        _gaps(client, token, job_id)

    async def _status(session: AsyncSession) -> str:
        row = await session.scalar(
            select(CandidateSkill).where(CandidateSkill.user_id == uuid.UUID(user_id))
        )
        assert row is not None
        return str(row.status)

    assert _run(_status) == "rejected"


def test_the_three_non_satisfying_states_are_never_collapsed(client: TestClient) -> None:
    """All three at once, each in its own bucket."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Docker", "rejected")
    _give_skill(user_id, "Redis", "suggested")
    job_id = _create_job(
        client, token, "Kubernetes is required. Docker is required. Redis is required."
    )

    gaps = _gaps(client, token, job_id)

    assert _names(gaps, "required_gaps") == ["Kubernetes"]
    assert _names(gaps, "rejected_requirements") == ["Docker"]
    assert _names(gaps, "needs_confirmation") == ["Redis"]


# --- ordering -----------------------------------------------------------


def test_entries_are_sorted_by_skill_name_within_a_bucket(client: TestClient) -> None:
    """Alphabetical, not database insertion order."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(
        client, token, "Redis is required. Docker is required. Kubernetes is required."
    )

    names = _names(_gaps(client, token, job_id), "required_gaps")

    assert names == sorted(names) == ["Docker", "Kubernetes", "Redis"]


def test_the_bucket_order_is_fixed(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Python is required.")

    keys = [k for k in _gaps(client, token, job_id) if k in _BUCKETS]

    assert keys == list(_BUCKETS)


def test_two_identical_requests_return_identical_json(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Docker", "rejected")
    _give_skill(user_id, "Redis", "suggested")
    job_id = _create_job(
        client, token, "Kubernetes is required. Docker is preferred. Redis is mentioned."
    )

    first = client.get(f"{_BASE}/{job_id}/gaps", headers=_headers(token))
    second = client.get(f"{_BASE}/{job_id}/gaps", headers=_headers(token))

    assert first.json() == second.json()


def test_multiple_evidence_rows_are_deterministic(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "suggested", excerpt="First evidence")

    async def _add_more(session: AsyncSession) -> None:
        row = await session.scalar(
            select(CandidateSkill).where(CandidateSkill.user_id == uuid.UUID(user_id))
        )
        assert row is not None
        session.add(
            SkillEvidence(
                candidate_skill_id=row.id,
                source_type="github",
                source_identifier="ada/toolkit",
                excerpt="Second evidence",
                extraction_method="github_readme_match",
                confidence=Decimal("0.90"),
            )
        )
        await session.commit()

    _run(_add_more)
    job_id = _create_job(client, token, "Python is required.")

    first = _gaps(client, token, job_id)["needs_confirmation"][0]["candidate_evidence"]
    second = _gaps(client, token, job_id)["needs_confirmation"][0]["candidate_evidence"]

    assert len(first) == 2
    assert first == second


# --- consistency with 4.3 ------------------------------------------------


def test_every_match_missing_skill_lands_in_exactly_one_gap_bucket(
    client: TestClient,
) -> None:
    """THE CONSISTENCY GUARANTEE the shared resolver exists to make
    structural."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    _give_skill(user_id, "Docker", "rejected")
    _give_skill(user_id, "Redis", "suggested")
    job_id = _create_job(
        client,
        token,
        "Python is required. Kubernetes is required. Docker is preferred. "
        "Redis is mentioned. Java is mentioned.",
    )

    match = _match(client, token, job_id)
    gaps = _gaps(client, token, job_id)

    all_gap_names: list[str] = []
    for bucket in _BUCKETS:
        all_gap_names.extend(_names(gaps, bucket))

    for row in match["missing_skills"]:
        assert all_gap_names.count(row["skill_name"]) == 1, (
            f"{row['skill_name']} should appear in exactly one bucket"
        )


def test_every_match_matched_skill_appears_in_no_gap_bucket(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    _give_skill(user_id, "Redis", "suggested")
    job_id = _create_job(client, token, "Python is required. Redis is required.")

    match = _match(client, token, job_id)
    gaps = _gaps(client, token, job_id)

    # Python is matched AND confirmed -> in no bucket at all.
    ordinary = _names(gaps, "required_gaps") + _names(gaps, "preferred_gaps")
    ordinary += _names(gaps, "informational_gaps")
    assert "Python" in [r["skill_name"] for r in match["matched_skills"]]
    assert "Python" not in ordinary
    assert "Python" not in _names(gaps, "rejected_requirements")
    # Redis is matched for scoring but still needs review.
    assert "Redis" in [r["skill_name"] for r in match["matched_skills"]]
    assert _names(gaps, "needs_confirmation") == ["Redis"]
    assert "Redis" not in ordinary


def test_the_totals_reconcile_with_the_requirement_count(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "confirmed")
    _give_skill(user_id, "Docker", "rejected")
    _give_skill(user_id, "Redis", "suggested")
    job_id = _create_job(
        client,
        token,
        "Python is required. Kubernetes is required. Docker is preferred. Redis is mentioned.",
    )

    totals = _gaps(client, token, job_id)["totals"]

    assert (
        totals["satisfied"]
        + totals["required_gaps"]
        + totals["preferred_gaps"]
        + totals["informational_gaps"]
        + totals["needs_confirmation"]
        + totals["rejected_requirements"]
        == totals["total_requirements"]
        == 4
    )


# --- ownership -----------------------------------------------------------


def test_gaps_require_authentication(client: TestClient) -> None:
    assert client.get(f"{_BASE}/{uuid.uuid4()}/gaps").status_code == 401


def test_another_users_job_gaps_are_forbidden(client: TestClient) -> None:
    _seed_taxonomy()
    owner, _ = _new_user(client)
    other, _ = _new_user(client)
    job_id = _create_job(client, owner, "Python is required.")

    assert client.get(f"{_BASE}/{job_id}/gaps", headers=_headers(other)).status_code == 403


def test_a_nonexistent_job_is_404(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)

    assert client.get(f"{_BASE}/{uuid.uuid4()}/gaps", headers=_headers(token)).status_code == 404


def test_a_deleted_job_no_longer_has_gaps(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Python is required.")

    assert client.delete(f"{_BASE}/{job_id}", headers=_headers(token)).status_code == 204

    assert client.get(f"{_BASE}/{job_id}/gaps", headers=_headers(token)).status_code == 404


# --- data changes --------------------------------------------------------


def test_confirming_a_skill_removes_the_gap(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "rejected")
    job_id = _create_job(client, token, "Python is required.")
    assert _names(_gaps(client, token, job_id), "rejected_requirements") == ["Python"]

    _set_status(user_id, "Python", "confirmed")

    gaps = _gaps(client, token, job_id)
    assert all(gaps[bucket] == [] for bucket in _BUCKETS)


def test_confirming_a_suggested_skill_leaves_needs_confirmation(
    client: TestClient,
) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "suggested")
    job_id = _create_job(client, token, "Python is required.")
    assert _names(_gaps(client, token, job_id), "needs_confirmation") == ["Python"]

    _set_status(user_id, "Python", "confirmed")

    assert _gaps(client, token, job_id)["needs_confirmation"] == []


def test_rejecting_a_skill_moves_it_to_the_rejected_bucket(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _give_skill(user_id, "Python", "suggested")
    job_id = _create_job(client, token, "Python is required.")

    _set_status(user_id, "Python", "rejected")

    gaps = _gaps(client, token, job_id)
    assert _names(gaps, "rejected_requirements") == ["Python"]
    assert gaps["needs_confirmation"] == []
    assert gaps["required_gaps"] == []


def test_manually_adding_a_skill_removes_the_gap(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Kubernetes is required.")
    assert _names(_gaps(client, token, job_id), "required_gaps") == ["Kubernetes"]

    client.post("/api/v1/candidate-skills", headers=_headers(token), json={"name": "Kubernetes"})

    assert _gaps(client, token, job_id)["required_gaps"] == []


def test_editing_the_job_description_reconciles_the_gaps(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Python is required.")
    assert _names(_gaps(client, token, job_id), "required_gaps") == ["Python"]

    client.patch(
        f"{_BASE}/{job_id}",
        headers=_headers(token),
        json={"description": "Kubernetes is required."},
    )

    assert _names(_gaps(client, token, job_id), "required_gaps") == ["Kubernetes"]


def test_an_unrelated_job_edit_leaves_gaps_unchanged(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Python is required.")
    before = _gaps(client, token, job_id)

    client.patch(f"{_BASE}/{job_id}", headers=_headers(token), json={"company": "Renamed Ltd"})

    assert _gaps(client, token, job_id) == before


# --- the read-only boundary ----------------------------------------------


def test_a_gap_request_mutates_nothing(client: TestClient) -> None:
    """Asserted on ids AND timestamps across every table the endpoint
    reads — a count-only check would miss an in-place UPDATE."""
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
            rows.add(("ev", ev.id, ev.excerpt, ev.created_at, ev.updated_at))
        for req in (await session.scalars(select(JobSkillRequirement))).all():
            rows.add(("req", req.id, req.requirement_level, req.created_at, req.updated_at))
        for job in (await session.scalars(select(SavedJob))).all():
            rows.add(("job", job.id, job.description, job.created_at, job.updated_at))
        return rows

    before = _run(_snapshot)

    for _ in range(3):
        _gaps(client, token, job_id)

    assert _run(_snapshot) == before


def test_a_gap_request_creates_no_candidate_rows(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    job_id = _create_job(client, token, "Python and Kubernetes are required.")

    async def _counts(session: AsyncSession) -> tuple[int, int]:
        return (
            (await session.scalar(select(func.count()).select_from(CandidateSkill))) or 0,
            (await session.scalar(select(func.count()).select_from(SkillEvidence))) or 0,
        )

    _gaps(client, token, job_id)

    assert _run(_counts) == (0, 0)


def test_no_gap_table_exists(client: TestClient) -> None:
    """Gaps are derived on read — nothing is persisted, so there is no
    table that could go stale."""

    async def _tables(session: AsyncSession) -> list[str]:
        from sqlalchemy import text

        rows = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name LIKE '%gap%'"
            ),
            {"schema": TEST_SCHEMA},
        )
        return [row[0] for row in rows]

    assert _run(_tables) == []


# --- query cost ---------------------------------------------------------


def _add_evidence(user_id: str, skill_name: str, count: int) -> None:
    """Pile extra evidence rows onto an existing candidate skill, so the
    fan-out an N+1 would follow is wide on the evidence side too."""

    async def _write(session: AsyncSession) -> None:
        skill = await session.scalar(select(Skill).where(Skill.slug == skill_name.casefold()))
        assert skill is not None
        row = await session.scalar(
            select(CandidateSkill).where(
                CandidateSkill.user_id == uuid.UUID(user_id),
                CandidateSkill.skill_id == skill.id,
            )
        )
        assert row is not None, f"{skill_name!r} has no candidate skill to attach evidence to"
        for n in range(count):
            session.add(
                SkillEvidence(
                    candidate_skill_id=row.id,
                    source_type="github",
                    source_identifier=f"ada/repo-{skill_name}-{n}",
                    excerpt=f"Used {skill_name} in repo {n}",
                    extraction_method="github_readme_match",
                    confidence=Decimal("0.90"),
                )
            )
        await session.commit()

    _run(_write)


def _count_gap_selects(client: TestClient, token: str, job_id: str) -> tuple[int, dict[str, Any]]:
    """SELECTs one real `/gaps` request issues, counted on the engine the
    ENDPOINT itself uses.

    `get_db` is temporarily pointed at an instrumented engine, rather
    than counting a hand-written replay of the queries we believe the
    endpoint makes. A replay only proves the shape we wrote down is
    bounded; this proves the shipped request path is — so a later change
    that reaches into a relationship per requirement is caught here
    instead of quietly diverging from a replay nobody updated.

    The engine is warmed first: SQLAlchemy runs a one-off dialect
    initialisation on first connect, and those SELECTs are not the
    endpoint's. NullPool for the same reason conftest.py gives — a
    connection is opened and closed inside the one loop that uses it.
    """
    engine = create_async_engine(
        get_settings().database_url,
        connect_args=_SEARCH_PATH_CONNECT_ARGS,
        poolclass=NullPool,
    )
    counter = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _warm() -> None:
        async with factory() as session:
            await session.execute(select(1))

    async def _instrumented_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    asyncio.run(_warm())

    previous = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = _instrumented_db
    counter["n"] = 0
    try:
        payload = _gaps(client, token, job_id)
    finally:
        app.dependency_overrides[get_db] = previous
        asyncio.run(engine.dispose())

    return counter["n"], payload


# Twelve requirements across all three levels, so an implementation that
# went back to the database once per requirement would be unmistakable.
_LARGE_JOB_DESCRIPTION = (
    "Python is required. Docker is required. Redis is required. "
    "Git is required. Linux is required. PostgreSQL is required. "
    "Kubernetes is preferred. Java is preferred. React is preferred. "
    "TypeScript is preferred. We also use Django here. We also use Flask here."
)


def test_the_gap_query_count_is_bounded_and_constant(client: TestClient) -> None:
    """`/gaps` costs the same fixed number of SELECTs whether the job has
    one requirement or twelve.

    Asserted as an EQUALITY between two fixture sizes, not just a small
    upper bound: a per-requirement or per-evidence-row query would make
    the large job cost strictly more, and that difference is the thing
    this test exists to catch. The absolute ceiling is asserted too, so
    "constant but large" cannot pass either.
    """
    _seed_taxonomy()
    token, user_id = _new_user(client)

    # Eight candidate skills covering every resolution state, three of
    # them with a wide evidence fan-out.
    for name in ("Python", "Docker", "Git", "PostgreSQL"):
        _give_skill(user_id, name, "confirmed")
    for name in ("Redis", "React"):
        _give_skill(user_id, name, "suggested")
    for name in ("Linux", "TypeScript"):
        _give_skill(user_id, name, "rejected")
    for name in ("Python", "Redis", "Linux"):
        _add_evidence(user_id, name, 4)

    small_job = _create_job(client, token, "Python is required.")
    large_job = _create_job(client, token, _LARGE_JOB_DESCRIPTION)

    small_queries, small = _count_gap_selects(client, token, small_job)
    large_queries, large = _count_gap_selects(client, token, large_job)

    # Guard the fixture itself: if extraction ever stopped producing
    # these rows, the equality below would pass on two trivial jobs and
    # prove nothing.
    assert small["totals"]["total_requirements"] == 1
    assert large["totals"]["total_requirements"] == 12
    assert (
        sum(len(entry["candidate_evidence"]) for bucket in _BUCKETS for entry in large[bucket])
        >= 10
    )

    assert large_queries == small_queries, (
        f"the read set grew with the fixture: {small_queries} -> {large_queries} SELECTs"
    )
    assert large_queries <= 6, f"expected a bounded read set, got {large_queries} SELECTs"
