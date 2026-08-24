"""A user's connection to a PUBLIC GitHub account (Prompt 3.1)."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GitHubConnection(Base):
    """Which public GitHub account a candidate says is theirs.

    Strictly 1:1 with `users` — `user_id` is both the primary key and the
    foreign key, the same shape as `profiles` (see docs/decisions.md).
    That is not just tidiness: ownership *is* the primary key here, so
    there is no second id for an API route to accept, mis-scope, or leak.
    Connecting a different account replaces the row rather than adding
    one.

    WHAT IS DELIBERATELY NOT STORED. No GitHub password (this product
    never asks for one), no OAuth token, no personal access token, no
    refresh token, no email address, no private repository data, and
    none of the public-but-irrelevant profile fields GitHub also returns
    (avatar, bio, company, follower counts). Only what Prompt 3.2's
    ingestion actually needs, plus enough for the UI to show the user
    what they connected and when it was last checked.

    NO GLOBAL UNIQUENESS ON `github_user_id` OR `username`, and that is a
    decision rather than an oversight. Prompt 3.1 proves no ownership —
    it reads a public page, it does not authenticate against GitHub. A
    unique constraint would therefore enforce a claim nobody verified:
    whoever typed a given username first would permanently prevent its
    real owner from connecting their own account. That is a
    denial-of-service wearing data-integrity clothes. Two users may
    reference the same public account; nothing about reading public data
    conflicts. Revisit if OAuth ever makes ownership provable.

    Disconnecting DELETES this row, unlike a rejected candidate skill
    (app/schemas/skill.py), which is kept as a tombstone. The difference
    is what would otherwise come back: an extraction re-run re-suggests
    skills automatically, so a rejection must persist to suppress it,
    whereas nothing ever re-creates a GitHub connection except the user
    asking for it again. "Disconnect" should mean the identity is gone,
    not flagged.
    """

    __tablename__ = "github_connections"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # GitHub's immutable numeric account id. BigInteger rather than
    # Integer because it is GitHub's key space, not ours, and a 32-bit
    # ceiling is not ours to assume.
    #
    # This, not `username`, is what Prompt 3.2 must key ingestion on: a
    # GitHub username can be changed by its owner and later claimed by
    # somebody else, so a stored username alone can silently start
    # pointing at a stranger's repositories.
    github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The canonical `login` GitHub returned, not what the user typed —
    # GitHub lookups are case-insensitive, so "ADA" and "ada" both
    # resolve, and the account's own spelling is the one worth showing.
    # 39 characters is GitHub's maximum username length.
    username: Mapped[str] = mapped_column(String(39), nullable=False)
    # Public repository count as of `last_verified_at`. A snapshot, not a
    # live figure — the UI presents it with the timestamp for exactly
    # that reason, rather than implying it is current.
    public_repo_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # When GitHub last confirmed this account exists. Distinct from
    # `updated_at`, which also moves when nothing about the account
    # changed, and from `created_at`, which is when the user first
    # connected.
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
