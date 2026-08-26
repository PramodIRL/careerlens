"""add candidate qualification provenance and review state

Revision ID: 75ce7fb927ca
Revises: dbf2d1f330c6
Create Date: 2026-08-27 00:43:13.612872


Prompt 5.1b. Gives each candidate qualification fact a provenance and a
review state, so the resume can fill the store automatically without
ever overwriting what the candidate has said about their own record.

ALTER ONLY — NO NEW TABLE. `skill_evidence` is a separate table because
one skill genuinely has many evidences (a resume line plus four distinct
GitHub signals). A qualification fact holds exactly one value and
therefore one provenance, so a second table would buy a join and an
idempotency story for a cardinality that cannot arise.

`status` mirrors `candidate_skills.status`, vocabulary included, because
it carries the same meaning and the same override invariant: an
extraction may write `suggested` and refresh it, but `confirmed` and
`rejected` are the user's word and outrank the extractor permanently.

Existing rows were all typed by hand, so they backfill as
`confirmed` / `manual` — the honest reading, and the one that protects
them from being refreshed by the first resume run after this deploys.

Schema only. No extraction runs here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "75ce7fb927ca"
down_revision: str | Sequence[str] | None = "dbf2d1f330c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default on the two NOT NULL columns so this succeeds
    # against a table that already has rows, then dropped immediately —
    # from here on new rows take their value from the ORM default, not
    # the database. The same add-then-drop technique
    # 90da84c6fbb1 used for candidate_skills.status.
    #
    # The backfill values are the honest reading of what is already
    # there: every pre-existing row was typed by a human through
    # PATCH /qualifications, so it is confirmed and manual — and
    # therefore protected from the first resume extraction that runs
    # after this migration.
    op.add_column(
        "candidate_qualifications",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="confirmed"),
    )
    op.alter_column("candidate_qualifications", "status", server_default=None)
    op.add_column(
        "candidate_qualifications",
        sa.Column("source_type", sa.String(length=20), nullable=False, server_default="manual"),
    )
    op.alter_column("candidate_qualifications", "source_type", server_default=None)

    op.add_column(
        "candidate_qualifications",
        sa.Column("source_identifier", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "candidate_qualifications", sa.Column("excerpt", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "candidate_qualifications",
        sa.Column("extraction_method", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "candidate_qualifications",
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
    )
    op.create_check_constraint(
        "ck_candidate_qualifications_confidence_range",
        "candidate_qualifications",
        "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "ck_candidate_qualifications_confidence_range", "candidate_qualifications", type_="check"
    )
    op.drop_column("candidate_qualifications", "confidence")
    op.drop_column("candidate_qualifications", "extraction_method")
    op.drop_column("candidate_qualifications", "excerpt")
    op.drop_column("candidate_qualifications", "source_identifier")
    op.drop_column("candidate_qualifications", "source_type")
    op.drop_column("candidate_qualifications", "status")
