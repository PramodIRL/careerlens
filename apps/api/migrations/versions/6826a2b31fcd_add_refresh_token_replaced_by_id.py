"""add refresh_token replaced_by_id

Revision ID: 6826a2b31fcd
Revises: 6dca34132440
Create Date: 2026-08-22 22:40:38.776832

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6826a2b31fcd"
down_revision: str | Sequence[str] | None = "6dca34132440"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FK_NAME = "fk_refresh_tokens_replaced_by_id_refresh_tokens"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("refresh_tokens", sa.Column("replaced_by_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        _FK_NAME,
        "refresh_tokens",
        "refresh_tokens",
        ["replaced_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(_FK_NAME, "refresh_tokens", type_="foreignkey")
    op.drop_column("refresh_tokens", "replaced_by_id")
