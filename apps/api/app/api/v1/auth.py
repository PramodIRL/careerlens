"""Authentication endpoints: register, login, refresh, logout, me.

Error responses are deliberately generic where specificity would leak
information an attacker could use: login never reveals whether an email
exists or the password was wrong, and neither refresh, logout, nor the
protected-route dependency distinguish "invalid", "expired", or
"revoked" in their response body. See the Prompt 1.1 report for the
full rationale.
"""

import uuid
from datetime import UTC, datetime

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.rate_limit import rate_limit_auth
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPairResponse,
    UserResponse,
)
from app.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.security import decode_access_token as _decode_access_token

router = APIRouter()
_bearer_scheme = HTTPBearer(auto_error=False)

# Identical wording regardless of the actual cause — never gives an
# attacker a signal to distinguish "no such account" from "wrong
# password", or "expired" from "forged" from "revoked".
_GENERIC_LOGIN_ERROR = "invalid email or password"
_GENERIC_AUTH_ERROR = "could not validate credentials"


async def _issue_token_pair(db: AsyncSession, user_id: uuid.UUID) -> TokenPairResponse:
    access_token, expires_in = create_access_token(user_id)
    raw_refresh_token, token_hash, expires_at = generate_refresh_token()
    db.add(RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at))
    await db.commit()
    return TokenPairResponse(
        access_token=access_token,
        refresh_token=raw_refresh_token,
        token_type="bearer",
        expires_in=expires_in,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(rate_limit_auth),
) -> User:
    """Duplicate email gets a specific 409, not a generic error: unlike
    login, this isn't a credential-guessing surface — a client
    deliberately submitted an email it wants an account for, and needs a
    clear, actionable reason it can't have one. See docs/decisions.md."""
    email = body.email.lower()
    existing = await db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already registered",
        )

    user = User(email=email, hashed_password=hash_password(body.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenPairResponse)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(rate_limit_auth),
) -> TokenPairResponse:
    email = body.email.lower()
    user = await db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_LOGIN_ERROR)

    return await _issue_token_pair(db, user.id)


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(rate_limit_auth),
) -> TokenPairResponse:
    token_hash = hash_refresh_token(body.refresh_token)
    record = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))

    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR)

    if record.revoked_at is not None:
        # Reuse of an already-rotated/revoked token is a compromise
        # signal (a legitimate client would never do this) — revoke
        # every active session for this user, not just this one token.
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == record.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR)

    if record.expires_at < datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR)

    record.revoked_at = datetime.now(UTC)
    return await _issue_token_pair(db, record.user_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> None:
    token_hash = hash_refresh_token(body.refresh_token)
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await db.commit()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Protected-route dependency: verify the bearer JWT, return the user.

    Missing header, malformed token, bad signature, expired token, and
    "user no longer exists" all produce the same 401 + generic message.
    """
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR)

    try:
        user_id = _decode_access_token(credentials.credentials)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR
        ) from exc

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR)
    return user


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
