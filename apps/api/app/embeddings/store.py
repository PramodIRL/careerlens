"""Persisting embeddings, and deciding when not to compute one.

Splits from app/embeddings/content.py the same way
app/skill_extraction.py splits from app/skill_matching.py: that module
decides WHAT text is worth embedding, this one decides what to WRITE and
— more importantly — what not to recompute.

THE POINT OF THIS MODULE IS THE SKIP. An embedding is the expensive part
of any future semantic feature: a real provider is a network call, per
document, billed. So every write here first asks whether the stored
`content_hash` already matches, and returns without ever awaiting the
provider when it does. `EmbeddingSyncSummary.skipped` is what a test
asserts on, and a spy provider counting its own calls is what proves the
provider was genuinely not consulted rather than merely not written.

OWNERSHIP IS ENFORCED BY SIGNATURE. There is no read function here that
does not take `user_id`, and every one of them filters on it. Combined
with `user_id` being part of the natural key
(app/models/embedding.py), addressing another user's row is not
something a caller can do by forgetting a clause.

NO SIMILARITY, NO RETRIEVAL. This module computes no distance, orders by
no vector, and has no nearest-neighbour query. Nothing in `skill_match_v1`
or `skill_gap_v1` calls it, and no endpoint exposes it.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings.content import EmbeddingDocument
from app.embeddings.hashing import content_hash, normalize_text
from app.embeddings.provider import EmbeddingProvider
from app.models.embedding import EMBEDDING_DIMENSION, Embedding


class EmbeddingDimensionError(ValueError):
    """A provider returned a vector the column cannot hold.

    Its own type so a caller can tell a misconfiguration apart from a
    bad argument. Raised BEFORE the insert, so the failure names the
    provider and the two dimensions rather than surfacing as a driver
    type error from inside PostgreSQL.
    """


@dataclass
class EmbeddingSyncSummary:
    """Per-run counts, returned for logging and asserted on in tests.

    A re-run over unchanged content must report `computed=0` and
    `skipped` equal to the number of documents — the same shape, and the
    same purpose, as `RequirementSummary` in
    app/job_requirements/extract.py.
    """

    computed: int = 0
    skipped: int = 0
    written: int = 0


def _validate_dimension(provider: EmbeddingProvider, vector: list[float]) -> None:
    if len(vector) != EMBEDDING_DIMENSION:
        raise EmbeddingDimensionError(
            f"provider {provider.model_identifier!r} returned a vector of length "
            f"{len(vector)}, but the embeddings column holds {EMBEDDING_DIMENSION}"
        )


async def _stored_hash(
    db: AsyncSession, document: EmbeddingDocument, model_identifier: str
) -> str | None:
    """The `content_hash` already stored for this document's natural key,
    or None if there is no row yet."""
    # `scalars(...).first()` rather than `scalar(...)`: AsyncSession.scalar
    # is typed as returning Any, which would silently erase the return
    # type under mypy strict. At most one row can match — the arguments
    # are the full natural key.
    result = await db.scalars(
        select(Embedding.content_hash).where(
            Embedding.user_id == document.user_id,
            Embedding.source_type == document.source_type,
            Embedding.source_id == document.source_id,
            Embedding.chunk_index == document.chunk_index,
            Embedding.model_identifier == model_identifier,
        )
    )
    return result.first()


async def _write(
    db: AsyncSession,
    document: EmbeddingDocument,
    *,
    model_identifier: str,
    digest: str,
    vector: list[float],
) -> bool:
    """Upsert one row on the owner/source/chunk/model key. Returns
    whether anything was actually written.

    `on_conflict_do_update` rather than a plain insert because two
    concurrent runs over the same source would otherwise race into a
    unique violation; the `where` clause keeps a genuinely-unchanged row
    from having its `updated_at` bumped, the same way
    app/job_requirements/extract.py's `_write_requirement` does.
    """
    values = {
        "id": uuid.uuid4(),
        "user_id": document.user_id,
        "source_type": document.source_type,
        "source_id": document.source_id,
        "chunk_index": document.chunk_index,
        "char_start": document.char_start,
        "char_end": document.char_end,
        "content_hash": digest,
        "model_identifier": model_identifier,
        "embedding": vector,
    }
    statement = pg_insert(Embedding).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="uq_embeddings_owner_source_chunk_model",
        set_={
            "char_start": statement.excluded.char_start,
            "char_end": statement.excluded.char_end,
            "content_hash": statement.excluded.content_hash,
            "embedding": statement.excluded.embedding,
            "updated_at": func.now(),
        },
        where=Embedding.content_hash.is_distinct_from(statement.excluded.content_hash),
    )
    # An INSERT always yields a CursorResult at runtime (it has
    # .rowcount); AsyncSession.execute() is only typed as Result[Any].
    result = cast("CursorResult[Any]", await db.execute(statement))
    return bool(result.rowcount)


async def upsert_embeddings(
    db: AsyncSession,
    provider: EmbeddingProvider,
    documents: Sequence[EmbeddingDocument],
) -> EmbeddingSyncSummary:
    """Embed and store every document that is not already current.

    Does NOT commit — the caller owns the transaction, the same contract
    as `extract_job_requirements`. That is what would let a future caller
    save a source row and its embeddings in one commit.

    Per document: normalize, hash, compare against what is stored. A
    matching hash means the source has not changed under this model, so
    the provider is never awaited. A differing hash — or no row at all —
    means embed and upsert.

    A model change is not handled here as a special case because it does
    not need to be: `model_identifier` is part of the key, so a document
    embedded under a new model simply finds no stored hash and is
    computed, leaving the previous model's row untouched beside it.
    """
    summary = EmbeddingSyncSummary()
    for document in documents:
        digest = content_hash(document.text)
        if await _stored_hash(db, document, provider.model_identifier) == digest:
            summary.skipped += 1
            continue

        vector = await provider.embed_text(normalize_text(document.text))
        _validate_dimension(provider, vector)
        summary.computed += 1
        if await _write(
            db,
            document,
            model_identifier=provider.model_identifier,
            digest=digest,
            vector=vector,
        ):
            summary.written += 1
    return summary


async def get_embedding(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    source_type: str,
    source_id: str,
    model_identifier: str,
    chunk_index: int = 0,
) -> Embedding | None:
    """One embedding, addressed by its full natural key.

    `user_id` is required, not optional. A row belonging to someone else
    returns None — indistinguishable from one that does not exist, which
    is the correct answer to give a caller who has no business knowing
    the difference.
    """
    result = await db.scalars(
        select(Embedding).where(
            Embedding.user_id == user_id,
            Embedding.source_type == source_type,
            Embedding.source_id == source_id,
            Embedding.chunk_index == chunk_index,
            Embedding.model_identifier == model_identifier,
        )
    )
    return result.first()


async def list_embeddings_for_source(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    source_type: str,
    source_id: str,
    model_identifier: str,
) -> list[Embedding]:
    """Every chunk stored for one source, in chunk order.

    Ordered by `chunk_index` so the result reads back in the order the
    source text was cut, not in whatever order PostgreSQL returns rows.
    Scoped by `user_id` for the same reason `get_embedding` is.
    """
    result = await db.scalars(
        select(Embedding)
        .where(
            Embedding.user_id == user_id,
            Embedding.source_type == source_type,
            Embedding.source_id == source_id,
            Embedding.model_identifier == model_identifier,
        )
        .order_by(Embedding.chunk_index)
    )
    return list(result)
