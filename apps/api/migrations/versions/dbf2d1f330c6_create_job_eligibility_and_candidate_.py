"""create job eligibility and candidate qualification tables

Revision ID: dbf2d1f330c6
Revises: 48884966ac50
Create Date: 2026-08-26 11:42:46.708659

Prompt 5.1a. Opens the eligibility domain: the qualification bars a
posting sets, and the facts a candidate declares about themselves.

THREE NEW TABLES, NO EXISTING TABLE ALTERED. `job_skill_requirements`
and `candidate_skills` are untouched, and deliberately so — a CGPA floor
is not a skill, and putting one in the skill tables would have meant a
nullable `skill_id` and two meanings in one column. Skill matching and
eligibility stay separate domains all the way down to the schema.

  candidate_qualifications
      One row per (user, fact type), not one wide row per user. Each
      fact gets its own provenance and review state in 5.1b, which a
      wide table could only express as parallel columns per field. No
      `status` column yet: every row here is user-declared, so there is
      nothing to review — 5.1b adds it, exactly as
      90da84c6fbb1 added `status` to candidate_skills once Prompt 2.4
      gave it a meaning.

      Every value column is NULLABLE, and absence means UNKNOWN. Nothing
      in this product may default an unstated CGPA to 0.0 and report a
      candidate as failing on that basis.

  job_eligibility_requirements
      One row per (job, requirement type). `value_scale` is nullable
      because a posting saying "minimum CGPA 7.5" has not said which
      scale it means, and the resolver reports UNDETERMINED rather than
      assuming one.

  job_eligibility_requirement_values
      Accepted values for a categorical requirement ("B.Tech or B.E."),
      as rows rather than a delimited string — membership is a join, not
      a substring search.

Schema only. No extraction runs here, and no candidate is evaluated:
eligibility verdicts are derived on read and never stored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dbf2d1f330c6"
down_revision: str | Sequence[str] | None = "48884966ac50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "candidate_qualifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("fact_type", sa.String(length=40), nullable=False),
        sa.Column("value_numeric", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("value_text", sa.String(length=80), nullable=True),
        sa.Column("value_scale", sa.Numeric(precision=4, scale=2), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "fact_type", name="uq_candidate_qualifications_user_fact"),
    )
    op.create_index(
        op.f("ix_candidate_qualifications_user_id"),
        "candidate_qualifications",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "job_eligibility_requirements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("saved_job_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_type", sa.String(length=40), nullable=False),
        sa.Column("comparator", sa.String(length=10), nullable=False),
        sa.Column("numeric_value", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("numeric_max", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("value_scale", sa.Numeric(precision=4, scale=2), nullable=True),
        sa.Column("requirement_level", sa.String(length=20), nullable=False),
        sa.Column("open_ended", sa.Boolean(), nullable=False),
        sa.Column("matched_term", sa.String(length=80), nullable=False),
        sa.Column("excerpt", sa.String(length=500), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("extraction_method", sa.String(length=40), nullable=False),
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
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_job_eligibility_requirements_confidence_range",
        ),
        sa.ForeignKeyConstraint(["saved_job_id"], ["saved_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "saved_job_id", "requirement_type", name="uq_job_eligibility_requirements_job_type"
        ),
    )
    op.create_index(
        op.f("ix_job_eligibility_requirements_saved_job_id"),
        "job_eligibility_requirements",
        ["saved_job_id"],
        unique=False,
    )
    op.create_table(
        "job_eligibility_requirement_values",
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("value", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(
            ["requirement_id"], ["job_eligibility_requirements.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("requirement_id", "value"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("job_eligibility_requirement_values")
    op.drop_index(
        op.f("ix_job_eligibility_requirements_saved_job_id"),
        table_name="job_eligibility_requirements",
    )
    op.drop_table("job_eligibility_requirements")
    op.drop_index(
        op.f("ix_candidate_qualifications_user_id"), table_name="candidate_qualifications"
    )
    op.drop_table("candidate_qualifications")
