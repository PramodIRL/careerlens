"""Focused tests for pgvector retrieval (app/embeddings/retrieval.py).

WHAT THESE CAN AND CANNOT PROVE. The only provider is the deterministic
mock, whose vectors are measurably indistinguishable from random — two
texts about the same subject are no closer than two unrelated ones (see
its docstring). So nothing here asserts that "Kubernetes" retrieves
"container orchestration"; that claim is unverifiable until a real
provider exists, and pretending otherwise would be a test that passes
for the wrong reason.

What IS provable, and is what these cover: that the SQL ranks by cosine
distance in the right DIRECTION, that ownership and model filtering hold,
that hydration does not N+1, and that results are stable. Ordering is
asserted against cosine similarities computed independently in Python
from the same deterministic vectors — so the test knows the right answer
without trusting the query that produced it.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.embeddings.content import EmbeddingDocument
from app.embeddings.provider import MockEmbeddingProvider
from app.embeddings.retrieval import find_similar, find_similar_evidence
from app.embeddings.store import upsert_embeddings
from app.models.candidate_skill import CandidateSkill
from app.models.embedding import EMBEDDING_DIMENSION, Embedding
from app.models.saved_job import SavedJob
from app.models.skill import Skill
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.schemas.embedding import EmbeddingSourceType
from app.schemas.skill import EvidenceSourceType, ExtractionMethod, SkillCategory
from app.settings import get_settings
from scripts.backfill_embeddings import backfill_embeddings
from tests.conftest import _SEARCH_PATH_CONNECT_ARGS

_MODEL = "mock-deterministic-v1"
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


def _cosine(a: list[float], b: list[float]) -> float:
    """Both provider outputs are unit length, so the dot product IS the
    cosine similarity. Computed here in Python so the expected ordering
    is derived independently of the SQL under test."""
    return sum(x * y for x, y in zip(a, b, strict=True))


async def _user(db: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash"))
    await db.commit()
    return user_id


async def _evidence_row(db: AsyncSession, user_id: uuid.UUID, excerpt: str) -> SkillEvidence:
    """A candidate skill plus one evidence row, so a hit has something
    real to be traced back to."""
    skill = Skill(
        id=uuid.uuid4(),
        name=f"Skill {uuid.uuid4()}",
        slug=str(uuid.uuid4()),
        category=SkillCategory.LANGUAGE.value,
    )
    candidate_skill = CandidateSkill(id=uuid.uuid4(), user_id=user_id, skill_id=skill.id)
    evidence = SkillEvidence(
        id=uuid.uuid4(),
        candidate_skill_id=candidate_skill.id,
        source_type=EvidenceSourceType.RESUME.value,
        source_identifier=str(uuid.uuid4()),
        excerpt=excerpt,
        extraction_method=ExtractionMethod.RESUME_ALIAS_MATCH.value,
        confidence=1,
    )
    # Flushed in dependency order: these models declare no ORM
    # relationships, so SQLAlchemy has no basis to order the INSERTs
    # itself and would trip the skill_id foreign key.
    db.add(skill)
    await db.flush()
    db.add(candidate_skill)
    await db.flush()
    db.add(evidence)
    await db.commit()
    return evidence


async def _store(
    db: AsyncSession,
    user_id: uuid.UUID,
    texts: dict[str, str],
    *,
    model_identifier: str = _MODEL,
) -> None:
    """Embed {source_id: text} for one user under one model."""
    await upsert_embeddings(
        db,
        _provider(model_identifier),
        [
            EmbeddingDocument(
                user_id=user_id,
                source_type=_EVIDENCE,
                source_id=source_id,
                chunk_index=0,
                text=text,
            )
            for source_id, text in texts.items()
        ],
    )
    await db.commit()


# --- ordering ----------------------------------------------------------


@pytest.mark.anyio
async def test_results_are_ordered_by_cosine_similarity_descending(db: AsyncSession) -> None:
    """The load-bearing test. `<=>` is a DISTANCE, so the query orders
    ascending by it — getting that backwards returns the LEAST relevant
    rows in a plausible-looking order. Expected ordering is computed in
    Python from the same deterministic vectors."""
    user_id = await _user(db)
    texts = {str(uuid.uuid4()): f"evidence text number {index}" for index in range(6)}
    await _store(db, user_id, texts)

    query_text = "a job requirement about backend systems"
    query_vector = _provider().embed_text_sync(query_text)

    expected = [
        source_id
        for source_id, _ in sorted(
            (
                (source_id, _cosine(query_vector, _provider().embed_text_sync(text)))
                for source_id, text in texts.items()
            ),
            key=lambda pair: -pair[1],
        )
    ]

    hits = await find_similar(
        db, user_id=user_id, query_vector=query_vector, model_identifier=_MODEL, limit=6
    )

    assert [hit.source_id for hit in hits] == expected
    assert all(hits[i].similarity >= hits[i + 1].similarity for i in range(len(hits) - 1)), (
        "similarity must decrease down the list"
    )


@pytest.mark.anyio
async def test_an_exact_text_match_ranks_first_with_similarity_one(db: AsyncSession) -> None:
    """The one relationship the mock genuinely encodes: identical text
    gives an identical vector. Nearest-first must therefore put it at
    the top with similarity 1.0."""
    user_id = await _user(db)
    target = str(uuid.uuid4())
    texts = {target: "Built a REST API with FastAPI"}
    texts.update({str(uuid.uuid4()): f"unrelated evidence {i}" for i in range(4)})
    await _store(db, user_id, texts)

    hits = await find_similar(
        db,
        user_id=user_id,
        query_vector=_provider().embed_text_sync("Built a REST API with FastAPI"),
        model_identifier=_MODEL,
        limit=5,
    )

    assert hits[0].source_id == target
    assert hits[0].similarity == pytest.approx(1.0, abs=1e-6)


@pytest.mark.anyio
async def test_limit_and_determinism(db: AsyncSession) -> None:
    """Same inputs, same output — twice, byte for byte."""
    user_id = await _user(db)
    await _store(db, user_id, {str(uuid.uuid4()): f"evidence {i}" for i in range(8)})
    query_vector = _provider().embed_text_sync("query")

    first = await find_similar(
        db, user_id=user_id, query_vector=query_vector, model_identifier=_MODEL, limit=3
    )
    second = await find_similar(
        db, user_id=user_id, query_vector=query_vector, model_identifier=_MODEL, limit=3
    )

    assert len(first) == 3
    assert first == second


# --- filtering ---------------------------------------------------------


@pytest.mark.anyio
async def test_retrieval_never_crosses_users(db: AsyncSession) -> None:
    """Both users store the SAME text, so the other user's row is an
    equally good match by distance — only the ownership filter keeps it
    out. A weaker fixture would pass even if the filter were missing."""
    owner_id = await _user(db)
    other_id = await _user(db)
    shared_text = "Built Kubernetes-based microservices"
    owner_source = str(uuid.uuid4())
    other_source = str(uuid.uuid4())

    await _store(db, owner_id, {owner_source: shared_text})
    await _store(db, other_id, {other_source: shared_text})

    hits = await find_similar(
        db,
        user_id=owner_id,
        query_vector=_provider().embed_text_sync(shared_text),
        model_identifier=_MODEL,
        limit=10,
    )

    assert [hit.source_id for hit in hits] == [owner_source]


@pytest.mark.anyio
async def test_a_different_model_is_never_returned(db: AsyncSession) -> None:
    """Two models occupy different vector spaces, so a distance between
    them is meaningless rather than weak."""
    user_id = await _user(db)
    source_id = str(uuid.uuid4())
    await _store(db, user_id, {source_id: "shared text"}, model_identifier="mock-deterministic-v2")

    hits = await find_similar(
        db,
        user_id=user_id,
        query_vector=_provider().embed_text_sync("shared text"),
        model_identifier=_MODEL,
        limit=10,
    )

    assert hits == []


@pytest.mark.anyio
async def test_min_similarity_filters_when_supplied(db: AsyncSession) -> None:
    """No threshold is built in; one supplied by a caller is honoured.
    1.0 admits only the exact-text match."""
    user_id = await _user(db)
    target = str(uuid.uuid4())
    texts = {target: "exact"}
    texts.update({str(uuid.uuid4()): f"other {i}" for i in range(4)})
    await _store(db, user_id, texts)

    hits = await find_similar(
        db,
        user_id=user_id,
        query_vector=_provider().embed_text_sync("exact"),
        model_identifier=_MODEL,
        limit=10,
        min_similarity=0.999,
    )

    assert [hit.source_id for hit in hits] == [target]


# --- evidence traceability and query count -----------------------------


@pytest.mark.anyio
async def test_evidence_hits_carry_metadata_and_stay_traceable(db: AsyncSession) -> None:
    """Everything the UI would need to explain a hit — and nothing that
    could be mistaken for a claim about the candidate."""
    user_id = await _user(db)
    evidence = await _evidence_row(db, user_id, "Built Kubernetes-based microservices")
    await _store(db, user_id, {str(evidence.id): "Built Kubernetes-based microservices"})

    results = await find_similar_evidence(
        db,
        user_id=user_id,
        query_vector=_provider().embed_text_sync("Built Kubernetes-based microservices"),
        model_identifier=_MODEL,
        limit=5,
    )

    assert len(results) == 1
    result = results[0]
    assert result.evidence_id == evidence.id
    assert result.excerpt == "Built Kubernetes-based microservices"
    assert result.evidence_source_type == EvidenceSourceType.RESUME.value
    assert result.evidence_source_identifier == evidence.source_identifier
    assert result.hit.model_identifier == _MODEL
    assert result.hit.source_type == _EVIDENCE
    assert result.hit.similarity == pytest.approx(1.0, abs=1e-6)
    # A hit must not be expressible as a claim that a skill is satisfied.
    assert not hasattr(result.hit, "skill_id")
    assert not hasattr(result.hit, "satisfied")


@pytest.mark.anyio
async def test_evidence_hydration_does_not_n_plus_one(db: AsyncSession) -> None:
    """Two SELECTs regardless of how many hits come back: one to rank,
    one to hydrate. Counting statements is what makes this a real
    assertion rather than a comment."""
    user_id = await _user(db)
    stored: dict[str, str] = {}
    for index in range(6):
        evidence = await _evidence_row(db, user_id, f"evidence excerpt {index}")
        stored[str(evidence.id)] = f"evidence excerpt {index}"
    await _store(db, user_id, stored)

    statements: list[str] = []
    # AsyncSession.get_bind() already hands back the underlying SYNC
    # Engine, which is what carries the DBAPI-level event hooks.
    sync_engine = db.get_bind()

    def _record(conn: object, cursor: object, statement: str, *args: object) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _record)
    try:
        results = await find_similar_evidence(
            db,
            user_id=user_id,
            query_vector=_provider().embed_text_sync("evidence excerpt 3"),
            model_identifier=_MODEL,
            limit=6,
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)

    assert len(results) == 6
    assert len(statements) == 2, f"expected 2 SELECTs, got {len(statements)}: {statements}"


@pytest.mark.anyio
async def test_a_hit_whose_evidence_is_gone_is_dropped(db: AsyncSession) -> None:
    """`source_id` has no foreign key behind it, so a dangling reference
    is possible. It must not surface as a hit with no excerpt to show."""
    user_id = await _user(db)
    await _store(db, user_id, {str(uuid.uuid4()): "orphaned"})

    results = await find_similar_evidence(
        db,
        user_id=user_id,
        query_vector=_provider().embed_text_sync("orphaned"),
        model_identifier=_MODEL,
        limit=5,
    )

    assert results == []
    # The embedding itself is untouched — retrieval reads, never writes.
    assert await db.scalar(select(Embedding.id).where(Embedding.user_id == user_id)) is not None


# --- the backfill writer -----------------------------------------------


@pytest.mark.anyio
async def test_backfill_writes_embeddings_and_is_idempotent(db: AsyncSession) -> None:
    """The script is thin glue over tested 5.1 helpers, but glue that is
    never run with data is untested. One test covering the two things
    that matter: it writes something, and a second run recomputes
    nothing."""
    user_id = await _user(db)
    evidence = await _evidence_row(db, user_id, "Built Kubernetes-based microservices")
    db.add(
        SavedJob(
            id=uuid.uuid4(),
            user_id=user_id,
            company="Example Ltd",
            title="Platform Engineer",
            description="Experience with container orchestration is required.",
        )
    )
    await db.commit()

    first = await backfill_embeddings(db, _provider())
    second = await backfill_embeddings(db, _provider())

    assert first.computed >= 2, "expected at least the evidence excerpt and one job chunk"
    assert first.written == first.computed
    assert (second.computed, second.written) == (0, 0)
    assert second.skipped == first.computed

    # The evidence document is addressable by its evidence id, which is
    # what makes a retrieved hit traceable back to a stored row.
    stored = await db.scalars(
        select(Embedding.source_id).where(
            Embedding.user_id == user_id, Embedding.source_type == _EVIDENCE
        )
    )
    assert str(evidence.id) in set(stored.all())
