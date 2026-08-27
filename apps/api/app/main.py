import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.auth import router as auth_router
from app.api.v1.candidate_skill import router as candidate_skill_router
from app.api.v1.github_connection import router as github_connection_router
from app.api.v1.github_ingestion import router as github_ingestion_router
from app.api.v1.health import router as health_router
from app.api.v1.job_import import router as job_import_router
from app.api.v1.profile import router as profile_router
from app.api.v1.qualification import router as qualification_router
from app.api.v1.resume import router as resume_router
from app.api.v1.roadmap import router as roadmap_router
from app.api.v1.saved_job import router as saved_job_router
from app.api.v1.skill_profile import router as skill_profile_router


def _allowed_origins() -> list[str]:
    """Read allowed browser origins from WEB_ORIGIN (comma-separated)."""
    origins = os.environ.get("WEB_ORIGIN", "http://localhost:3000")
    return [origin.strip() for origin in origins.split(",") if origin.strip()]


app = FastAPI(title="CareerLens API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    # POST added in Prompt 1.1 for the auth endpoints (register/login/
    # refresh/logout) — health was GET-only before this. PATCH added in
    # Prompt 1.3 for the profile update endpoint — without it, the
    # browser's CORS preflight (OPTIONS with
    # Access-Control-Request-Method: <method>) is rejected by this
    # middleware with a 400 before the real request is ever sent,
    # regardless of what the route itself allows (see docs/decisions.md).
    # DELETE added in Prompt 2.1 for the resume delete endpoint, learning
    # that lesson ahead of time instead of rediscovering it live again.
    # PUT added in Prompt 3.1 for the GitHub connection endpoint, which is
    # an idempotent replace of a per-user singleton. Same lesson a third
    # time: without it the browser's preflight is rejected here with a 400
    # and the route is never reached, however correct the route is.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
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
app.include_router(resume_router, prefix="/api/v1/resumes")
# Prompt 4.1 — user-owned saved job descriptions. Inert storage: no
# skill extraction (4.2) and no matching (4.3) happen here.
app.include_router(saved_job_router, prefix="/api/v1/saved-jobs")
# Prompt 4.1b — PDF import produces an editable DRAFT and persists
# nothing. Only the saved-jobs router above creates a row.
app.include_router(job_import_router, prefix="/api/v1/job-imports")
app.include_router(candidate_skill_router, prefix="/api/v1/candidate-skills")
# Prompt 5.1a — the candidate's own academic and experience facts.
# Structurally self-scoped (no user id in the path): these are exam
# results and graduation dates, so the surface has nothing to enumerate.
app.include_router(qualification_router, prefix="/api/v1/qualifications")
# Prompt 3.4 — the READ-ONLY presentation view over the same two tables
# the candidate-skill routes above mutate. A separate prefix, not an
# extension of /candidate-skills, so the mutation contract those routes
# (and their tests) pin stays untouched.
app.include_router(skill_profile_router, prefix="/api/v1/skill-profile")
app.include_router(github_connection_router, prefix="/api/v1/github-connection")
# Prompt 3.2 — mounted under the same prefix: an import belongs to a
# connection, and there is no ingestion without one.
app.include_router(github_ingestion_router, prefix="/api/v1/github-connection")
# Prompt 6.3 — ONE combined, candidate-level learning roadmap. Its own
# prefix rather than an extension of /saved-jobs: a roadmap references
# many jobs at once, the same reasoning that gave /skill-profile its own
# prefix instead of extending /candidate-skills. Read-only and not
# persisted; nothing here writes a row.
app.include_router(roadmap_router, prefix="/api/v1/roadmap")
