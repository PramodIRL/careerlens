"""Deleting a resume removes the skill evidence it produced (Prompt 3.4
follow-up).

THE BUG THIS FILE EXISTS TO PREVENT. `skill_evidence.source_identifier`
cites a resume by a polymorphic string, not a foreign key, so deleting a
resume fires no cascade. Its evidence — and any candidate skill resting
solely on it — survived, and the unified skill profile went on citing a
document the user had just deleted.

The fix must be narrow, and most of these tests exist to prove what it
does NOT touch: GitHub evidence, manual evidence, another resume's
evidence, another user's anything, and every confirmed or rejected
decision the user has made.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
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
from app.storage import get_resume_storage
from app.storage.local import LocalResumeStorage
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override
from tests.test_resume import _PDF_BYTES

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}
_PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _use_temp_storage(tmp_path: Path) -> Generator[None, None, None]:
    app.dependency_overrides[get_resume_storage] = lambda: LocalResumeStorage(tmp_path)
    yield
    app.dependency_overrides.pop(get_resume_storage, None)


@pytest.fixture(autouse=True)
def _stub_enqueue_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evidence is written directly below rather than by the worker: this
    is about the DELETE path, and stubbing keeps the tests off a real
    broker (same reasoning as tests/test_resume.py)."""
    monkeypatch.setattr("app.api.v1.resume.enqueue_extraction", lambda resume_id: None)


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
    assert register.status_code == 201
    login = client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200
    return login.json()["access_token"], register.json()["id"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upload(client: TestClient, token: str, filename: str = "resume.pdf") -> str:
    response = client.post(
        "/api/v1/resumes",
        headers=_headers(token),
        files={"file": (filename, _PDF_BYTES, "application/pdf")},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _delete(client: TestClient, token: str, resume_id: str) -> Response:
    return client.delete(f"/api/v1/resumes/{resume_id}", headers=_headers(token))


def _attach_evidence(
    user_id: str,
    skill_name: str,
    *,
    source_type: str,
    source_identifier: str,
    extraction_method: str,
    status: str = "suggested",
    excerpt: str | None = None,
) -> None:
    """The state a worker (or manual add) would have left behind."""

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
                confidence=Decimal("0.90"),
            )
        )
        await session.commit()

    _run(_write)


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


def _skill_names(user_id: str) -> set[str]:
    async def _read(session: AsyncSession) -> set[str]:
        rows = (
            await session.scalars(
                select(Skill.name)
                .join(CandidateSkill, CandidateSkill.skill_id == Skill.id)
                .where(CandidateSkill.user_id == uuid.UUID(user_id))
            )
        ).all()
        return set(rows)

    return _run(_read)


def _evidence(user_id: str) -> list[tuple[str, str]]:
    """(source_type, source_identifier) for every piece of this user's
    evidence, sorted."""

    async def _read(session: AsyncSession) -> list[tuple[str, str]]:
        rows = (
            await session.execute(
                select(SkillEvidence.source_type, SkillEvidence.source_identifier)
                .join(CandidateSkill, CandidateSkill.id == SkillEvidence.candidate_skill_id)
                .where(CandidateSkill.user_id == uuid.UUID(user_id))
            )
        ).all()
        return sorted((source, identifier) for source, identifier in rows)

    return _run(_read)


def _status_of(user_id: str, skill_name: str) -> str | None:
    async def _read(session: AsyncSession) -> str | None:
        skill = await session.scalar(select(Skill).where(Skill.slug == skill_name.casefold()))
        assert skill is not None
        candidate_skill = await session.scalar(
            select(CandidateSkill).where(
                CandidateSkill.user_id == uuid.UUID(user_id),
                CandidateSkill.skill_id == skill.id,
            )
        )
        return candidate_skill.status if candidate_skill else None

    return _run(_read)


# --------------------------------------------------------------------
# A. The bug itself
# --------------------------------------------------------------------


