"""add resume extraction fields

Revision ID: 266984262a64
Revises: 5c4a2b275fdb
Create Date: 2026-08-23 20:15:39.521053

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "266984262a64"
down_revision: str | Sequence[str] | None = "5c4a2b275fdb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("resumes", sa.Column("extracted_text", sa.Text(), nullable=True))
    op.add_column("resumes", sa.Column("error_message", sa.String(length=500), nullable=True))
    # server_default (not just the model's Python-side default) so this
    # doesn't fail against a table that already has rows (any resumes
    # uploaded under Prompt 2.1) — NOT NULL needs a value for them too.
    # Dropped once applied: new rows get 0 from the ORM/application
    # default from here on, not from the database.
    op.add_column(
        "resumes",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("resumes", "attempt_count", server_default=None)
    op.add_column("resumes", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))

    # Prompt 2.2 renames the status vocabulary to match this prompt's
    # own wording (see docs/decisions.md): uploaded -> queued,
    # completed -> succeeded. "processing"/"failed" are unchanged.
    # Existing Prompt 2.1 rows (never processed) become "queued" so
    # they're picked up the same as any other never-processed resume.
    op.execute("UPDATE resumes SET status = 'queued' WHERE status = 'uploaded'")
    op.execute("UPDATE resumes SET status = 'succeeded' WHERE status = 'completed'")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE resumes SET status = 'uploaded' WHERE status = 'queued'")
    op.execute("UPDATE resumes SET status = 'completed' WHERE status = 'succeeded'")

    op.drop_column("resumes", "processed_at")
    op.drop_column("resumes", "attempt_count")
    op.drop_column("resumes", "error_message")
    op.drop_column("resumes", "extracted_text")
