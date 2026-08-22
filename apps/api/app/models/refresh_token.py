import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RefreshToken(Base):
    """A single refresh-token session.

    Identified by a hash of the raw token — the raw token itself is never
    stored (see app/security.py, and the Prompt 1.1 report for why).

    Rotation: using a token to get a new access token creates a new row,
    sets revoked_at on this one, and points replaced_by_id at the new
    row — an exact, unambiguous link to the one token that replaced this
    one (Prompt 1.2 bug fix).

    Reuse of an already-revoked token is normally treated as a
    compromise signal (every active session for the user is revoked —
    see app/api/v1/auth.py) — *except* when the presented token is
    exactly the immediate, still-active predecessor (via replaced_by_id)
    and was revoked within a short configurable window, which is instead
    treated as a benign concurrent-refresh race (two rapid page reloads,
    or two tabs, both racing from the same starting cookie) and just
    fails that one request without touching the rest of the session.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
