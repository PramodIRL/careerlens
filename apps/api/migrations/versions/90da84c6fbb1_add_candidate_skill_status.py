"""add candidate skill status

Revision ID: 90da84c6fbb1
Revises: 610680fe7d6a
Create Date: 2026-08-23 23:44:45.051373

Prompt 2.4. Adds the review state a user sets on an extracted skill
(see app.schemas.skill.CandidateSkillStatus). "rejected" is a persistent
tombstone rather than a deletion, which is what stops the next
extraction run from silently recreating a skill the user turned down.

Schema only — no extraction runs here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "90da84c6fbb1"
down_revision: str | Sequence[str] | None = "610680fe7d6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default so this succeeds against a table that already has
    # rows — any candidate skill created before this migration was, by
    # definition, never reviewed. Dropped immediately afterwards: from
    # here on new rows get their value from the ORM/application default,
    # not the database, matching `resumes.status` (and the same
    # add-then-drop technique 266984262a64 used for attempt_count).
    op.add_column(
        "candidate_skills",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="suggested"),
    )
    op.alter_column("candidate_skills", "status", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("candidate_skills", "status")
