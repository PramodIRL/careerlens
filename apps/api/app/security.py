"""Password hashing and JWT/refresh-token helpers.

Passwords are hashed with bcrypt — slow and salted by design — so a
database leak alone never hands out a usable password (see the Prompt 1.1
report for the full explanation). Refresh tokens are random opaque
strings; only a SHA-256 hash of each is ever persisted, for the same
underlying reason: a leaked hash is useless without the original random
value, which only ever lives in the client that received it.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.settings import get_settings


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt. Never store the plaintext."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    """Check a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    """Issue a short-lived JWT access token.

    Returns (token, expires_in_seconds).
    """
    settings = get_settings()
    expires_in = settings.jwt_access_token_expire_minutes * 60
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(seconds=expires_in),
        "type": "access",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, expires_in


def decode_access_token(token: str) -> uuid.UUID:
    """Verify and decode an access token, returning the user id.

    Raises jwt.PyJWTError (invalid signature, malformed, or expired) —
    callers must turn this into a generic 401, never a specific reason,
    so a caller can't distinguish "expired" from "forged" from the
    response and use that as a probing signal.
    """
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return uuid.UUID(payload["sub"])


def hash_refresh_token(raw_token: str) -> str:
    """SHA-256 hash of a raw refresh token, for storage and lookup.

    SHA-256 rather than bcrypt is deliberate: the raw token is already a
    32-byte cryptographically random value, not a low-entropy human
    password, so it has no need for bcrypt's slow, salted design — a
    fast, deterministic hash keeps refresh/logout lookups a simple
    indexed query.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_refresh_token() -> tuple[str, str, datetime]:
    """Generate a new raw refresh token, its hash, and its expiry.

    Returns (raw_token, token_hash, expires_at). Only token_hash is ever
    persisted — the raw token is returned to the client once, at issuance,
    and never stored server-side in any form.
    """
    settings = get_settings()
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_refresh_token(raw_token)
    expires_at = datetime.now(UTC) + timedelta(days=settings.jwt_refresh_token_expire_days)
    return raw_token, token_hash, expires_at
