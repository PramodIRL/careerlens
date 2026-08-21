"""enable pgvector extension

Revision ID: 5b0b21f962b1
Revises:
Create Date: 2026-08-22 01:37:08.281948

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b0b21f962b1"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enable the pgvector extension. No product tables are created here —
    later prompts will add their own migrations for those."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    """Disable the pgvector extension."""
    op.execute("DROP EXTENSION IF EXISTS vector")
