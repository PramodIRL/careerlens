import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RefreshToken(Base):
    """A single refresh-token session.

    Identified by a hash of the raw token — the raw token itself is never
    stored (see app/security.py, and the Prompt 1.1 report for why).

    Rotation: using a token to get a new access token creates a new row
    and sets revoked_at on this one. Presenting an already-revoked token
    again is treated as a compromise signal — see app/api/v1/auth.py,
    which revokes every active session for the user when that happens.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