def test_deleting_a_resume_removes_its_evidence_and_the_orphaned_skill(
    client: TestClient,
) -> None:
    """A: resume-only suggested skill. Nothing else supports it, so the
    skill goes with the document — otherwise the profile keeps citing a
    resume the user just deleted."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _upload(client, token)
    _attach_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
        excerpt="Languages: Python",
    )
    assert _skill_names(user_id) == {"Python"}

    assert _delete(client, token, resume_id).status_code == 204

    assert _evidence(user_id) == []
    assert _skill_names(user_id) == set()


# --------------------------------------------------------------------
# B–C. What deletion must NOT touch
# --------------------------------------------------------------------


def test_github_evidence_survives_and_keeps_the_skill(client: TestClient) -> None:
    """B: the skill is still genuinely evidenced, so it stays — only the
    resume's own citation disappears."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _upload(client, token)
    _attach_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
    )
    _attach_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="ada/toolkit",
        extraction_method="github_language_match",
    )

    assert _delete(client, token, resume_id).status_code == 204

    assert _evidence(user_id) == [("github", "ada/toolkit")]
    assert _skill_names(user_id) == {"Python"}
    assert _status_of(user_id, "Python") == "suggested"


def test_manual_evidence_survives_and_keeps_the_skill(client: TestClient) -> None:
    """C: a manual assertion is the user's own word and is never removed
    by a resume deletion."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _upload(client, token)
    _attach_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
    )
    _attach_evidence(
        user_id,
        "Python",
        source_type="manual",
        source_identifier=user_id,
        extraction_method="manual_entry",
    )

    assert _delete(client, token, resume_id).status_code == 204

    assert _evidence(user_id) == [("manual", user_id)]
    assert _skill_names(user_id) == {"Python"}


# --------------------------------------------------------------------
# D–E. Override invariants
# --------------------------------------------------------------------


def test_a_confirmed_skill_survives_losing_its_only_evidence(client: TestClient) -> None:
    """D: the user asserted this. Deleting the document that first
    suggested it must not silently un-confirm it."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _upload(client, token)
    _attach_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
    )
    _set_status(user_id, "Python", "confirmed")

    assert _delete(client, token, resume_id).status_code == 204

    assert _evidence(user_id) == []
    assert _status_of(user_id, "Python") == "confirmed"


def test_a_rejected_skill_keeps_its_tombstone(client: TestClient) -> None:
    """E: rejection is a persistent tombstone. Removing it here would let
    a future extraction run re-suggest the skill, silently discarding the
    user's decision."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _upload(client, token)
    _attach_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
    )
    _set_status(user_id, "Python", "rejected")

    assert _delete(client, token, resume_id).status_code == 204

    assert _evidence(user_id) == []
    assert _status_of(user_id, "Python") == "rejected"


# --------------------------------------------------------------------
# F–H. Scoping
# --------------------------------------------------------------------


def test_another_resumes_evidence_is_untouched(client: TestClient) -> None:
    """F: two resumes, one deleted. Only the deleted one's citation goes."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_a = _upload(client, token, "a.pdf")
    resume_b = _upload(client, token, "b.pdf")
    _attach_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_a,
        extraction_method="resume_alias_match",
    )
    _attach_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_b,
        extraction_method="resume_alias_match",
    )
    _attach_evidence(
        user_id,
        "Django",
        source_type="resume",
        source_identifier=resume_b,
        extraction_method="resume_alias_match",
    )

    assert _delete(client, token, resume_a).status_code == 204

    assert _evidence(user_id) == sorted([("resume", resume_b), ("resume", resume_b)])
    assert _skill_names(user_id) == {"Python", "Django"}


def test_one_users_deletion_never_touches_anothers_evidence(client: TestClient) -> None:
    """G: ownership. The route 403/404s on someone else's resume, and the
    removal is user-scoped as well — defence in depth."""
    _seed_taxonomy()
    token_a, user_a = _new_user(client)
    token_b, user_b = _new_user(client)
    resume_a = _upload(client, token_a, "a.pdf")
    resume_b = _upload(client, token_b, "b.pdf")
    _attach_evidence(
        user_a,
        "Python",
        source_type="resume",
        source_identifier=resume_a,
        extraction_method="resume_alias_match",
    )
    _attach_evidence(
        user_b,
        "Python",
        source_type="resume",
        source_identifier=resume_b,
        extraction_method="resume_alias_match",
    )

    # A cannot delete B's resume at all...
    assert _delete(client, token_a, resume_b).status_code == 403
    assert _evidence(user_b) == [("resume", resume_b)]

    # ...and deleting their own leaves B entirely alone.
    assert _delete(client, token_a, resume_a).status_code == 204
    assert _evidence(user_a) == []
    assert _evidence(user_b) == [("resume", resume_b)]
    assert _skill_names(user_b) == {"Python"}


