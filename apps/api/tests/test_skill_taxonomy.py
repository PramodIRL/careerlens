"""Schema-level tests for the Prompt 2.3 skill taxonomy and evidence
model: the constraints and cascade behavior that the rest of the system
will rely on.

These are deliberately database-level rather than API-level — Prompt 2.3
adds no endpoints, and the guarantees being tested here (an alias
resolves to exactly one skill, a candidate cannot hold the same skill
twice, evidence dies with its candidate skill, confidence cannot leave
0..1) are properties of the schema itself. Prompt 2.4 will layer
extraction on top and should not have to re-prove any of them.

Async tests via anyio, matching tests/test_local_storage.py — unlike
tests/test_extraction.py there is no Celery task calling `asyncio.run`
internally here, so a running event loop is fine.
"""

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.candidate_skill import CandidateSkill
from app.models.skill import Skill, SkillAlias, SkillRelation
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.schemas.skill import EvidenceSourceType, ExtractionMethod, SkillCategory
from app.settings import get_settings
from tests.conftest import TEST_SCHEMA

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}


@pytest.fixture
def anyio_backend() -> str:
    # Pin to asyncio, matching tests/test_local_storage.py.
    return "asyncio"


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """A session bound to the isolated test schema, with its engine
    disposed inside the same event loop that created it (see
    tests/conftest.py on why that matters)."""
    engine = create_async_engine(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _add_user(db: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", hashed_password="not-a-real-hash"
    )
    db.add(user)
    await db.commit()
    return user


async def _add_skill(db: AsyncSession, name: str, category: SkillCategory | None = None) -> Skill:
    skill = Skill(name=name, slug=name.casefold(), category=category.value if category else None)
    db.add(skill)
    await db.commit()
    return skill


async def _add_candidate_skill(db: AsyncSession, user: User, skill: Skill) -> CandidateSkill:
    candidate_skill = CandidateSkill(user_id=user.id, skill_id=skill.id)
    db.add(candidate_skill)
    await db.commit()
    return candidate_skill


def _evidence(
    candidate_skill: CandidateSkill,
    *,
    source_type: EvidenceSourceType = EvidenceSourceType.MANUAL,
    source_identifier: str = "seed-identifier",
    confidence: str = "1.00",
    excerpt: str | None = None,
) -> SkillEvidence:
    return SkillEvidence(
        candidate_skill_id=candidate_skill.id,
        source_type=source_type.value,
        source_identifier=source_identifier,
        excerpt=excerpt,
        extraction_method=ExtractionMethod.MANUAL_ENTRY.value,
        confidence=Decimal(confidence),
    )


# --- skills / aliases / relations -------------------------------------


@pytest.mark.anyio
async def test_two_skills_cannot_share_a_slug(db: AsyncSession) -> None:
    await _add_skill(db, "Python", SkillCategory.LANGUAGE)

    db.add(Skill(name="PYTHON", slug="python", category=SkillCategory.LANGUAGE.value))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.anyio
async def test_category_may_be_null_for_a_user_coined_skill(db: AsyncSession) -> None:
    """A skill coined through a profile's target skills has no taxonomy
    category, and must not be forced to invent one."""
    skill = await _add_skill(db, "Elixir")

    assert skill.category is None


@pytest.mark.anyio
async def test_an_alias_cannot_be_claimed_by_two_different_skills(db: AsyncSession) -> None:
    """The guarantee a JSON/array column could not have enforced: one
    alias resolves to exactly one canonical skill, globally."""
    javascript = await _add_skill(db, "JavaScript", SkillCategory.LANGUAGE)
    java = await _add_skill(db, "Java", SkillCategory.LANGUAGE)
    db.add(SkillAlias(skill_id=javascript.id, alias="js", alias_slug="js"))
    await db.commit()

    db.add(SkillAlias(skill_id=java.id, alias="JS", alias_slug="js"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.anyio
async def test_a_skill_cannot_be_related_to_itself(db: AsyncSession) -> None:
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)

    db.add(SkillRelation(from_skill_id=python.id, to_skill_id=python.id))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.anyio
async def test_deleting_a_skill_cascades_to_its_aliases_and_relations(db: AsyncSession) -> None:
    docker = await _add_skill(db, "Docker", SkillCategory.INFRASTRUCTURE)
    kubernetes = await _add_skill(db, "Kubernetes", SkillCategory.INFRASTRUCTURE)
    db.add(SkillAlias(skill_id=kubernetes.id, alias="k8s", alias_slug="k8s"))
    db.add(SkillRelation(from_skill_id=docker.id, to_skill_id=kubernetes.id))
    db.add(SkillRelation(from_skill_id=kubernetes.id, to_skill_id=docker.id))
    await db.commit()

    await db.delete(kubernetes)
    await db.commit()

    assert (await db.scalars(select(SkillAlias))).all() == []
    assert (await db.scalars(select(SkillRelation))).all() == []


# --- candidate skills -------------------------------------------------


@pytest.mark.anyio
async def test_a_candidate_cannot_hold_the_same_skill_twice(db: AsyncSession) -> None:
    user = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    await _add_candidate_skill(db, user, python)

    db.add(CandidateSkill(user_id=user.id, skill_id=python.id))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.anyio
async def test_two_candidates_may_hold_the_same_skill(db: AsyncSession) -> None:
    alice = await _add_user(db)
    bob = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)

    await _add_candidate_skill(db, alice, python)
    await _add_candidate_skill(db, bob, python)

    assert len((await db.scalars(select(CandidateSkill))).all()) == 2


@pytest.mark.anyio
async def test_a_skill_referenced_by_a_candidate_cannot_be_deleted(db: AsyncSession) -> None:
    """RESTRICT, not CASCADE: shared canonical vocabulary must not
    disappear out from under the candidates who reference it."""
    user = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    await _add_candidate_skill(db, user, python)

    await db.delete(python)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


# --- evidence ---------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("bad_confidence", ["-0.01", "1.01", "2.00"])
async def test_confidence_outside_zero_to_one_is_rejected(
    db: AsyncSession, bad_confidence: str
) -> None:
    user = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    candidate_skill = await _add_candidate_skill(db, user, python)

    db.add(_evidence(candidate_skill, confidence=bad_confidence))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.anyio
@pytest.mark.parametrize("good_confidence", ["0.00", "0.50", "1.00"])
async def test_confidence_within_zero_to_one_is_accepted(
    db: AsyncSession, good_confidence: str
) -> None:
    user = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    candidate_skill = await _add_candidate_skill(db, user, python)

    db.add(_evidence(candidate_skill, confidence=good_confidence))
    await db.commit()

    stored = (await db.scalars(select(SkillEvidence))).one()
    assert stored.confidence == Decimal(good_confidence)


@pytest.mark.anyio
async def test_one_candidate_skill_supports_evidence_from_several_sources(
    db: AsyncSession,
) -> None:
    """The core "multiple evidence records for one candidate skill"
    requirement: resume, GitHub and manual evidence coexist because the
    natural key is scoped per source."""
    user = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    candidate_skill = await _add_candidate_skill(db, user, python)

    db.add(
        _evidence(
            candidate_skill,
            source_type=EvidenceSourceType.RESUME,
            source_identifier=str(uuid.uuid4()),
            excerpt="Built a REST API with FastAPI and PostgreSQL",
            confidence="0.80",
        )
    )
    db.add(
        _evidence(
            candidate_skill,
            source_type=EvidenceSourceType.GITHUB,
            source_identifier="PramodIRL/careerlens",
            confidence="0.60",
        )
    )
    db.add(
        _evidence(
            candidate_skill,
            source_type=EvidenceSourceType.MANUAL,
            source_identifier=str(user.id),
            confidence="1.00",
        )
    )
    await db.commit()

    rows = (await db.scalars(select(SkillEvidence))).all()
    assert len(rows) == 3
    assert {row.source_type for row in rows} == {"resume", "github", "manual"}


@pytest.mark.anyio
async def test_duplicate_evidence_for_the_same_source_and_method_is_rejected(
    db: AsyncSession,
) -> None:
    """The idempotency key that lets Prompt 2.4 re-run extraction over
    the same resume without accumulating duplicates."""
    user = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    candidate_skill = await _add_candidate_skill(db, user, python)
    resume_id = str(uuid.uuid4())
    db.add(
        _evidence(
            candidate_skill,
            source_type=EvidenceSourceType.RESUME,
            source_identifier=resume_id,
            excerpt="first mention of Python",
        )
    )
    await db.commit()

    # A different excerpt is still the same (skill, source, method) —
    # one representative excerpt per source is the documented contract.
    db.add(
        _evidence(
            candidate_skill,
            source_type=EvidenceSourceType.RESUME,
            source_identifier=resume_id,
            excerpt="second mention of Python",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


@pytest.mark.anyio
async def test_the_same_skill_from_two_different_resumes_is_two_evidence_rows(
    db: AsyncSession,
) -> None:
    user = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    candidate_skill = await _add_candidate_skill(db, user, python)

    for _ in range(2):
        db.add(
            _evidence(
                candidate_skill,
                source_type=EvidenceSourceType.RESUME,
                source_identifier=str(uuid.uuid4()),
            )
        )
    await db.commit()

    assert len((await db.scalars(select(SkillEvidence))).all()) == 2


@pytest.mark.anyio
async def test_excerpt_may_be_null(db: AsyncSession) -> None:
    """Evidence with nothing quotable (a GitHub language statistic, a
    manual assertion) must not be forced to invent an excerpt."""
    user = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    candidate_skill = await _add_candidate_skill(db, user, python)

    db.add(_evidence(candidate_skill, excerpt=None))
    await db.commit()

    assert (await db.scalars(select(SkillEvidence))).one().excerpt is None


# --- ownership cascades -----------------------------------------------


@pytest.mark.anyio
async def test_deleting_a_candidate_skill_deletes_its_evidence(db: AsyncSession) -> None:
    """ "A user can correct or remove extracted skills" (project brief),
    expressed without any status column: removing the claim removes the
    evidence supporting it."""
    user = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    candidate_skill = await _add_candidate_skill(db, user, python)
    db.add(_evidence(candidate_skill))
    await db.commit()

    await db.delete(candidate_skill)
    await db.commit()

    assert (await db.scalars(select(SkillEvidence))).all() == []


@pytest.mark.anyio
async def test_deleting_a_user_removes_their_candidate_skills_and_evidence(
    db: AsyncSession,
) -> None:
    """Ownership is derived, so a single user delete must cascade two
    levels down — evidence has no user_id of its own to clean up."""
    user = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    candidate_skill = await _add_candidate_skill(db, user, python)
    db.add(_evidence(candidate_skill))
    await db.commit()

    await db.delete(user)
    await db.commit()

    assert (await db.scalars(select(CandidateSkill))).all() == []
    assert (await db.scalars(select(SkillEvidence))).all() == []
    # The canonical skill is shared vocabulary and must survive.
    assert len((await db.scalars(select(Skill))).all()) == 1


@pytest.mark.anyio
async def test_evidence_owner_is_reachable_only_through_its_candidate_skill(
    db: AsyncSession,
) -> None:
    """Documents the derived-ownership contract later prompts must use:
    there is no user_id on skill_evidence, so an ownership check joins
    back to candidate_skills."""
    alice = await _add_user(db)
    python = await _add_skill(db, "Python", SkillCategory.LANGUAGE)
    candidate_skill = await _add_candidate_skill(db, alice, python)
    db.add(_evidence(candidate_skill))
    await db.commit()

    owner_id = await db.scalar(
        select(CandidateSkill.user_id).join(
            SkillEvidence, SkillEvidence.candidate_skill_id == CandidateSkill.id
        )
    )

    assert owner_id == alice.id
    assert not hasattr(SkillEvidence, "user_id")
