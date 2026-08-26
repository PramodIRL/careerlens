"""pgvector-backed embedding storage (embedding infrastructure slice)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.embeddings.vector_type import Vector

# THE ONE DIMENSION THIS PROJECT USES. 384 — the width of
# `all-MiniLM-L6-v2`, the most plausible first real provider, chosen so
# the column does not have to be rebuilt the day a real model arrives.
#
# This constant is a SCHEMA fact and must stay equal to the literal in
# the create-embeddings-table migration; changing it is a migration, not
# a configuration change. `Settings.embedding_dimension` configures the
# PROVIDER's output width, and app/embeddings/store.py refuses to write
# a vector whose length differs from this — a loud, readable error
# instead of a driver-level type failure.
EMBEDDING_DIMENSION = 384


class Embedding(Base):
    """One vector for one chunk of one source row.

    OWNERSHIP IS STRUCTURAL, NOT DERIVED. Unlike `skill_evidence` — which
    deliberately has no `user_id` and reaches its owner through its
    parent — this table stores `user_id` and puts it in the natural key.
    The reason is that `source_type`/`source_id` is a polymorphic string
    pair with no foreign key behind it, so there is no join that can
    prove ownership for every source kind. Making the owner part of the
    uniqueness boundary means a row cannot be addressed at all without
    naming whose it is: `app/embeddings/store.py` has no read path that
    omits `user_id`, so cross-user access is a schema property rather
    than a check someone has to remember to write.

    NOT NULL, never nullable. A NULL owner would read as "shared", and
    nothing in this slice is shared — every embeddable source (skill
    evidence, a connected account's repository, a saved job) belongs to
    exactly one user. Leaving the column nullable would create a hole
    that a future writer could fall into silently.

    NO SOURCE TEXT COLUMN. Only `content_hash` is stored. The text
    itself already lives in the row it came from, and duplicating it
    here would create a second copy of private resume prose on a
    different access path — see app/schemas/embedding.py. `char_start`/
    `char_end` are what make a chunk explainable without that copy.

    `UNIQUE(user_id, source_type, source_id, chunk_index,
    model_identifier)` is the deduplication identity:

      * same owner + same source chunk + same model -> ONE row. Re-running
        over unchanged content matches on `content_hash` and never calls
        the provider (app/embeddings/store.py).
      * content changed -> same row, new `content_hash` and new vector.
        Current state, not history: a superseded vector has no use, and
        keeping it would grow the table without bound.
      * model changed -> a SIBLING row. Two models' vectors are not
        comparable, so the identifier has to be part of the key rather
        than a column that gets overwritten. The cost is that rows for a
        retired model linger until something deletes them; there is no
        garbage collection in this slice.

    NOTHING HERE FEEDS MATCHING. No similarity is computed, no nearest
    neighbour is queried, no score reads this table, and no endpoint
    exposes it. `skill_match_v1` and `skill_gap_v1` are untouched and
    unaware of it. The schema is shaped for a later semantic-retrieval
    slice; that slice is what will add an index and a query path.
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "source_type",
            "source_id",
            "chunk_index",
            "model_identifier",
            name="uq_embeddings_owner_source_chunk_model",
        ),
        # Permanent invariants, so they earn a database CHECK — the same
        # line app/models/skill_evidence.py draws between a numeric range
        # (enforced) and a vocabulary (kept in Python).
        CheckConstraint("chunk_index >= 0", name="ck_embeddings_chunk_index_non_negative"),
        CheckConstraint(
            "(char_start IS NULL) = (char_end IS NULL)",
            name="ck_embeddings_char_span_paired",
        ),
        CheckConstraint(
            "char_start IS NULL OR char_start < char_end",
            name="ck_embeddings_char_span_ordered",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Closed set validated by app.schemas.embedding.EmbeddingSourceType,
    # stored as plain text — see that module.
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # Polymorphic, deliberately not a foreign key: no single FK can point
    # at three different source tables. Same trade-off, and the same lack
    # of referential integrity, that `skill_evidence.source_identifier`
    # documents.
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # 0 for a source that yields one document; the ordinal of the chunk
    # for a job description, which yields several.
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Character offsets into the SOURCE row's text, so a chunk can be
    # quoted back from it verbatim. Set for job-description chunks, which
    # are contiguous slices; NULL for candidate-side summaries, which are
    # assembled from several fields and so span nothing contiguous.
    # Inventing offsets for those would be a lie a reader could not
    # detect, which is why the pair is nullable rather than defaulted.
    char_start: Mapped[int | None] = mapped_column(Integer, default=None)
    char_end: Mapped[int | None] = mapped_column(Integer, default=None)
    # SHA-256 of the NORMALIZED source text, as 64 hex characters — see
    # app/embeddings/hashing.py. Cryptographic and stable across
    # processes; Python's built-in hash() is neither.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Which model produced this vector. Metadata AND part of the natural
    # key above. For the mock provider this is "mock-deterministic-v1",
    # which names it honestly as a fake rather than borrowing a real
    # model's identifier.
    model_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
