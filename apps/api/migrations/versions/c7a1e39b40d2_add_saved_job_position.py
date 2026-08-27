"""add saved job position

Revision ID: c7a1e39b40d2
Revises: 59ae0cc1cf6b
Create Date: 2026-08-27 15:40:12.884301

Prompt 6.3. Adds the user-controlled ordering of a candidate's saved
jobs, which is the primary signal `roadmap_priority_v1` uses to decide
which jobs matter — deliberately NOT `skill_match_v1`, because "how well
do I match this" is a different question from "which of these do I
want".

`position` ORDERS, it does not LABEL. The "1, 2, 3" a user sees is the
1-based index in the sorted response, so a gap left by a deleted job is
invisible and nothing ever has to renumber to stay correct.

THE UNIQUE CONSTRAINT IS DEFERRABLE, and that is load-bearing rather
than decorative: reordering rewrites a whole list in one transaction, so
intermediate states legitimately hold two rows at the same position. A
non-deferrable constraint rejects the reorder halfway through, and the
usual workaround — shuffling everything to negative positions first —
doubles the writes to work around a check that simply belongs at COMMIT.

Schema only. No roadmap is stored: it is derived on read, because a
persisted plan asserting "AWS is required by 3 of your top 5 jobs" in a
SENTENCE becomes a false claim about the user's own data the moment two
of those jobs are deleted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7a1e39b40d2"
down_revision: str | Sequence[str] | None = "59ae0cc1cf6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default so this succeeds against a table that already has
    # rows; dropped immediately afterwards so new rows take their value
    # from the application, matching 90da84c6fbb1 and 266984262a64.
    op.add_column(
        "saved_jobs",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )

    # BACKFILL PRESERVES WHAT EVERY EXISTING USER ALREADY SEES. The list
    # endpoint returned `created_at DESC` before this prompt, so seeding
    # positions in that order means the first render after migrating is
    # byte-identical to the last render before it. A user's order only
    # changes when the user changes it.
    op.execute(
        """
        UPDATE saved_jobs AS target
        SET position = ranked.row_number - 1
        FROM (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY user_id ORDER BY created_at DESC, id
                ) AS row_number
            FROM saved_jobs
        ) AS ranked
        WHERE target.id = ranked.id
        """
    )

    op.alter_column("saved_jobs", "position", server_default=None)

    # Per USER, never global — two candidates ordering their own lists
    # are unrelated events, and a global constraint would make one
    # user's reorder fail because of another's.
    op.create_unique_constraint(
        "uq_saved_jobs_user_id_position",
        "saved_jobs",
        ["user_id", "position"],
        deferrable=True,
        initially="DEFERRED",
    )
    # The list endpoint's only ordering. Composite with user_id because
    # every query is already scoped to one owner.
    op.create_index(
        "ix_saved_jobs_user_id_position", "saved_jobs", ["user_id", "position"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_saved_jobs_user_id_position", table_name="saved_jobs")
    op.drop_constraint("uq_saved_jobs_user_id_position", "saved_jobs", type_="unique")
    op.drop_column("saved_jobs", "position")
