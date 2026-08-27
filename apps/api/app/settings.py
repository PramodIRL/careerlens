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

    # --- Public GitHub connection (Prompt 3.1) ---
    # Base URL for GitHub's REST API. Configurable so a test or a future
    # GitHub Enterprise deployment can point elsewhere; there is no
    # credential here and never will be one in this flow — CareerLens
    # reads only unauthenticated, public endpoints.
    github_api_base_url: str = "https://api.github.com"
    # Total budget for one GitHub request. A user is waiting on a form
    # submit while this runs, so it must be short and hard — the whole
    # point of doing this in the request path rather than a worker is
    # that it is bounded. Applied per phase (connect/read/write/pool) by
    # app/github/http.py.
    github_request_timeout_seconds: float = 5.0
    # GitHub rejects API requests that send no User-Agent, so this is
    # required rather than cosmetic. It identifies the application, not
    # a user, and carries no credential.
    github_user_agent: str = "CareerLens/0.1 (+https://github.com/careerlens)"

    # --- Public GitHub ingestion (Prompt 3.2) ---
    # How many repositories one ingestion run will fetch DETAIL for
    # (languages + README). This is a hard budget constraint, not a
    # preference: unauthenticated GitHub allows 60 requests/hour per IP,
    # and each repository costs two of them. 20 repositories is already
    # ~42 requests — about 70% of the hourly budget for a single user.
    # Repositories are processed most-recently-pushed first, so the cap
    # keeps the most relevant work. Forks are excluded before the cap is
    # applied and never spend a request.
    github_max_repositories: int = 20
    # Safety valve on pagination. 10 pages x 100 per page = 1000
    # repositories. Hitting it marks the listing INCOMPLETE, which
    # suppresses deletion reconciliation (app/github/ingestion.py) —
    # never delete based on a listing we know was cut short.
    github_max_repository_pages: int = 10
    # README text is truncated to this many characters before storage.
    # Prompt 3.3 must quote a verbatim excerpt as evidence, so the text
    # itself has to be kept — a hash cannot be quoted — but a README is
    # third-party content with no natural size bound. 20k characters
    # covers essentially every real README; anything longer is stored
    # truncated with `readme_truncated` set, never silently clipped.
    github_readme_max_chars: int = 20_000
    # How many times a *transient* ingestion failure (timeout, 5xx,
    # transport error) is retried before the run is marked failed.
    # Rate limiting is retried separately and does not consume these
    # attempts in the same way — see app/github/ingestion.py.
    github_ingestion_max_retries: int = 2

    # --- Job import (Prompt 4.1b) ------------------------------------
    # Job description PDFs are parsed in-request and discarded, never
    # stored. Same default as the resume limit for consistency, but its
    # own setting so the two can diverge.
    job_pdf_max_size_bytes: int = 5 * 1024 * 1024

    # --- Embeddings (embedding infrastructure slice) -----------------
    # THERE IS NO EMBEDDING CREDENTIAL HERE AND NO VARIABLE NAME FOR
    # ONE. The only provider that exists is a deterministic local mock;
    # nothing in this product calls an embedding API, and adding a real
    # provider is a later slice that will bring its own decision about
    # where its key lives.
    #
    # Which provider `get_embedding_provider()` builds. "mock" is the
    # only known value; an unknown name raises rather than falling back,
    # so a typo cannot silently fill the table with fake vectors
    # labelled as something else.
    embedding_provider: str = "mock"
    # The width of the vectors the PROVIDER produces. The COLUMN's width
    # is fixed at 384 by the migration and by
    # app/models/embedding.py's EMBEDDING_DIMENSION — this setting
    # exists so a provider can be pointed at a different width, and
    # app/embeddings/store.py fails loudly if the two disagree rather
    # than letting PostgreSQL reject the insert. Changing the column
    # itself is a migration, not a configuration change.
    embedding_dimension: int = 384
    # Stored with every vector and part of the deduplication identity,
    # so changing it means "re-embed under a new model" rather than
    # "relabel the existing rows". Named to be unmistakably a fake.
    embedding_model_identifier: str = "mock-deterministic-v1"
    # The real model used when `embedding_provider` is "local" (Prompt
    # 5.2b). Runs in-process via onnxruntime — there is no hosted API and
    # no credential. 384 dimensions, matching the column; a model of a
    # different width would need a migration, not a config change.
    embedding_local_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # --- LLM explanations (Prompt 6.1) -------------------------------
    # THERE IS NO LLM CREDENTIAL HERE AND NO VARIABLE NAME FOR ONE. The
    # only provider that exists is a deterministic local mock: no key is
    # read or requested, and no model service is ever called. A real
    # provider is a later slice, and it brings its own decisions about
    # timeouts, retries and where its credential lives.
    #
    # An unknown name raises rather than falling back to the mock —
    # serving templated placeholder prose as though a model had written
    # it is the one failure this feature cannot afford.
    explanation_provider: str = "mock"

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
