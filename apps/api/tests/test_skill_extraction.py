"""Integration tests for persisting resume skill extraction
(app/skill_extraction.py).

Where tests/test_skill_matching.py pins down *what the text says*, these
pin down *what gets written* — and above all the invariant the override
design rests on: an automatic re-run must never undo a decision a person
made. The load-bearing tests here are the byte-identical rerun and the
three override interactions (confirm / reject / manual add).

Runs against the real seeded taxonomy rather than a hand-built one, so
these also catch a seed change that would break extraction.
"""

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.candidate_skill import CandidateSkill
from app.models.resume import Resume
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.schemas.resume import ResumeStatus
from app.schemas.skill import (
    CandidateSkillStatus,
    EvidenceSourceType,
    ExtractionMethod,
    SkillCategory,
)
from app.settings import get_settings
from app.skill_extraction import extract_skills_for_resume, load_taxonomy_terms
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}

_RESUME_TEXT = """Jane Candidate — Backend Engineer

SKILLS
Languages: Python, SQL, Go
Frameworks: Django, FastAPI
Tools: Docker, Git

EXPERIENCE
Built a REST API with FastAPI and PostgreSQL for a research team.
Containerised the service with Docker and wrote unit tests in pytest.
"""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _seeded_user_and_resume(
    db: AsyncSession, text: str = _RESUME_TEXT
) -> tuple[uuid.UUID, Resume]:
    await seed_skill_taxonomy(db)

    user_id = uuid.uuid4()
    db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash"))
    await db.commit()

    resume = Resume(
        id=uuid.uuid4(),
        user_id=user_id,
        storage_key=f"{user_id}/{uuid.uuid4()}.pdf",
        original_filename="resume.pdf",
        content_type="application/pdf",
        file_size_bytes=len(text),
        status=ResumeStatus.SUCCEEDED.value,
        extracted_text=text,
    )
    db.add(resume)
    await db.commit()
    # Return the id, not the User: tests call db.expire_all() to force a
    # re-read, which would strand a detached ORM object.
    return user_id, resume


async def _skill_named(db: AsyncSession, name: str) -> Skill:
    skill = await db.scalar(select(Skill).where(Skill.slug == name.casefold()))
    assert skill is not None, f"expected {name!r} in the seeded taxonomy"
    return skill


async def _candidate_skill_for(
    db: AsyncSession, user_id: uuid.UUID, name: str
) -> CandidateSkill | None:
    skill = await _skill_named(db, name)
    return await db.scalar(
        select(CandidateSkill).where(
            CandidateSkill.user_id == user_id, CandidateSkill.skill_id == skill.id
        )
    )


