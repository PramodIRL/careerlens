"""add skill taxonomy and evidence

Revision ID: 610680fe7d6a
Revises: 266984262a64
Create Date: 2026-08-23 22:56:38.386793

Prompt 2.3. Extends the EXISTING `skills` table (created by Prompt 1.3's
8156d76f48cc and already referenced by `profile_target_skills`) with a
taxonomy, rather than introducing a second "canonical skills" table —
splitting skill identity would break the guarantee that "Python" means
one row across the whole system. See docs/decisions.md.

Schema only: no rows are inserted here. Seeding the taxonomy is a
separate, explicit step (`make seed-skills`), for the same reason
266984262a64 did not enqueue Celery jobs — a migration must stay
applicable in an environment where the rest of the system is not
available, and must not carry side effects beyond the schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "610680fe7d6a"
down_revision: str | Sequence[str] | None = "266984262a64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # `category` is nullable: skills coined by users through their
    # profile's target skills have no taxonomy category, and backfilling
    # them with "other" would invent information.
    op.add_column("skills", sa.Column("category", sa.String(length=40), nullable=True))
    # server_default so this succeeds against a table that already has
    # rows. Unlike 266984262a64's attempt_count, the default is KEPT
    # rather than dropped — matching how `profiles` and `resumes` carry
    # their own updated_at defaults.
    op.add_column(
        "skills",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "skill_aliases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("alias", sa.String(length=80), nullable=False),
        sa.Column("alias_slug", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Global, not per-skill: one alias must resolve to exactly one
        # canonical skill.
        sa.UniqueConstraint("alias_slug"),
    )
    op.create_index(op.f("ix_skill_aliases_skill_id"), "skill_aliases", ["skill_id"], unique=False)

    op.create_table(
        "skill_relations",
        sa.Column("from_skill_id", sa.Uuid(), nullable=False),
        sa.Column("to_skill_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "from_skill_id <> to_skill_id", name="ck_skill_relations_no_self_reference"
        ),
        sa.ForeignKeyConstraint(["from_skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("from_skill_id", "to_skill_id"),
    )

    op.create_table(
        "candidate_skills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
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
        # RESTRICT, not CASCADE: a shared canonical skill must not be
        # deletable while candidates still reference it.
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "skill_id", name="uq_candidate_skills_user_skill"),
    )
    op.create_index(
        op.f("ix_candidate_skills_skill_id"), "candidate_skills", ["skill_id"], unique=False
    )
    op.create_index(
        op.f("ix_candidate_skills_user_id"), "candidate_skills", ["user_id"], unique=False
    )

    op.create_table(
        "skill_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_skill_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_identifier", sa.String(length=255), nullable=False),
        sa.Column("excerpt", sa.String(length=500), nullable=True),
        sa.Column("extraction_method", sa.String(length=40), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
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
        # A value range is a permanent invariant, so it belongs in the
        # database — unlike the closed-set vocabularies (source_type,
        # extraction_method), which stay Python-side so they can grow
        # without a migration.
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_skill_evidence_confidence_range"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_skill_id"], ["candidate_skills.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # The idempotency key that lets Prompt 2.4 re-run extraction over
        # the same resume without accumulating duplicate evidence.
        sa.UniqueConstraint(
            "candidate_skill_id",
            "source_type",
            "source_identifier",
            "extraction_method",
            name="uq_skill_evidence_natural_key",
        ),
    )
    op.create_index(
        op.f("ix_skill_evidence_candidate_skill_id"),
        "skill_evidence",
        ["candidate_skill_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_skill_evidence_candidate_skill_id"), table_name="skill_evidence")
    op.drop_table("skill_evidence")
    op.drop_index(op.f("ix_candidate_skills_user_id"), table_name="candidate_skills")
    op.drop_index(op.f("ix_candidate_skills_skill_id"), table_name="candidate_skills")
    op.drop_table("candidate_skills")
    op.drop_table("skill_relations")
    op.drop_index(op.f("ix_skill_aliases_skill_id"), table_name="skill_aliases")
    op.drop_table("skill_aliases")
    op.drop_column("skills", "updated_at")
    op.drop_column("skills", "category")
