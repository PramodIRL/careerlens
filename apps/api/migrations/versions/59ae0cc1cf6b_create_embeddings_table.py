"""create embeddings table

Revision ID: 59ae0cc1cf6b
Revises: 75ce7fb927ca
Create Date: 2026-08-27

Adds the one table this slice needs and touches nothing else. No
existing table is altered, no column is dropped, and no data is
migrated — an embedding is derived from rows that already exist, so
there is nothing to backfill and `downgrade` loses only a cache that can
be rebuilt.

The `vector(384)` dimension is written as a LITERAL here rather than
imported from app.models.embedding.EMBEDDING_DIMENSION. A migration
records what was actually applied to a database at a point in time; if
it read a constant, editing that constant would silently change the
history of what this revision did. The two must stay equal, and the
model's docstring says so.

The pgvector extension itself is not created here — revision
5b0b21f962b1 already does it, and it is the first revision in the chain,
so `vector` is guaranteed to exist by the time this runs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.embeddings.vector_type import Vector

# revision identifiers, used by Alembic.
revision: str = "59ae0cc1cf6b"
down_revision: str | Sequence[str] | None = "75ce7fb927ca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("model_identifier", sa.String(length=100), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_embeddings_chunk_index_non_negative"),
        sa.CheckConstraint(
            "(char_start IS NULL) = (char_end IS NULL)", name="ck_embeddings_char_span_paired"
        ),
        sa.CheckConstraint(
            "char_start IS NULL OR char_start < char_end", name="ck_embeddings_char_span_ordered"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "source_type",
            "source_id",
            "chunk_index",
            "model_identifier",
            name="uq_embeddings_owner_source_chunk_model",
        ),
    )
    op.create_index(op.f("ix_embeddings_user_id"), "embeddings", ["user_id"], unique=False)

    # NO VECTOR INDEX (ivfflat/hnsw). An approximate-nearest-neighbour
    # index only serves distance queries, and this slice performs none —
    # building one now would cost write time and memory to accelerate a
    # query that does not exist. It also cannot be tuned yet: ivfflat's
    # list count wants a representative row count, and there is no
    # production data to derive one from. The retrieval slice adds it,
    # in its own migration, when there is something to measure.


def downgrade() -> None:
    op.drop_index(op.f("ix_embeddings_user_id"), table_name="embeddings")
    op.drop_table("embeddings")
