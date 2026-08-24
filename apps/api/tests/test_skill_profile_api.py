"""API tests for the unified skill profile (Prompt 3.4).

    GET /api/v1/skill-profile

The endpoint is READ-ONLY, so the tests that matter most are the ones
that would catch it quietly becoming otherwise:

  * a GET mutates NOTHING — asserted on row counts, ids AND updated_at
    timestamps across both tables, not merely on the response body
  * `by_source` counts distinct SKILLS, not evidence rows (Prompt 3.3
    writes up to four rows per skill per repository)
  * `strongest_evidence_confidence` is max(), pinned explicitly against
    a mean or a sum so nobody "improves" it into a score
  * rejected skills are counted but never listed
  * two identical requests return identical JSON

Evidence is written directly rather than through the resume/GitHub
workers: this endpoint reads committed rows and has no knowledge of who
wrote them, which is exactly the separation being tested.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.main import app
from app.models.candidate_skill import CandidateSkill
from app.models.resume import Resume
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.rate_limit import _request_log
from app.settings import get_settings
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"
_BASE = "/api/v1/skill-profile"
_SKILLS = "/api/v1/candidate-skills"


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


def _register_and_login(client: TestClient, email: str) -> tuple[str, str]:
    register = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    assert register.status_code == 201
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200
    return login.json()["access_token"], register.json()["id"]


def _new_user(client: TestClient) -> tuple[str, str]:
    return _register_and_login(client, f"{uuid.uuid4()}@example.com")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _profile(client: TestClient, token: str) -> Any:
    response = client.get(_BASE, headers=_headers(token))
    assert response.status_code == 200
    return response.json()


def _entry(payload: Any, skill_name: str) -> Any:
    matches = [row for row in payload["skills"] if row["skill_name"] == skill_name]
    assert len(matches) == 1, f"expected exactly one {skill_name!r} entry"
    return matches[0]


# --------------------------------------------------------------------
# Fixtures that write evidence directly
# --------------------------------------------------------------------


def _add_evidence(
    user_id: str,
    skill_name: str,
    *,
    source_type: str,
    source_identifier: str,
    extraction_method: str,
    confidence: str = "0.90",
    excerpt: str | None = None,
    status: str = "suggested",
) -> None:
    """Create (or reuse) a candidate skill and attach one evidence row —
    the state a worker would have left behind."""

    async def _write(session: AsyncSession) -> None:
        skill = await session.scalar(select(Skill).where(Skill.slug == skill_name.casefold()))
        assert skill is not None, f"{skill_name!r} is not in the seeded taxonomy"

        candidate_skill = await session.scalar(
            select(CandidateSkill).where(
                CandidateSkill.user_id == uuid.UUID(user_id),
                CandidateSkill.skill_id == skill.id,
            )
        )
        if candidate_skill is None:
            candidate_skill = CandidateSkill(
                user_id=uuid.UUID(user_id), skill_id=skill.id, status=status
            )
            session.add(candidate_skill)
            await session.flush()

        session.add(
            SkillEvidence(
                candidate_skill_id=candidate_skill.id,
                source_type=source_type,
                source_identifier=source_identifier,
                excerpt=excerpt,
                extraction_method=extraction_method,
                confidence=Decimal(confidence),
            )
        )
        await session.commit()

    _run(_write)


def _add_resume(user_id: str, filename: str) -> str:
    """A succeeded resume row, so its id resolves to a filename."""

    async def _write(session: AsyncSession) -> str:
        resume = Resume(
            id=uuid.uuid4(),
            user_id=uuid.UUID(user_id),
            storage_key=f"{user_id}/{uuid.uuid4()}.pdf",
            original_filename=filename,
            content_type="application/pdf",
            file_size_bytes=1024,
            status="succeeded",
            extracted_text="Languages: Python",
            processed_at=datetime.now(UTC),
        )
        session.add(resume)
        await session.commit()
        return str(resume.id)

    return _run(_write)


def _set_status(user_id: str, skill_name: str, status: str) -> None:
    async def _write(session: AsyncSession) -> None:
        skill = await session.scalar(select(Skill).where(Skill.slug == skill_name.casefold()))
        assert skill is not None
        candidate_skill = await session.scalar(
            select(CandidateSkill).where(
                CandidateSkill.user_id == uuid.UUID(user_id),
                CandidateSkill.skill_id == skill.id,
            )
        )
        assert candidate_skill is not None
        candidate_skill.status = status
        await session.commit()

    _run(_write)


def _db_snapshot() -> set[tuple]:
    """Every candidate skill and evidence row, with ids AND timestamps —
    the strict form of "this endpoint writes nothing"."""

    async def _read(session: AsyncSession) -> set[tuple]:
        rows: set[tuple] = set()
        for cs in (await session.scalars(select(CandidateSkill))).all():
            rows.add((cs.id, cs.user_id, cs.skill_id, cs.status, cs.created_at, cs.updated_at))
        for ev in (await session.scalars(select(SkillEvidence))).all():
            rows.add(
                (
                    ev.id,
                    ev.candidate_skill_id,
                    ev.source_type,
                    ev.source_identifier,
                    ev.extraction_method,
                    ev.excerpt,
                    ev.confidence,
                    ev.created_at,
                    ev.updated_at,
                )
            )
        return rows

    return _run(_read)


def _row_counts() -> tuple[int, int, int, int]:
    async def _read(session: AsyncSession) -> tuple[int, int, int, int]:
        return (
            (await session.scalar(select(func.count()).select_from(CandidateSkill))) or 0,
            (await session.scalar(select(func.count()).select_from(SkillEvidence))) or 0,
            (await session.scalar(select(func.count()).select_from(Skill))) or 0,
            (await session.scalar(select(func.count()).select_from(Resume))) or 0,
        )

    return _run(_read)


# --------------------------------------------------------------------
# Authentication and ownership
# --------------------------------------------------------------------


def test_requires_authentication(client: TestClient) -> None:
    assert client.get(_BASE).status_code == 401


def test_another_users_skills_never_appear(client: TestClient) -> None:
    _seed_taxonomy()
    _, owner_id = _new_user(client)
    _add_evidence(
        owner_id,
        "Python",
        source_type="github",
        source_identifier="owner/repo",
        extraction_method="github_language_match",
    )

    other_token, _ = _new_user(client)
    payload = _profile(client, other_token)

    assert payload["skills"] == []
    assert payload["summary"]["total"] == 0


def test_two_users_profiles_are_independent(client: TestClient) -> None:
    _seed_taxonomy()
    token_a, user_a = _new_user(client)
    token_b, user_b = _new_user(client)
    _add_evidence(
        user_a,
        "Python",
        source_type="github",
        source_identifier="a/repo",
        extraction_method="github_language_match",
    )
    _add_evidence(
        user_b,
        "Django",
        source_type="github",
        source_identifier="b/repo",
        extraction_method="github_topic_match",
    )

    assert [s["skill_name"] for s in _profile(client, token_a)["skills"]] == ["Python"]
    assert [s["skill_name"] for s in _profile(client, token_b)["skills"]] == ["Django"]


# --------------------------------------------------------------------
# Empty state
# --------------------------------------------------------------------


def test_empty_profile_is_a_200_with_zeroed_summary(client: TestClient) -> None:
    """Never a 404: "you have no skills yet" is a normal state, and the
    UI should not need an error branch for a new account."""
    _seed_taxonomy()
    token, _ = _new_user(client)

    payload = _profile(client, token)

    assert payload["skills"] == []
    assert payload["summary"] == {
        "total": 0,
        "confirmed": 0,
        "suggested": 0,
        "rejected": 0,
        "by_source": {"resume": 0, "github": 0, "manual": 0},
        "multi_source": 0,
        # Nothing is awaiting review when there is nothing at all.
        "reviewed": True,
    }


def test_every_source_key_is_present_even_at_zero(client: TestClient) -> None:
    """A client should never have to distinguish "contributed nothing"
    from "key missing"."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="owner/repo",
        extraction_method="github_language_match",
    )

    by_source = _profile(client, token)["summary"]["by_source"]

    assert set(by_source) == {"resume", "github", "manual"}
    assert by_source["resume"] == 0
    assert by_source["manual"] == 0


