"""Integration tests for pgvector-backed embedding storage
(app/embeddings/store.py).

Where tests/test_embeddings.py pins down what gets embedded, these pin
down what gets WRITTEN — and above all the invariant the whole design
exists for: unchanged content must not cost a provider call. That is
asserted with a counting provider rather than by inspecting rows,
because "did not write" and "did not compute" are different claims and
only the second one saves anything on a real provider.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.embeddings.content import EmbeddingDocument
from app.embeddings.hashing import content_hash
from app.embeddings.provider import MOCK_MODEL_IDENTIFIER, MockEmbeddingProvider
from app.embeddings.store import (
    EmbeddingDimensionError,
    get_embedding,
    list_embeddings_for_source,
    upsert_embeddings,
)
from app.models.embedding import EMBEDDING_DIMENSION, Embedding
from app.models.user import User
from app.schemas.embedding import EmbeddingSourceType
from app.settings import get_settings
from tests.conftest import _SEARCH_PATH_CONNECT_ARGS

_SOURCE_TYPE = EmbeddingSourceType.SAVED_JOB_DESCRIPTION.value


class CountingProvider:
    """A mock that records how many times it was actually consulted.

    Delegates the arithmetic to MockEmbeddingProvider so these tests
    exercise the real vector, not a second fake with its own behaviour.
    """

    def __init__(
        self,
        *,
        dimension: int = EMBEDDING_DIMENSION,
        model_identifier: str = MOCK_MODEL_IDENTIFIER,
    ) -> None:
        self._inner = MockEmbeddingProvider(dimension=dimension, model_identifier=model_identifier)
        self.calls = 0

    @property
    def model_identifier(self) -> str:
        return self._inner.model_identifier

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    async def embed_text(self, text: str) -> list[float]:
        self.calls += 1
        return self._inner.embed_text_sync(text)


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


async def _user(db: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash"))
    await db.commit()
    return user_id


def _document(
    user_id: uuid.UUID,
    text: str = "We are hiring a backend engineer who writes Python.",
    *,
    source_id: str | None = None,
    chunk_index: int = 0,
    char_start: int | None = 0,
    char_end: int | None = 51,
) -> EmbeddingDocument:
    return EmbeddingDocument(
        user_id=user_id,
        source_type=_SOURCE_TYPE,
        source_id=source_id or str(uuid.uuid4()),
        chunk_index=chunk_index,
        text=text,
        char_start=char_start,
        char_end=char_end,
    )


async def _row_count(db: AsyncSession) -> int:
    return (await db.scalar(select(func.count()).select_from(Embedding))) or 0


# --- deduplication: the reason this module exists ---------------------


@pytest.mark.anyio
async def test_unchanged_content_is_never_recomputed(db: AsyncSession) -> None:
    """Same source + same content + same model -> the provider is not
    called a second time, and no duplicate row appears."""
    user_id = await _user(db)
    document = _document(user_id)
    provider = CountingProvider()

    first = await upsert_embeddings(db, provider, [document])
    await db.commit()
    second = await upsert_embeddings(db, provider, [document])
    await db.commit()

    assert (first.computed, first.written, first.skipped) == (1, 1, 0)
    assert (second.computed, second.written, second.skipped) == (0, 0, 1)
    assert provider.calls == 1, "the provider must not be consulted for unchanged content"
    assert await _row_count(db) == 1


@pytest.mark.anyio
async def test_changed_content_replaces_the_vector_in_place(db: AsyncSession) -> None:
    """A new hash means a new embedding — but the SAME row. Current
    state, not history: a superseded vector has no reader."""
    user_id = await _user(db)
    source_id = str(uuid.uuid4())
    provider = CountingProvider()

    await upsert_embeddings(db, provider, [_document(user_id, "Python", source_id=source_id)])
    await db.commit()
    original = await get_embedding(
        db,
        user_id=user_id,
        source_type=_SOURCE_TYPE,
        source_id=source_id,
        model_identifier=provider.model_identifier,
    )
    assert original is not None
    original_id, original_vector = original.id, list(original.embedding)

    summary = await upsert_embeddings(
        db, provider, [_document(user_id, "PostgreSQL", source_id=source_id)]
    )
    await db.commit()
    db.expire_all()

    updated = await get_embedding(
        db,
        user_id=user_id,
        source_type=_SOURCE_TYPE,
        source_id=source_id,
        model_identifier=provider.model_identifier,
    )
    assert updated is not None
    assert (summary.computed, summary.skipped) == (1, 0)
    assert provider.calls == 2
    assert await _row_count(db) == 1
    assert updated.id == original_id, "changed content updates in place"
    assert updated.content_hash == content_hash("PostgreSQL")
    assert list(updated.embedding) != original_vector


@pytest.mark.anyio
async def test_a_new_model_writes_a_sibling_row(db: AsyncSession) -> None:
    """Two models' vectors are not comparable, so a model change must
    not overwrite — the previous model's row stays addressable under its
    own identifier."""
    user_id = await _user(db)
    source_id = str(uuid.uuid4())
    document = _document(user_id, "Python", source_id=source_id)

    old_provider = CountingProvider(model_identifier="mock-deterministic-v1")
    new_provider = CountingProvider(model_identifier="mock-deterministic-v2")

    await upsert_embeddings(db, old_provider, [document])
    await db.commit()
    summary = await upsert_embeddings(db, new_provider, [document])
    await db.commit()

    assert (summary.computed, summary.skipped) == (1, 0), "a model change forces recomputation"
    assert await _row_count(db) == 2

    for provider in (old_provider, new_provider):
        stored = await get_embedding(
            db,
            user_id=user_id,
            source_type=_SOURCE_TYPE,
            source_id=source_id,
            model_identifier=provider.model_identifier,
        )
        assert stored is not None
        assert stored.model_identifier == provider.model_identifier
        # Same content, so the hashes agree; the vectors must not.
        assert stored.content_hash == content_hash("Python")

    old_row = await get_embedding(
        db,
        user_id=user_id,
        source_type=_SOURCE_TYPE,
        source_id=source_id,
        model_identifier="mock-deterministic-v1",
    )
    new_row = await get_embedding(
        db,
        user_id=user_id,
        source_type=_SOURCE_TYPE,
        source_id=source_id,
        model_identifier="mock-deterministic-v2",
    )
    assert old_row is not None and new_row is not None
    assert list(old_row.embedding) != list(new_row.embedding)


# --- metadata and the vector round trip -------------------------------


@pytest.mark.anyio
async def test_metadata_is_persisted(db: AsyncSession) -> None:
    user_id = await _user(db)
    source_id = str(uuid.uuid4())
    provider = CountingProvider()

    await upsert_embeddings(
        db,
        provider,
        [
            _document(
                user_id,
                "Requirements: Python, SQL",
                source_id=source_id,
                chunk_index=3,
                char_start=120,
                char_end=145,
            )
        ],
    )
    await db.commit()

    stored = await get_embedding(
        db,
        user_id=user_id,
        source_type=_SOURCE_TYPE,
        source_id=source_id,
        chunk_index=3,
        model_identifier=provider.model_identifier,
    )

    assert stored is not None
    assert stored.user_id == user_id
    assert stored.source_type == _SOURCE_TYPE
    assert stored.source_id == source_id
    assert stored.chunk_index == 3
    assert stored.char_start == 120
    assert stored.char_end == 145
    assert stored.content_hash == content_hash("Requirements: Python, SQL")
    assert stored.model_identifier == MOCK_MODEL_IDENTIFIER
    assert stored.created_at is not None
    assert stored.updated_at is not None
    # The source text itself is deliberately not a column — only its hash
    # is stored, so no copy of private prose lands in this table.
    assert not hasattr(stored, "text")


@pytest.mark.anyio
async def test_the_vector_round_trips_through_pgvector(db: AsyncSession) -> None:
    """Exact equality, not an epsilon: the provider rounds to float32 so
    what goes into a `vector` column is what comes back out."""
    user_id = await _user(db)
    source_id = str(uuid.uuid4())
    provider = CountingProvider()
    text = "Built a REST API with FastAPI"

    await upsert_embeddings(db, provider, [_document(user_id, text, source_id=source_id)])
    await db.commit()
    db.expire_all()

    stored = await get_embedding(
        db,
        user_id=user_id,
        source_type=_SOURCE_TYPE,
        source_id=source_id,
        model_identifier=provider.model_identifier,
    )

    assert stored is not None
    assert len(stored.embedding) == EMBEDDING_DIMENSION
    assert list(stored.embedding) == MockEmbeddingProvider(
        dimension=EMBEDDING_DIMENSION
    ).embed_text_sync(text)


@pytest.mark.anyio
async def test_chunks_are_stored_and_read_back_in_order(db: AsyncSession) -> None:
    user_id = await _user(db)
    source_id = str(uuid.uuid4())
    provider = CountingProvider()

    documents = [
        _document(user_id, f"chunk {index}", source_id=source_id, chunk_index=index)
        for index in (2, 0, 1)
    ]
    summary = await upsert_embeddings(db, provider, documents)
    await db.commit()

    stored = await list_embeddings_for_source(
        db,
        user_id=user_id,
        source_type=_SOURCE_TYPE,
        source_id=source_id,
        model_identifier=provider.model_identifier,
    )

    assert summary.computed == 3
    assert [row.chunk_index for row in stored] == [0, 1, 2]


# --- dimension validation ---------------------------------------------


@pytest.mark.anyio
async def test_a_wrong_dimension_is_rejected_before_the_insert(db: AsyncSession) -> None:
    """A misconfigured provider fails with a message naming both widths,
    rather than as a driver type error from inside PostgreSQL."""
    user_id = await _user(db)
    provider = CountingProvider(dimension=8)

    with pytest.raises(EmbeddingDimensionError, match="length 8"):
        await upsert_embeddings(db, provider, [_document(user_id)])

    assert await _row_count(db) == 0


# --- ownership ---------------------------------------------------------


@pytest.mark.anyio
async def test_one_user_cannot_read_another_users_embedding(db: AsyncSession) -> None:
    """The owner is part of the natural key, so a row cannot even be
    addressed without naming whose it is. Someone else's row is
    indistinguishable from one that does not exist."""
    owner_id = await _user(db)
    other_id = await _user(db)
    source_id = str(uuid.uuid4())
    provider = CountingProvider()

    await upsert_embeddings(db, provider, [_document(owner_id, source_id=source_id)])
    await db.commit()

    assert (
        await get_embedding(
            db,
            user_id=owner_id,
            source_type=_SOURCE_TYPE,
            source_id=source_id,
            model_identifier=provider.model_identifier,
        )
        is not None
    )
    assert (
        await get_embedding(
            db,
            user_id=other_id,
            source_type=_SOURCE_TYPE,
            source_id=source_id,
            model_identifier=provider.model_identifier,
        )
        is None
    )
    assert (
        await list_embeddings_for_source(
            db,
            user_id=other_id,
            source_type=_SOURCE_TYPE,
            source_id=source_id,
            model_identifier=provider.model_identifier,
        )
        == []
    )


@pytest.mark.anyio
async def test_two_users_saving_the_same_source_get_their_own_rows(db: AsyncSession) -> None:
    """`source_id` is a polymorphic string with no foreign key behind it,
    so nothing stops two users from naming the same one. Including the
    owner in the unique key is what keeps that from silently merging two
    people's data into one row."""
    first_id = await _user(db)
    second_id = await _user(db)
    source_id = str(uuid.uuid4())
    provider = CountingProvider()

    await upsert_embeddings(db, provider, [_document(first_id, source_id=source_id)])
    await upsert_embeddings(db, provider, [_document(second_id, source_id=source_id)])
    await db.commit()

    assert await _row_count(db) == 2


@pytest.mark.anyio
async def test_deleting_a_user_removes_their_embeddings(db: AsyncSession) -> None:
    """ON DELETE CASCADE, like every other user-owned table here."""
    user_id = await _user(db)
    await upsert_embeddings(db, CountingProvider(), [_document(user_id)])
    await db.commit()

    user = await db.get(User, user_id)
    assert user is not None
    await db.delete(user)
    await db.commit()

    assert await _row_count(db) == 0