async def _names_for(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    rows = (
        await db.scalars(
            select(Skill.name)
            .join(CandidateSkill, CandidateSkill.skill_id == Skill.id)
            .where(CandidateSkill.user_id == user_id)
        )
    ).all()
    return set(rows)


async def _snapshot(db: AsyncSession, user_id: uuid.UUID) -> set[tuple]:
    db.expire_all()
    candidate_skills = (
        await db.scalars(select(CandidateSkill).where(CandidateSkill.user_id == user_id))
    ).all()
    rows: set[tuple] = {
        (cs.id, cs.skill_id, cs.status, cs.created_at, cs.updated_at) for cs in candidate_skills
    }
    evidence = (
        await db.scalars(
            select(SkillEvidence).where(
                SkillEvidence.candidate_skill_id.in_([cs.id for cs in candidate_skills])
            )
        )
    ).all()
    rows |= {
        (e.id, e.candidate_skill_id, e.excerpt, e.confidence, e.created_at, e.updated_at)
        for e in evidence
    }
    return rows


# --- the happy path ---------------------------------------------------


@pytest.mark.anyio
async def test_extraction_creates_suggested_skills_with_evidence(db: AsyncSession) -> None:
    user_id, resume = await _seeded_user_and_resume(db)

    summary = await extract_skills_for_resume(db, resume.id)

    names = await _names_for(db, user_id)
    assert {"Python", "SQL", "Django", "FastAPI", "Docker", "Git", "pytest"} <= names
    assert summary.candidate_skills_created == summary.skills_matched

    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    assert python.status == CandidateSkillStatus.SUGGESTED.value

    evidence = (
        await db.scalars(select(SkillEvidence).where(SkillEvidence.candidate_skill_id == python.id))
    ).all()
    assert len(evidence) == 1
    assert evidence[0].source_type == EvidenceSourceType.RESUME.value
    assert evidence[0].source_identifier == str(resume.id)
    assert evidence[0].extraction_method == ExtractionMethod.RESUME_ALIAS_MATCH.value
    assert evidence[0].confidence == Decimal("0.90")
    assert evidence[0].excerpt is not None
    assert "Python" in evidence[0].excerpt


@pytest.mark.anyio
async def test_an_ambiguous_term_is_matched_only_from_the_skills_list(db: AsyncSession) -> None:
    """ "Go" appears in the SKILLS list of the fixture resume, so it is
    matched — but at the lowest confidence."""
    user_id, resume = await _seeded_user_and_resume(db)

    await extract_skills_for_resume(db, resume.id)

    go = await _candidate_skill_for(db, user_id, "Go")
    assert go is not None
    evidence = (
        await db.scalars(select(SkillEvidence).where(SkillEvidence.candidate_skill_id == go.id))
    ).one()
    assert evidence.confidence == Decimal("0.60")


@pytest.mark.anyio
async def test_a_skill_absent_from_the_resume_is_not_created(db: AsyncSession) -> None:
    user_id, resume = await _seeded_user_and_resume(db)

    await extract_skills_for_resume(db, resume.id)

    assert "Kubernetes" not in await _names_for(db, user_id)


@pytest.mark.anyio
async def test_extraction_is_skipped_for_a_resume_without_text(db: AsyncSession) -> None:
    user_id, resume = await _seeded_user_and_resume(db, text="Python everywhere")
    resume.status = ResumeStatus.FAILED.value
    resume.extracted_text = None
    await db.commit()

    summary = await extract_skills_for_resume(db, resume.id)

    assert summary.skills_matched == 0
    assert await _names_for(db, user_id) == set()


@pytest.mark.anyio
async def test_only_curated_skills_are_matchable(db: AsyncSession) -> None:
    """A skill coined through Prompt 1.3's target skills has no category
    and must never start matching against other people's resumes."""
    user_id, resume = await _seeded_user_and_resume(db, text="Skills: Elixir, Python")
    db.add(Skill(name="Elixir", slug="elixir", category=None))
    await db.commit()

    terms = await load_taxonomy_terms(db)
    assert all(term.term.casefold() != "elixir" for term in terms)

    await extract_skills_for_resume(db, resume.id)
    assert "Elixir" not in await _names_for(db, user_id)


# --- idempotency ------------------------------------------------------


@pytest.mark.anyio
async def test_rerunning_extraction_changes_absolutely_nothing(db: AsyncSession) -> None:
    """The idempotency guarantee, asserted strictly: same ids, same
    timestamps — proving the evidence upsert's WHERE clause suppresses
    no-op writes rather than merely avoiding duplicates."""
    user_id, resume = await _seeded_user_and_resume(db)
    resume_id = resume.id  # _snapshot expires the session; hold a plain id
    await extract_skills_for_resume(db, resume_id)
    before = await _snapshot(db, user_id)

    summary = await extract_skills_for_resume(db, resume_id)

    assert summary.candidate_skills_created == 0
    assert summary.evidence_written == 0
    assert summary.evidence_removed == 0
    assert summary.suggestions_removed == 0
    assert await _snapshot(db, user_id) == before


@pytest.mark.anyio
async def test_a_second_resume_adds_evidence_without_duplicating_the_skill(
    db: AsyncSession,
) -> None:
    user_id, first = await _seeded_user_and_resume(db)
    await extract_skills_for_resume(db, first.id)
    second = Resume(
        id=uuid.uuid4(),
        user_id=user_id,
        storage_key=f"{user_id}/{uuid.uuid4()}.pdf",
        original_filename="resume-2.pdf",
        content_type="application/pdf",
        file_size_bytes=42,
        status=ResumeStatus.SUCCEEDED.value,
        extracted_text="Skills: Python, Redis",
    )
    db.add(second)
    await db.commit()

    await extract_skills_for_resume(db, second.id)

    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    evidence = (
        await db.scalars(select(SkillEvidence).where(SkillEvidence.candidate_skill_id == python.id))
    ).all()
    assert len(evidence) == 2
    assert {e.source_identifier for e in evidence} == {str(first.id), str(second.id)}


# --- reconciliation ---------------------------------------------------


@pytest.mark.anyio
async def test_a_suggestion_the_resume_no_longer_supports_is_removed(db: AsyncSession) -> None:
    """The taxonomy is editable, so a suggestion must not outlive the
    evidence that justified it."""
    user_id, resume = await _seeded_user_and_resume(db)
    await extract_skills_for_resume(db, resume.id)
    assert "Redis" not in await _names_for(db, user_id)

    resume.extracted_text = "Skills: Redis"
    await db.commit()
    summary = await extract_skills_for_resume(db, resume.id)

    names = await _names_for(db, user_id)
    assert names == {"Redis"}
    assert summary.suggestions_removed > 0


@pytest.mark.anyio
async def test_reconciliation_never_removes_a_confirmed_skill(db: AsyncSession) -> None:
    user_id, resume = await _seeded_user_and_resume(db)
    await extract_skills_for_resume(db, resume.id)
    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    python.status = CandidateSkillStatus.CONFIRMED.value
    await db.commit()

    resume.extracted_text = "Skills: Redis"
    await db.commit()
    await extract_skills_for_resume(db, resume.id)

    survivor = await _candidate_skill_for(db, user_id, "Python")
    assert survivor is not None
    assert survivor.status == CandidateSkillStatus.CONFIRMED.value


# --- the override invariants ------------------------------------------


@pytest.mark.anyio
async def test_a_rerun_does_not_downgrade_a_confirmed_skill(db: AsyncSession) -> None:
    user_id, resume = await _seeded_user_and_resume(db)
    await extract_skills_for_resume(db, resume.id)
    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    python.status = CandidateSkillStatus.CONFIRMED.value
    await db.commit()

    await extract_skills_for_resume(db, resume.id)

    db.expire_all()
    still = await _candidate_skill_for(db, user_id, "Python")
    assert still is not None
    assert still.status == CandidateSkillStatus.CONFIRMED.value


@pytest.mark.anyio
async def test_a_rerun_does_not_resurrect_a_rejected_skill(db: AsyncSession) -> None:
    """The single most important test in this file. If rejection were a
    delete, this rerun would recreate the skill as a fresh suggestion
    and the user's decision would vanish."""
    user_id, resume = await _seeded_user_and_resume(db)
    await extract_skills_for_resume(db, resume.id)
    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    python.status = CandidateSkillStatus.REJECTED.value
    await db.commit()

    await extract_skills_for_resume(db, resume.id)

    db.expire_all()
    still = await _candidate_skill_for(db, user_id, "Python")
    assert still is not None
    assert still.status == CandidateSkillStatus.REJECTED.value
    assert still.id == python.id  # the same tombstone, not a new row


@pytest.mark.anyio
async def test_a_manually_added_skill_absent_from_the_resume_survives_a_rerun(
    db: AsyncSession,
) -> None:
    user_id, resume = await _seeded_user_and_resume(db)
    kubernetes = await _skill_named(db, "Kubernetes")
    manual = CandidateSkill(
        user_id=user_id,
        skill_id=kubernetes.id,
        status=CandidateSkillStatus.CONFIRMED.value,
    )
    db.add(manual)
    await db.flush()
    db.add(
        SkillEvidence(
            candidate_skill_id=manual.id,
            source_type=EvidenceSourceType.MANUAL.value,
            source_identifier=str(user_id),
            excerpt=None,
            extraction_method=ExtractionMethod.MANUAL_ENTRY.value,
            confidence=Decimal("1.00"),
        )
    )
    await db.commit()

    await extract_skills_for_resume(db, resume.id)

    db.expire_all()
    survivor = await _candidate_skill_for(db, user_id, "Kubernetes")
    assert survivor is not None
    assert survivor.status == CandidateSkillStatus.CONFIRMED.value


@pytest.mark.anyio
async def test_manual_evidence_is_never_removed_by_reconciliation(db: AsyncSession) -> None:
    """Reconciliation is scoped to this resume's own alias-match
    evidence; another source's evidence is not its business."""
    user_id, resume = await _seeded_user_and_resume(db)
    await extract_skills_for_resume(db, resume.id)
    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    db.add(
        SkillEvidence(
            candidate_skill_id=python.id,
            source_type=EvidenceSourceType.MANUAL.value,
            source_identifier=str(user_id),
            excerpt=None,
            extraction_method=ExtractionMethod.MANUAL_ENTRY.value,
            confidence=Decimal("1.00"),
        )
    )
    await db.commit()

    resume.extracted_text = "Skills: Redis"
    await db.commit()
    await extract_skills_for_resume(db, resume.id)

    remaining = (
        await db.scalars(select(SkillEvidence).where(SkillEvidence.candidate_skill_id == python.id))
    ).all()
    assert [e.source_type for e in remaining] == [EvidenceSourceType.MANUAL.value]


@pytest.mark.anyio
async def test_extraction_only_ever_writes_for_the_resumes_own_owner(db: AsyncSession) -> None:
    user_id, resume = await _seeded_user_and_resume(db)
    other = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", hashed_password="not-a-real-hash"
    )
    db.add(other)
    await db.commit()

    await extract_skills_for_resume(db, resume.id)

    owners = set((await db.scalars(select(CandidateSkill.user_id))).all())
    assert owners == {user_id}


@pytest.mark.anyio
async def test_seeded_categories_are_carried_through_to_candidate_skills(
    db: AsyncSession,
) -> None:
    user_id, resume = await _seeded_user_and_resume(db)

    await extract_skills_for_resume(db, resume.id)

    python = await _skill_named(db, "Python")
    assert python.category == SkillCategory.LANGUAGE.value
