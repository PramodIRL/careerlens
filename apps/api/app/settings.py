from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Shared repo-root .env (see .env.example), resolved relative to this file
# so it's found regardless of the process's working directory.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    """Environment-based configuration for the API.

    Values come from real process environment variables, or from the
    repo-root .env file if present (copy .env.example to .env — see
    README.md). The defaults below match .env.example and only work
    against a local dev Postgres container, never a real deployment.
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    postgres_user: str = "careerlens"
    postgres_password: str = "changeme"
    postgres_db: str = "careerlens"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Signs access JWTs. This default is an obvious local-dev-only
    # placeholder (matches the postgres_password convention above),
    # length chosen to meet HS256's 32-byte recommended minimum (RFC
    # 7518 §3.2) so local dev doesn't trip PyJWT's key-length warning —
    # every real deployment must set its own via the environment.
    jwt_secret: str = "insecure-dev-secret-change-me-before-deploying"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 30

    auth_rate_limit_max_requests: int = 10
    auth_rate_limit_window_seconds: int = 60

    # How long a just-rotated refresh token is still tolerated if
    # presented again, provided it's exactly the immediate predecessor
    # of the still-active token that replaced it (see
    # app/api/v1/auth.py). Covers a legitimate concurrent-refresh race
    # (two rapid page reloads, or two tabs, both firing from the same
    # starting cookie) without weakening detection of genuine reuse —
    # anything older than one generation, or reused after this window,
    # still revokes the whole session. Security/UX trade-off: larger
    # values tolerate slower/laggier races (better UX) but also widen
    # the window an attacker with a freshly-rotated-away token could
    # exploit before compromise handling kicks in (weaker security);
    # keep this small — a few seconds covers realistic browser/network
    # timing without meaningfully helping an attacker.
    auth_refresh_reuse_grace_seconds: int = 5

    # Local filesystem directory uploaded resumes are written to
    # (app/storage/local.py), relative to the process's working
    # directory — every Makefile target and `uv run` invocation in this
    # repo runs with apps/api as cwd, so this resolves to
    # apps/api/var/resumes in practice. Not committed (see .gitignore).
    # A future S3-compatible backend replaces this without changing any
    # resume domain code — see app/storage/base.py.
    resume_storage_dir: str = "var/resumes"
    # Maximum accepted resume upload size, in bytes. Default: 5 MiB —
    # comfortably more than a text-based PDF/DOCX resume needs, without
    # being reckless.
    resume_max_size_bytes: int = 5 * 1024 * 1024

    # Redis is already provisioned via docker-compose (see the rate-limit
    # decision in docs/decisions.md) — Prompt 2.2 is what finally uses it,
    # as both the Celery broker and, for now, nothing else (no result
    # backend: Postgres — the resumes row itself — is the single source
    # of truth for extraction job state; see app/worker.py).
    redis_url: str = "redis://localhost:6379/0"
    # How many times a *transient* extraction failure (e.g. a storage
    # read error) is retried via Celery's own retry mechanism before the
    # job is marked permanently failed. Does not apply to permanent
    # failures (a malformed document fails identically every time, so
    # retrying it only delays the user-visible failure for nothing) —
    # see app/worker.py's _TransientExtractionError / _PermanentExtractionError.
    resume_extraction_max_retries: int = 2

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy DSN built from the settings above."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
