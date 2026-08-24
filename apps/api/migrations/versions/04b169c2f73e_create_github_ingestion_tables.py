"""create github ingestion tables

Revision ID: 04b169c2f73e
Revises: a013618c2ddf
Create Date: 2026-08-24 14:12:07.884210

Prompt 3.2. Normalized PUBLIC repository data plus the state of each
ingestion run.

Three things here are load-bearing rather than incidental:

  * `uq_github_repositories_user_repo` is on (user_id, github_repo_id),
    NOT on full_name — a repository can be renamed, and upserting on the
    name would create a duplicate row on the next run.
  * The unique index on github_ingestion_runs is PARTIAL, covering only
    queued/processing rows, so a user may have any number of finished
    runs but never two active ones.
  * github_repositories.deleted_at supports SOFT deletion; nothing here
    ever removes a repository row.

No raw GitHub JSON is stored anywhere. The readme_* columns are the only
retained source snapshot — see app/models/github_repository.py.

Schema only: no rows, and no GitHub requests are made here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "04b169c2f73e"
down_revision: str | Sequence[str] | None = "a013618c2ddf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "github_repositories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # BigInteger: GitHub's key space, not ours.
        sa.Column("github_repo_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("full_name", sa.String(length=400), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("is_fork", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("primary_language", sa.String(length=100), nullable=True),
        sa.Column("stargazers_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("forks_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("github_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("github_updated_at", sa.DateTime(timezone=True), nullable=True),
        # The only retained raw source snapshot in this product.
        sa.Column("readme_text", sa.Text(), nullable=True),
        sa.Column("readme_sha", sa.String(length=64), nullable=True),
        sa.Column("readme_byte_size", sa.Integer(), nullable=True),
        sa.Column(
            "readme_truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        # Resume marker for a paused run — see the model for why this
        # cannot be inferred from updated_at.
        sa.Column("detail_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_github_repositories_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_github_repositories"),
        # Identity across reruns. On (user_id, github_repo_id) rather
        # than a global unique on github_repo_id, because Prompt 3.1
        # deliberately allows two users to connect the same public
        # account.
        sa.UniqueConstraint("user_id", "github_repo_id", name="uq_github_repositories_user_repo"),
    )
    op.create_index(
        "ix_github_repositories_user_id", "github_repositories", ["user_id"], unique=False
    )

    op.create_table(
        "github_repository_languages",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("language", sa.String(length=100), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["github_repositories.id"],
            ondelete="CASCADE",
            name="fk_github_repository_languages_repository_id",
        ),
        sa.PrimaryKeyConstraint("repository_id", "language", name="pk_github_repository_languages"),
    )

    op.create_table(
        "github_repository_topics",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["github_repositories.id"],
            ondelete="CASCADE",
            name="fk_github_repository_topics_repository_id",
        ),
        sa.PrimaryKeyConstraint("repository_id", "topic", name="pk_github_repository_topics"),
    )

    op.create_table(
        "github_ingestion_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        # Three distinct counts — see app/models/github_ingestion_run.py
        # for why collapsing them would produce a misleading UI.
        sa.Column("repositories_available", sa.Integer(), nullable=True),
        sa.Column("repositories_forks_excluded", sa.Integer(), nullable=True),
        sa.Column("repositories_total", sa.Integer(), nullable=True),
        sa.Column(
            "repositories_completed", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("repositories_failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_github_ingestion_runs_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_github_ingestion_runs"),
    )
    op.create_index(
        "ix_github_ingestion_runs_user_id", "github_ingestion_runs", ["user_id"], unique=False
    )
    # PARTIAL unique index: any number of finished runs per user, never
    # two active ones. This is what stops a double-clicked Import button
    # from starting two runs that race to upsert the same repositories.
    op.create_index(
        "uq_github_ingestion_runs_one_active_per_user",
        "github_ingestion_runs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'processing')"),
    )
    # Application defaults own these from here on, matching the
    # add-then-drop technique 90da84c6fbb1 and 266984262a64 used.
    op.alter_column("github_ingestion_runs", "status", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_github_ingestion_runs_one_active_per_user", table_name="github_ingestion_runs"
    )
    op.drop_index("ix_github_ingestion_runs_user_id", table_name="github_ingestion_runs")
    op.drop_table("github_ingestion_runs")
    op.drop_table("github_repository_topics")
    op.drop_table("github_repository_languages")
    op.drop_index("ix_github_repositories_user_id", table_name="github_repositories")
    op.drop_table("github_repositories")