def test_deleting_a_resume_with_no_evidence_still_works(client: TestClient) -> None:
    """H: the common case — a resume deleted before extraction ran, or
    one that matched nothing."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _upload(client, token)

    assert _delete(client, token, resume_id).status_code == 204

    assert _evidence(user_id) == []
    assert client.get("/api/v1/resumes", headers=_headers(token)).json() == []


def test_existing_deletion_behaviour_is_intact(client: TestClient) -> None:
    """I: the resume row and its stored file still go, 404 afterwards,
    and other resumes are unaffected."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    keep = _upload(client, token, "keep.pdf")
    drop = _upload(client, token, "drop.pdf")

    assert _delete(client, token, drop).status_code == 204

    assert client.get(f"/api/v1/resumes/{drop}", headers=_headers(token)).status_code == 404
    remaining = client.get("/api/v1/resumes", headers=_headers(token)).json()
    assert [row["id"] for row in remaining] == [keep]

    async def _count(session: AsyncSession) -> int:
        return (
            await session.scalar(
                select(func.count()).select_from(Resume).where(Resume.user_id == uuid.UUID(user_id))
            )
        ) or 0

    assert _run(_count) == 1


# --------------------------------------------------------------------
# The profile actually updates
# --------------------------------------------------------------------


def test_the_unified_profile_reflects_the_deletion(client: TestClient) -> None:
    """The user-visible symptom that started this: the profile must stop
    citing the deleted resume, while keeping what GitHub still supports."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _upload(client, token, "backend-engineer.pdf")
    _attach_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
        excerpt="Languages: Python",
    )
    _attach_evidence(
        user_id,
        "Python",
        source_type="github",
        source_identifier="ada/toolkit",
        extraction_method="github_language_match",
    )
    _attach_evidence(
        user_id,
        "Django",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
    )

    before = client.get("/api/v1/skill-profile", headers=_headers(token)).json()
    assert before["summary"]["by_source"] == {"resume": 2, "github": 1, "manual": 0}
    assert before["summary"]["multi_source"] == 1

    assert _delete(client, token, resume_id).status_code == 204

    after = client.get("/api/v1/skill-profile", headers=_headers(token)).json()
    assert after["summary"]["by_source"] == {"resume": 0, "github": 1, "manual": 0}
    assert after["summary"]["multi_source"] == 0
    assert [s["skill_name"] for s in after["skills"]] == ["Python"]
    # No evidence anywhere still names the deleted document.
    labels = [e["source_label"] for s in after["skills"] for e in s["evidence"]]
    assert labels == ["ada/toolkit"]


def test_deletion_is_atomic_with_the_resume_row(client: TestClient) -> None:
    """Resume gone AND evidence gone, or neither. A resume that vanished
    while its evidence survived is precisely the inconsistency being
    fixed, so the two share one transaction."""
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _upload(client, token)
    _attach_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
    )

    assert _delete(client, token, resume_id).status_code == 204

    async def _both(session: AsyncSession) -> tuple[int, int]:
        resumes = (
            await session.scalar(
                select(func.count()).select_from(Resume).where(Resume.id == uuid.UUID(resume_id))
            )
        ) or 0
        evidence = (
            await session.scalar(
                select(func.count())
                .select_from(SkillEvidence)
                .where(SkillEvidence.source_identifier == resume_id)
            )
        ) or 0
        return resumes, evidence

    assert _run(_both) == (0, 0)


def test_a_confirmed_skill_keeps_its_manual_evidence_after_the_resume_goes(
    client: TestClient,
) -> None:
    """The exact combination clarified from browser testing.

    A skill the resume suggested, that the user then confirmed AND
    asserted by hand, must survive its source document being deleted:
    the resume citation disappears, "Added by you" remains, and the
    confirmed decision is untouched. A confirmed skill is allowed to
    stand on the user's own word alone.
    """
    _seed_taxonomy()
    token, user_id = _new_user(client)
    resume_id = _upload(client, token)
    _attach_evidence(
        user_id,
        "Python",
        source_type="resume",
        source_identifier=resume_id,
        extraction_method="resume_alias_match",
        excerpt="Languages: Python",
    )
    _attach_evidence(
        user_id,
        "Python",
        source_type="manual",
        source_identifier=user_id,
        extraction_method="manual_entry",
    )
    _set_status(user_id, "Python", "confirmed")

    assert _delete(client, token, resume_id).status_code == 204

    # Resume citation gone, the user's own assertion intact...
    assert _evidence(user_id) == [("manual", user_id)]
    # ...and the decision is untouched.
    assert _status_of(user_id, "Python") == "confirmed"
