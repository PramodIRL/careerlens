"""Semantic fit and the local embedding provider (Prompt 5.2b).

TWO KINDS OF TEST, KEPT APART ON PURPOSE:

  * The formula, retrieval wiring, ownership and the baseline-unchanged
    guarantees use the DETERMINISTIC MOCK. They assert exact numbers and
    run everywhere, with no model and no network.
  * A small group at the bottom uses the REAL model to check the one
    thing a mock cannot express — that related text actually outranks
    unrelated text. Those skip when the model is unavailable (no network
    on a cold CI runner), because a missing download is an environment
    fact, not a defect in this code.

Nothing here asserts that `fit` has a particular *meaning*. The
thresholds behind it are provisional (see app/embeddings/semantic_fit.py)
and validating them is Prompt 5.3's job.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.embeddings.content import EmbeddingDocument
from app.embeddings.provider import (
    LOCAL_MODEL_IDENTIFIER,
    LocalSentenceEmbeddingProvider,
    MockEmbeddingProvider,
)
from app.embeddings.semantic_fit import (
    FORMULA_VERSION,
    SIMILARITY_CEIL,
    SIMILARITY_FLOOR,
    TOP_K,
    compute_semantic_fit,
)
from app.embeddings.store import upsert_embeddings
from app.matching.score import FORMULA_VERSION as MATCH_FORMULA_VERSION
from app.matching.score import RequirementInput, compute_score
from app.models.candidate_skill import CandidateSkill
from app.models.embedding import EMBEDDING_DIMENSION
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.schemas.embedding import EmbeddingSourceType
from app.schemas.skill import EvidenceSourceType, ExtractionMethod, SkillCategory
from app.settings import get_settings
from tests.conftest import _SEARCH_PATH_CONNECT_ARGS

_MODEL = "mock-deterministic-v1"
_JOB = EmbeddingSourceType.SAVED_JOB_DESCRIPTION.value
_EVIDENCE = EmbeddingSourceType.SKILL_EVIDENCE.value


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


def _provider(model_identifier: str = _MODEL) -> MockEmbeddingProvider:
    return MockEmbeddingProvider(dimension=EMBEDDING_DIMENSION, model_identifier=model_identifier)


async def _user(db: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash"))
    await db.commit()
    return user_id


async def _evidence(db: AsyncSession, user_id: uuid.UUID, excerpt: str) -> SkillEvidence:
    skill = Skill(
        id=uuid.uuid4(),
        name=f"Skill {uuid.uuid4()}",
        slug=str(uuid.uuid4()),
        category=SkillCategory.LANGUAGE.value,
    )
    candidate_skill = CandidateSkill(id=uuid.uuid4(), user_id=user_id, skill_id=skill.id)
    row = SkillEvidence(
        id=uuid.uuid4(),
        candidate_skill_id=candidate_skill.id,
        source_type=EvidenceSourceType.RESUME.value,
        source_identifier=str(uuid.uuid4()),
        excerpt=excerpt,
        extraction_method=ExtractionMethod.RESUME_ALIAS_MATCH.value,
        confidence=1,
    )
    db.add(skill)
    await db.flush()
    db.add(candidate_skill)
    await db.flush()
    db.add(row)
    await db.commit()
    return row


async def _store(
    db: AsyncSession,
    user_id: uuid.UUID,
    documents: list[EmbeddingDocument],
    *,
    model_identifier: str = _MODEL,
) -> None:
    await upsert_embeddings(db, _provider(model_identifier), documents)
    await db.commit()


def _job_doc(user_id: uuid.UUID, job_id: uuid.UUID, text: str) -> EmbeddingDocument:
    return EmbeddingDocument(
        user_id=user_id,
        source_type=_JOB,
        source_id=str(job_id),
        chunk_index=0,
        text=text,
        char_start=0,
        char_end=len(text),
    )


def _evidence_doc(user_id: uuid.UUID, evidence_id: uuid.UUID, text: str) -> EmbeddingDocument:
    return EmbeddingDocument(
        user_id=user_id,
        source_type=_EVIDENCE,
        source_id=str(evidence_id),
        chunk_index=0,
        text=text,
    )


# --- the formula, with exact numbers -----------------------------------


@pytest.mark.anyio
async def test_identical_text_is_a_full_strength_fit(db: AsyncSession) -> None:
    """The mock gives identical text an identical vector, so similarity
    is 1.0 — above CEIL, which must clamp to the maximum rather than
    overflow past 20."""
    user_id = await _user(db)
    job_id = uuid.uuid4()
    text = "Experience with container orchestration"
    evidence = await _evidence(db, user_id, text)
    await _store(
        db,
        user_id,
        [_job_doc(user_id, job_id, text), _evidence_doc(user_id, evidence.id, text)],
    )

    result = await compute_semantic_fit(
        db, user_id=user_id, saved_job_id=job_id, model_identifier=_MODEL
    )

    assert result.formula_version == FORMULA_VERSION == "semantic_fit_v1"
    assert result.fit == 20
    assert result.band == "strong"
    assert len(result.hits) == 1
    assert result.hits[0].evidence_id == evidence.id


@pytest.mark.anyio
async def test_no_evidence_above_the_floor_is_none_not_a_low_score(db: AsyncSession) -> None:
    """The mock's unrelated vectors sit near zero, well under FLOOR.
    That reports "nothing relevant found", NOT a judgement that the
    candidate is a poor match."""
    user_id = await _user(db)
    job_id = uuid.uuid4()
    evidence = await _evidence(db, user_id, "Taught piano lessons on weekends")
    await _store(
        db,
        user_id,
        [
            _job_doc(user_id, job_id, "Experience with container orchestration"),
            _evidence_doc(user_id, evidence.id, "Taught piano lessons on weekends"),
        ],
    )

    result = await compute_semantic_fit(
        db, user_id=user_id, saved_job_id=job_id, model_identifier=_MODEL
    )

    assert result.fit == 0
    assert result.band == "none"
    assert result.hits == []
    assert result.considered == 1, "the job WAS considered — there was simply no near evidence"


@pytest.mark.anyio
async def test_a_job_with_no_embeddings_returns_empty_not_an_error(db: AsyncSession) -> None:
    """Backfill has not run for this job yet. "Nothing to compare" is a
    truthful answer; inventing one would not be."""
    user_id = await _user(db)

    result = await compute_semantic_fit(
        db, user_id=user_id, saved_job_id=uuid.uuid4(), model_identifier=_MODEL
    )

    assert (result.fit, result.band, result.hits, result.considered) == (0, "none", [], 0)


@pytest.mark.anyio
async def test_results_are_capped_at_top_k(db: AsyncSession) -> None:
    user_id = await _user(db)
    job_id = uuid.uuid4()
    text = "Experience with container orchestration"
    documents = [_job_doc(user_id, job_id, text)]
    for _ in range(TOP_K + 3):
        evidence = await _evidence(db, user_id, text)
        documents.append(_evidence_doc(user_id, evidence.id, text))
    await _store(db, user_id, documents)

    result = await compute_semantic_fit(
        db, user_id=user_id, saved_job_id=job_id, model_identifier=_MODEL
    )

    assert len(result.hits) == TOP_K


# --- isolation ----------------------------------------------------------


@pytest.mark.anyio
async def test_another_users_evidence_is_never_retrieved(db: AsyncSession) -> None:
    """Both users store the SAME text, so the other user's evidence is an
    equally good match by distance — only ownership keeps it out."""
    owner_id = await _user(db)
    other_id = await _user(db)
    job_id = uuid.uuid4()
    text = "Experience with container orchestration"

    owner_evidence = await _evidence(db, owner_id, text)
    other_evidence = await _evidence(db, other_id, text)
    await _store(
        db,
        owner_id,
        [
            _job_doc(owner_id, job_id, text),
            _evidence_doc(owner_id, owner_evidence.id, text),
            _evidence_doc(other_id, other_evidence.id, text),
        ],
    )

    result = await compute_semantic_fit(
        db, user_id=owner_id, saved_job_id=job_id, model_identifier=_MODEL
    )

    assert [hit.evidence_id for hit in result.hits] == [owner_evidence.id]


@pytest.mark.anyio
async def test_a_different_model_is_never_mixed_in(db: AsyncSession) -> None:
    """Mock and real vectors live in different spaces. Asking under one
    model must not see the other's rows."""
    user_id = await _user(db)
    job_id = uuid.uuid4()
    text = "Experience with container orchestration"
    evidence = await _evidence(db, user_id, text)
    await _store(
        db,
        user_id,
        [_job_doc(user_id, job_id, text), _evidence_doc(user_id, evidence.id, text)],
        model_identifier="mock-deterministic-v2",
    )

    result = await compute_semantic_fit(
        db, user_id=user_id, saved_job_id=job_id, model_identifier=_MODEL
    )

    assert (result.fit, result.considered) == (0, 0)


