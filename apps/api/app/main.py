import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.auth import router as auth_router
from app.api.v1.health import router as health_router
from app.api.v1.profile import router as profile_router


def _allowed_origins() -> list[str]:
    """Read allowed browser origins from WEB_ORIGIN (comma-separated)."""
    origins = os.environ.get("WEB_ORIGIN", "http://localhost:3000")
    return [origin.strip() for origin in origins.split(",") if origin.strip()]


app = FastAPI(title="CareerLens API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    # POST added in Prompt 1.1 for the auth endpoints (register/login/
    # refresh/logout) — health was GET-only before this.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    # Prompt 1.2: the browser auth flow relies on an HttpOnly refresh-
    # token cookie, which the browser only attaches to (and accepts
    # Set-Cookie from) cross-origin requests when the response opts in
    # here. Requires allow_origins to stay an explicit list, never "*" —
    # already true via _allowed_origins() above.
    allow_credentials=True,
)

app.include_router(health_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1/auth")
app.include_router(profile_router, prefix="/api/v1/profiles")