# --------------------------------------------------------------------
# Per-source and combination
# --------------------------------------------------------------------


def test_resume_only_skill(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _add_resume(user_id, "backend-engineer.pdf")
    _add_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
        excerpt="Languages: Python",
    )

    payload = _profile(client, token)
    entry = _entry(payload, "Python")

    assert entry["sources"] == ["resume"]
    assert entry["evidence_count"] == 1
    assert payload["summary"]["by_source"] == {"resume": 1, "github": 0, "manual": 0}


def test_github_only_skill(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Docker",
        source_type="github",
        source_identifier="ada/scheduler",
        extraction_method="github_topic_match",
        excerpt="docker",
    )

    payload = _profile(client, token)

    assert _entry(payload, "Docker")["sources"] == ["github"]
    assert payload["summary"]["by_source"] == {"resume": 0, "github": 1, "manual": 0}


def test_manual_only_skill(client: TestClient) -> None:
    _seed_taxonomy()
    token, _ = _new_user(client)
    assert client.post(_SKILLS, headers=_headers(token), json={"name": "Kubernetes"}).status_code

    payload = _profile(client, token)
    entry = _entry(payload, "Kubernetes")

    assert entry["sources"] == ["manual"]
    assert entry["status"] == "confirmed"
    assert payload["summary"]["by_source"] == {"resume": 0, "github": 0, "manual": 1}


