from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# bcrypt (5.x) raises ValueError for any password whose UTF-8 encoding
# exceeds 72 bytes — it does not truncate. Validating this here, before
# the password ever reaches app/security.py, turns that crash into a
# normal 422 for both endpoints. Byte length, not character count: a
# password within a character-based max_length could still exceed 72
# bytes if it contains multi-byte UTF-8 characters (e.g. emoji).
_MAX_PASSWORD_BYTES = 72


def _validate_password_byte_length(v: str) -> str:
    if len(v.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {_MAX_PASSWORD_BYTES} bytes")
    return v


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    _check_password_byte_length = field_validator("password")(_validate_password_byte_length)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)

    _check_password_byte_length = field_validator("password")(_validate_password_byte_length)


class AccessTokenResponse(BaseModel):
    """Login/refresh response body.

    Deliberately has no refresh_token field — the browser flow (Prompt
    1.2) carries the refresh token only in an HttpOnly cookie, never in
    JSON a script on the page could read. See app/api/v1/auth.py.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    created_at: datetime
