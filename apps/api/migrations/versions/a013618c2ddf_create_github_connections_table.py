"""create github connections table

Revision ID: a013618c2ddf
Revises: 90da84c6fbb1
Create Date: 2026-08-24 12:58:43.650583

Prompt 3.1. Which PUBLIC GitHub account a user says is theirs, and
nothing more: no password (never asked for), no OAuth or personal access
token, no email, no private repository data.

Strictly 1:1 with `users` — `user_id` is both primary key and foreign
key, the same shape as `profiles`. There is deliberately NO unique
constraint on `github_user_id` or `username`: this prompt verifies that
an account exists, not that the caller owns it, so a global unique would
let whoever connects a username first permanently lock out its real
owner. See app/models/github_connection.py and docs/decisions.md.

Schema only — no rows, and no GitHub requests are made here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a013618c2ddf"
down_revision: str | Sequence[str] | None = "90da84c6fbb1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "github_connections",
        # PK and FK in one column: ownership is the primary key, so
        # there is no second id an API route could accept or mis-scope.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # BigInteger: this is GitHub's key space, not ours, so a 32-bit
        # ceiling is not ours to assume.
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        # 39 is GitHub's maximum username length.
        sa.Column("username", sa.String(length=39), nullable=False),
        sa.Column("public_repo_count", sa.Integer(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
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
            name="fk_github_connections_user_id_users",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_github_connections"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("github_connections")