def test_one_skill_from_two_sources_is_one_entry_marked_multi_source(
    client: TestClient,
) -> None:
    """The most useful thing this endpoint reports: a skill both a resume
    and a repository attest to is corroborated in a way neither alone
    is — but it is still ONE skill, not two."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _add_resume(user_id, "cv.pdf")
    _add_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
        excerpt="Languages: Python",
    )
    _add_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="ada/toolkit",
        extraction_method="github_language_match",
    )

    payload = _profile(client, token)
    entry = _entry(payload, "Python")

    assert len(payload["skills"]) == 1
    assert entry["sources"] == ["github", "resume"]
    assert entry["evidence_count"] == 2
    assert payload["summary"]["multi_source"] == 1
    assert payload["summary"]["by_source"] == {"resume": 1, "github": 1, "manual": 0}


def test_by_source_counts_skills_not_evidence_rows(client: TestClient) -> None:
    """Prompt 3.3 writes up to FOUR evidence rows for one skill in one
    repository (README, description, topic, language). Counting rows
    would make GitHub look four times more informative than it is,
    purely as an artefact of how 3.3 decomposes signals."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    for method, excerpt in (
        ("github_readme_match", "Written in Python."),
        ("github_description_match", "A Python toolkit."),
        ("github_topic_match", "python"),
        ("github_language_match", None),
    ):
        _add_evidence(
            user_id,
            "Python",
            source_type="github",
            source_identifier="ada/toolkit",
            extraction_method=method,
            excerpt=excerpt,
        )

    payload = _profile(client, token)

    assert _entry(payload, "Python")["evidence_count"] == 4
    assert payload["summary"]["by_source"]["github"] == 1
    assert payload["summary"]["multi_source"] == 0


# --------------------------------------------------------------------
# Status interaction
# --------------------------------------------------------------------


def test_confirmed_and_suggested_are_listed(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="a/one",
        extraction_method="github_language_match",
        status="suggested",
    )
    _add_evidence(
        user_id,
        "Django",
        source_type="github",
        source_identifier="a/two",
        extraction_method="github_topic_match",
        status="suggested",
    )
    _set_status(user_id, "Django", "confirmed")

    payload = _profile(client, token)

    assert {s["skill_name"] for s in payload["skills"]} == {"Python", "Django"}
    assert payload["summary"]["confirmed"] == 1
    assert payload["summary"]["suggested"] == 1
    assert payload["summary"]["total"] == 2


