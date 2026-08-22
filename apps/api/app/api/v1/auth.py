"""Authentication endpoints: register, login, refresh, logout, me.

Browser flow (Prompt 1.2): the refresh token lives only in an HttpOnly
cookie, scoped to /api/v1/auth, and is never present in a JSON response
or accepted from a request body — a script on the page cannot read it,
and there is no alternate path that would defeat that. Access tokens are
short-lived and returned in the body for the caller to hold in memory.

Error responses are deliberately generic where specificity would leak
information an attacker could use: login never reveals whether an email
exists or the password was wrong, and neither refresh, logout, nor the
protected-route dependency distinguish "invalid", "expired", or
"revoked" in their response body. See docs/decisions.md for the full
rationale on both the token design and the error-response design.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.rate_limit import rate_limit_auth
from app.schemas.auth import AccessTokenResponse, LoginRequest, RegisterRequest, UserResponse
from app.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.security import decode_access_token as _decode_access_token
from app.settings import get_settings

router = APIRouter()
_bearer_scheme = HTTPBearer(auto_error=False)

# Identical wording regardless of the actual cause — never gives an
# attacker a signal to distinguish "no such account" from "wrong
# password", or "expired" from "forged" from "revoked".
_GENERIC_LOGIN_ERROR = "invalid email or password"
_GENERIC_AUTH_ERROR = "could not validate credentials"

# Scoped to /api/v1/auth only: the browser never needs to send this
# cookie to /api/v1/health, /api/v1/auth/me, or any future endpoint —
# only the three routes that actually read it.
_REFRESH_COOKIE_NAME = "refresh_token"
_REFRESH_COOKIE_PATH = "/api/v1/auth"


def _set_refresh_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=raw_token,
        max_age=settings.jwt_refresh_token_expire_days * 24 * 60 * 60,
        httponly=True,
        # Works over plain HTTP on "localhost" specifically — modern
        # browsers treat it as a secure context — and is unconditionally
        # correct once real deployments use HTTPS. See docs/decisions.md.
        secure=True,
        samesite="lax",
        path=_REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    # Attributes must match _set_refresh_cookie's for the browser to
    # recognize this as clearing the same cookie rather than a no-op.
    response.delete_cookie(
        key=_REFRESH_COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="lax",
        path=_REFRESH_COOKIE_PATH,
    )


async def _issue_tokens(
    db: AsyncSession, response: Response, user_id: uuid.UUID
) -> tuple[AccessTokenResponse, RefreshToken]:
    """Stage a new access/refresh token pair and set the refresh cookie.

    Flushes (so the new row has an id) but does not commit — callers
    that need to link a rotated-away token's replaced_by_id to this new
    row do so before committing, so both changes land in one atomic
    transaction. Returns the new RefreshToken row alongside the response
    body so rotation can read its id.
    """
    access_token, expires_in = create_access_token(user_id)
    raw_refresh_token, token_hash, expires_at = generate_refresh_token()
    new_record = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    db.add(new_record)
    await db.flush()
    _set_refresh_cookie(response, raw_refresh_token)
    token_response = AccessTokenResponse(
        access_token=access_token, token_type="bearer", expires_in=expires_in
    )
    return token_response, new_record


async def _is_benign_concurrent_refresh_race(db: AsyncSession, record: RefreshToken) -> bool:
    """True if `record` (already known to be revoked) is exactly the
    immediate, still-active predecessor of the token that replaced it,
    and was revoked recently enough to plausibly be a losing side of a
    concurrent-refresh race rather than genuine token reuse.

    Both conditions matter: without the recency check, a session left
    idle for a long time would keep tolerating reuse of its one-hop-back
    predecessor indefinitely (a real, if narrow, weakening — see
    docs/decisions.md); without the adjacency check, a token several
    rotations old could slip through if all those rotations happened to
    occur within the recency window (e.g. a rapid reload loop).
    """
    if record.replaced_by_id is None:
        return False  # revoked with no known successor (logout, or a prior cascade) — never benign

    assert record.revoked_at is not None  # caller already checked this
    settings = get_settings()
    age = datetime.now(UTC) - record.revoked_at
    if age > timedelta(seconds=settings.auth_refresh_reuse_grace_seconds):
        return False

    successor = await db.get(RefreshToken, record.replaced_by_id)
    return successor is not None and successor.revoked_at is None


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


@router.post("/login", response_model=AccessTokenResponse)
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(rate_limit_auth),
) -> AccessTokenResponse:
    email = body.email.lower()
    user = await db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_LOGIN_ERROR)

    token_response, _new_record = await _issue_tokens(db, response, user.id)
    await db.commit()
    return token_response


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(rate_limit_auth),
) -> AccessTokenResponse:
    """Reads the refresh token from the HttpOnly cookie only — there is
    no request-body alternative, so there is only one path a raw token
    ever travels on, and it's never one JavaScript can read or write."""
    raw_token = request.cookies.get(_REFRESH_COOKIE_NAME)
    if raw_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR)

    token_hash = hash_refresh_token(raw_token)
    # FOR UPDATE: serializes concurrent requests presenting the same
    # token. Without this, two racing requests could both read
    # revoked_at IS NULL and both attempt to rotate — the loser here
    # instead blocks until the winner commits, then sees the fully
    # up-to-date row (revoked_at and replaced_by_id both set), which is
    # exactly the information the benign-race check below needs.
    record = await db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    )

    if record is None:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR)

    if record.revoked_at is not None:
        if await _is_benign_concurrent_refresh_race(db, record):
            # A losing side of a concurrent-refresh race (two rapid page
            # reloads, or two tabs, both starting from this token). Fail
            # only this request — do NOT cascade-revoke, and do NOT
            # touch the cookie: a concurrent winning request may have
            # already set a new one in this same browser, and clearing
            # it here would destroy that valid session instead of just
            # failing this one losing request. Never hand back the
            # replacement token pair either — this stays a plain 401.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR
            )

        # Otherwise: reuse of a token that is not the current chain's
        # immediate predecessor, or reused outside the recency window —
        # a compromise signal. Revoke every active session for this
        # user, not just this one token.
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == record.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await db.commit()
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR)

    if record.expires_at < datetime.now(UTC):
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_AUTH_ERROR)

    record.revoked_at = datetime.now(UTC)
    # Rotation: reissuing sets a fresh cookie (new raw token, new row),
    # then links the old row to it via replaced_by_id — the exact
    # adjacency the benign-race check above depends on. One commit for
    # both changes, keeping them atomic.
    token_response, new_record = await _issue_tokens(db, response, record.user_id)
    record.replaced_by_id = new_record.id
    await db.commit()
    return token_response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> None:
    """Idempotent: calling logout with no session (already logged out, or
    never logged in) is not an error — it just clears the cookie."""
    raw_token = request.cookies.get(_REFRESH_COOKIE_NAME)
    if raw_token is not None:
        token_hash = hash_refresh_token(raw_token)
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await db.commit()
    _clear_refresh_cookie(response)


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