# --- the guarantees the baseline depends on ----------------------------


def test_the_baseline_score_is_untouched_by_this_slice() -> None:
    """`skill_match_v1` arithmetic and its version string are unchanged.
    Semantic fit is a separate number under a separate version, and this
    pins both so a later edit cannot quietly blend them."""
    requirements = [
        RequirementInput(skill_id="a", level="required", satisfied=True),
        RequirementInput(skill_id="b", level="required", satisfied=False),
        RequirementInput(skill_id="c", level="preferred", satisfied=True),
    ]

    score = compute_score(requirements)

    assert MATCH_FORMULA_VERSION == "skill_match_v1"
    assert score.formula_version == "skill_match_v1"
    # (3 + 2) / 8 = 62.5%, which round() resolves to 62, not 63: Python
    # uses banker's rounding (ties to even). Pinned deliberately — this
    # is the value skill_match_v1 has always produced, and a change to
    # it would be a silent baseline shift.
    assert score.overall_score == 62
    assert score.required_matched == 1
    assert score.required_total == 2
    assert FORMULA_VERSION != MATCH_FORMULA_VERSION


@pytest.mark.anyio
async def test_a_semantic_hit_cannot_express_skill_satisfaction(db: AsyncSession) -> None:
    """Required-skill safety, structurally. A hit carries no skill id and
    no satisfied flag, so no code path can turn "relevant evidence" into
    "has this skill" — the job here is about Python while the evidence is
    about Java, and the hit still says nothing about skills."""
    user_id = await _user(db)
    job_id = uuid.uuid4()
    text = "Python is required for this role"
    evidence = await _evidence(db, user_id, "Built data pipelines using Java")
    await _store(
        db,
        user_id,
        [_job_doc(user_id, job_id, text), _evidence_doc(user_id, evidence.id, text)],
    )

    result = await compute_semantic_fit(
        db, user_id=user_id, saved_job_id=job_id, model_identifier=_MODEL
    )

    for hit in result.hits:
        assert not hasattr(hit.hit, "skill_id")
        assert not hasattr(hit.hit, "satisfied")
        assert not hasattr(hit, "skill_id")
    assert not hasattr(result, "matched_skills")
    assert not hasattr(result, "required_matched")