def test_rejected_is_counted_but_never_listed(client: TestClient) -> None:
    """A rejection is a tombstone meaning "this is not mine", so listing
    it in a PROFILE would contradict the user's own decision. Counting it
    keeps the tombstone visible rather than silently dropping data."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="a/one",
        extraction_method="github_language_match",
    )
    _add_evidence(
        user_id,
        "Django",
        source_type="github",
        source_identifier="a/two",
        extraction_method="github_topic_match",
    )
    _set_status(user_id, "Django", "rejected")

    payload = _profile(client, token)

    assert [s["skill_name"] for s in payload["skills"]] == ["Python"]
    assert payload["summary"]["rejected"] == 1
    # `total` is confirmed + suggested — rejected is reported separately.
    assert payload["summary"]["total"] == 1


def test_a_rejected_skill_does_not_contribute_to_by_source(client: TestClient) -> None:
    """A source cannot be said to "support" a skill the user disowned."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="a/one",
        extraction_method="github_language_match",
    )
    _set_status(user_id, "Python", "rejected")

    summary = _profile(client, token)["summary"]

    assert summary["by_source"] == {"resume": 0, "github": 0, "manual": 0}
    assert summary["multi_source"] == 0


def test_reviewed_flips_when_nothing_is_left_suggested(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="a/one",
        extraction_method="github_language_match",
        status="suggested",
    )
    assert _profile(client, token)["summary"]["reviewed"] is False

    _set_status(user_id, "Python", "confirmed")

    assert _profile(client, token)["summary"]["reviewed"] is True


# --------------------------------------------------------------------
# Confidence — a selection, never a score
# --------------------------------------------------------------------


def test_strongest_evidence_confidence_is_max_not_a_blend(client: TestClient) -> None:
    """0.90, 0.75 and 0.60 -> 0.90. Explicitly NOT the mean (0.75) and
    NOT the sum (2.25). Blending would invent the ranking model Prompt
    4.x owns; this is a selection of one already-stored value."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    for method, confidence in (
        ("github_readme_match", "0.90"),
        ("github_topic_match", "0.75"),
        ("github_description_match", "0.60"),
    ):
        _add_evidence(
            user_id,
            "Python",
            source_type="github",
            source_identifier="ada/toolkit",
            extraction_method=method,
            confidence=confidence,
        )

    entry = _entry(_profile(client, token), "Python")

    assert entry["strongest_evidence_confidence"] == 0.90
    assert entry["evidence_count"] == 3


def test_a_confirmed_skill_with_no_evidence_reports_zero_confidence(
    client: TestClient,
) -> None:
    """Reachable and intended: a confirmed skill survives losing every
    piece of evidence (GitHub disconnected, resume deleted) because the
    user asserted it. It must not crash or be dropped."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="a/one",
        extraction_method="github_language_match",
    )
    _set_status(user_id, "Python", "confirmed")

    async def _drop_evidence(session: AsyncSession) -> None:
        await session.execute(SkillEvidence.__table__.delete())
        await session.commit()

    _run(_drop_evidence)

    entry = _entry(_profile(client, token), "Python")

    assert entry["evidence_count"] == 0
    assert entry["sources"] == []
    assert entry["strongest_evidence_confidence"] == 0.0


# --------------------------------------------------------------------
# Provenance labels
# --------------------------------------------------------------------


def test_resume_evidence_is_labelled_with_the_filename(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _add_resume(user_id, "backend-engineer.pdf")
    _add_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
        excerpt="Languages: Python",
    )

    evidence = _entry(_profile(client, token), "Python")["evidence"][0]

    assert evidence["source_label"] == "backend-engineer.pdf"
    assert evidence["source_identifier"] == resume_id


def test_github_evidence_is_labelled_with_the_repository(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Docker",
        source_type="github",
        source_identifier="ada/scheduler",
        extraction_method="github_topic_match",
        excerpt="docker",
    )

    evidence = _entry(_profile(client, token), "Docker")["evidence"][0]

    assert evidence["source_label"] == "ada/scheduler"


def test_manual_evidence_has_no_label(client: TestClient) -> None:
    """The identifier is the user's own id; echoing "you asserted this
    because you are you" adds nothing the source type has not said."""
    _seed_taxonomy()
    token, _ = _new_user(client)
    client.post(_SKILLS, headers=_headers(token), json={"name": "Kubernetes"})

    evidence = _entry(_profile(client, token), "Kubernetes")["evidence"][0]

    assert evidence["source_label"] is None