# --- the real model -----------------------------------------------------


def _real_provider() -> LocalSentenceEmbeddingProvider:
    """Skip rather than fail when the model cannot be loaded.

    A cold CI runner has no Hugging Face cache and (by project decision)
    no network step to populate one, so an unavailable model is an
    environment fact rather than a defect. These run locally.
    """
    provider = LocalSentenceEmbeddingProvider()
    try:
        provider.embed_text_sync("warm")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"local embedding model unavailable: {exc}")
    return provider


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_the_real_model_has_the_column_dimension_and_repeats_exactly() -> None:
    provider = _real_provider()

    first = provider.embed_text_sync("Built Kubernetes-based microservices")
    second = provider.embed_text_sync("Built Kubernetes-based microservices")

    assert provider.model_identifier == LOCAL_MODEL_IDENTIFIER
    assert len(first) == EMBEDDING_DIMENSION == 384
    assert first == second, "same input must give the same vector within a process"
    assert sum(c * c for c in first) == pytest.approx(1.0, abs=1e-4), "unit length"


def test_related_text_outranks_unrelated_text() -> None:
    """The one thing the mock cannot express, and the whole reason a real
    provider was adopted. Deliberately a ranking assertion, not a
    threshold one: the ORDER is the claim this slice makes, while the
    absolute cut-offs are provisional and belong to 5.3."""
    provider = _real_provider()
    job = provider.embed_text_sync("Experience with container orchestration")

    related = [
        provider.embed_text_sync("Built Kubernetes-based microservices"),
        provider.embed_text_sync("Designed CI/CD pipelines with Docker and Helm"),
    ]
    unrelated = [
        provider.embed_text_sync("Wrote marketing copy for a bakery newsletter"),
        provider.embed_text_sync("Taught piano lessons to beginners on weekends"),
    ]

    worst_related = min(_cosine(job, vector) for vector in related)
    best_unrelated = max(_cosine(job, vector) for vector in unrelated)

    assert worst_related > best_unrelated, (
        f"related {worst_related:.4f} must outrank unrelated {best_unrelated:.4f}"
    )
    # And the provisional floor separates them, which is what makes the
    # current constants usable as a starting point rather than correct.
    assert best_unrelated < SIMILARITY_FLOOR < worst_related < SIMILARITY_CEIL