def test_a_deleted_resume_leaves_a_null_label_not_an_error(client: TestClient) -> None:
    """`source_identifier` is a polymorphic string with no FK, so a
    dangling reference is an expected state (docs/decisions.md), not a
    failure. The evidence stays as the historical record it is."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=str(uuid.uuid4()),  # never existed
        extraction_method="resume_alias_match",
        excerpt="Languages: Python",
    )

    evidence = _entry(_profile(client, token), "Python")["evidence"][0]

    assert evidence["source_label"] is None
    assert evidence["excerpt"] == "Languages: Python"


def test_another_users_resume_never_resolves_a_label(client: TestClient) -> None:
    """The label lookup is user-scoped, so even a source_identifier
    pointing at somebody else's resume fails closed."""
    _seed_taxonomy()
    _, other_id = _new_user(client)
    other_resume = _add_resume(other_id, "not-yours.pdf")

    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=other_resume,
        extraction_method="resume_alias_match",
    )

    evidence = _entry(_profile(client, token), "Python")["evidence"][0]

    assert evidence["source_label"] is None


# --------------------------------------------------------------------
# Determinism and the read-only guarantee
# --------------------------------------------------------------------


def test_two_identical_requests_return_identical_json(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _add_resume(user_id, "cv.pdf")
    for name, source, identifier, method in (
        ("Python", "resume", resume_id, "resume_alias_match"),
        ("Python", "github", "ada/one", "github_language_match"),
        ("Docker", "github", "ada/two", "github_topic_match"),
        ("Django", "github", "ada/three", "github_readme_match"),
    ):
        _add_evidence(
            user_id,
            name,
            source_type=source,
            source_identifier=identifier,
            extraction_method=method,
        )

    first = client.get(_BASE, headers=_headers(token))
    second = client.get(_BASE, headers=_headers(token))

    assert first.json() == second.json()


def test_skills_are_ordered_by_name(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    for name in ("Redis", "Docker", "Python"):
        _add_evidence(
            user_id,
            name,
            source_type="github",
            source_identifier=f"ada/{name.lower()}",
            extraction_method="github_topic_match",
        )

    names = [s["skill_name"] for s in _profile(client, token)["skills"]]

    assert names == sorted(names)


def test_a_get_mutates_nothing(client: TestClient) -> None:
    """THE read-only guarantee, asserted rather than assumed — on row
    counts across four tables AND on every id and updated_at timestamp.
    A count-only check would miss an UPDATE that touched a row in place.
    """
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _add_resume(user_id, "cv.pdf")
    _add_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
        excerpt="Languages: Python",
    )
    _add_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="ada/toolkit",
        extraction_method="github_language_match",
    )
    _add_evidence(
        user_id,
        "Django",
        source_type="github",
        source_identifier="ada/web",
        extraction_method="github_topic_match",
    )
    _set_status(user_id, "Django", "rejected")

    counts_before = _row_counts()
    snapshot_before = _db_snapshot()

    for _ in range(3):
        assert client.get(_BASE, headers=_headers(token)).status_code == 200

    assert _row_counts() == counts_before
    assert _db_snapshot() == snapshot_before


def test_a_get_creates_no_taxonomy_rows(client: TestClient) -> None:
    _seed_taxonomy()
    token, user_id = _new_user(client)
    _add_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="ada/toolkit",
        extraction_method="github_language_match",
    )
    _, _, skills_before, _ = _row_counts()

    _profile(client, token)

    _, _, skills_after, _ = _row_counts()
    assert skills_after == skills_before


# --------------------------------------------------------------------
# Consistency with the review endpoint
# --------------------------------------------------------------------


def test_the_profile_and_review_endpoints_agree_on_evidence(client: TestClient) -> None:
    """Both read the same rows with the same ordering, so a skill's
    evidence must be identical through either — otherwise the review
    surface and the profile could tell a user two different stories."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _add_resume(user_id, "cv.pdf")
    _add_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
        excerpt="Languages: Python",
    )
    _add_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="ada/toolkit",
        extraction_method="github_language_match",
    )

    from_profile = _entry(_profile(client, token), "Python")["evidence"]
    review = client.get(_SKILLS, headers=_headers(token))
    assert review.status_code == 200
    from_review = next(s for s in review.json() if s["skill_name"] == "Python")["evidence"]

    assert from_profile == from_review
