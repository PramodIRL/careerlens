# Decision Log

A short record of notable technical decisions made during development.
Add one entry per decision, most recent first.

## Template

- **Date**:
- **Decision**:
- **Problem**:
- **Alternatives**:
- **Trade-off**:
- **Outcome**:

---

- **Date**: 2026-08-22
- **Decision**: Isolate `apps/api` tests with a dedicated Postgres *schema*
  (`careerlens_test`), created/dropped by the test suite itself, rather than
  a separate physical database.
- **Problem**: Prompt 0.3 adds DB-backed tests but defines no product tables
  yet, so there's no data to isolate today — the goal is establishing a
  pattern that won't need rework once tables exist.
- **Alternatives**: (1) a second physical database, provisioned via
  `infra/postgres/init.sql` or a CI/local setup step; (2) no isolation at
  all — run tests directly against the dev database's default schema.
- **Trade-off**: A schema is simpler than a second database (no
  `docker-compose.yml`/init-script changes, no extra CI setup) and is fully
  self-managed by the test suite, but it shares the same Postgres role/
  connection limits as dev, and doesn't isolate server-level settings.
- **Outcome**: Went with the schema approach (`apps/api/tests/conftest.py`).

- **Date**: 2026-08-22
- **Decision**: Enable the `pgvector` extension via an Alembic migration,
  even though `infra/postgres/init.sql` already does the same thing at
  container first-boot.
- **Problem**: `init.sql` only runs once, on a fresh Docker volume — it
  doesn't help a CI database, a manually-created Postgres instance, or a
  future staging/production database that wasn't bootstrapped from our
  compose file.
- **Alternatives**: Rely on `init.sql` alone; skip the migration.
- **Trade-off**: Slight duplication (both statements are the same idempotent
  `CREATE EXTENSION IF NOT EXISTS vector`), but the migration is what makes
  the extension reproducible via `alembic upgrade head` on *any* Postgres
  instance, not just ones using our compose file.
- **Outcome**: Kept both — `init.sql` for convenient local container
  first-boot, the migration as the portable, authoritative schema history.

- **Date**: 2026-08-22
- **Decision**: Verify "the web page can reach the API" in `scripts/smoke.sh`
  via `curl` (checking the API's CORS response header and the web page's
  HTML markup), instead of a headless browser.
- **Problem**: The web page's health check runs client-side (`"use client"`
  + `useEffect`), so no plain HTTP request can observe the *resolved*
  "API is healthy" text the way a real browser would after executing
  JavaScript.
- **Alternatives**: Add Playwright/Puppeteer and actually render the page.
- **Trade-off**: A headless browser would give a direct, literal answer,
  but is a new, fairly heavy dependency for a "minimal" smoke test at this
  stage. The `curl`-based check instead verifies the exact mechanism that
  would make or break the page's fetch in a real browser (CORS) plus that
  the expected component actually renders into the served HTML — a real,
  specific check, just not a literal one. The gap (JS execution) is covered
  by a documented manual step instead.
- **Outcome**: `curl`-based CORS + markup checks, zero new dependencies;
  manual browser confirmation documented in `README.md` as the final step.

- **Date**: 2026-08-22
- **Decision**: Store refresh tokens as opaque random strings hashed with
  SHA-256, not as JWTs.
- **Problem**: A refresh token needs to be revocable server-side (logout,
  rotation, compromise) — a pure stateless JWT can't be "un-issued"
  without a separate blocklist anyway, so there's little benefit to
  making it self-describing.
- **Alternatives**: Issue refresh tokens as JWTs too (with a `type:
  refresh` claim); store the raw refresh token value directly instead of
  a hash.
- **Trade-off**: An opaque token needs a database row per session (one
  extra table, one extra query on refresh), but that row is exactly what
  makes rotation and revocation simple and immediate, rather than needing
  a growing blocklist. Storing a hash instead of the raw value means a
  database leak alone can't be used to impersonate anyone — the same
  reasoning as password hashing, just with a fast hash (SHA-256) instead
  of a slow one (bcrypt), since the input is already 256 bits of random
  data, not a guessable human password.
- **Outcome**: `refresh_tokens.token_hash` (SHA-256), rotation on every
  use, and reuse-of-a-revoked-token cascades to revoke the whole session.

- **Date**: 2026-08-22
- **Decision**: In-memory rate limiting for `/auth/register`, `/login`,
  `/refresh` (10 requests/60s per client IP, shared budget), rather than
  a Redis-backed limiter.
- **Problem**: These endpoints need *some* protection against
  credential-guessing and account-creation abuse from day one.
- **Alternatives**: Redis-backed counters (Redis is already provisioned
  via docker-compose, just unused by the API so far).
- **Trade-off**: In-memory counters are single-process — they reset on
  restart and are not shared across multiple API instances, so this
  would under-count (and under-protect) in a scaled, multi-instance
  deployment. Redis would fix that but adds a new client dependency and
  makes the auth test suite depend on Redis being reachable too, for a
  "strategy" that doesn't need to be the final production answer yet.
- **Outcome**: In-memory (`app/rate_limit.py`), explicitly documented as
  single-process-only; swapping in a Redis-backed limiter is a natural,
  isolated follow-up once there's a multi-instance deployment to protect.

- **Date**: 2026-08-22
- **Decision**: Login returns an identical, generic error for both "no
  such account" and "wrong password"; registration returns a specific
  `409` for a duplicate email.
- **Problem**: Login is a credential-guessing surface — a specific
  "no such account" response lets an attacker enumerate which emails
  have accounts before ever guessing a password. Registration is a
  different situation: a client that submits an email deliberately wants
  an account tied to it, and needs an actionable reason if it can't have
  one.
- **Alternatives**: Generic errors everywhere, including registration
  (silently "succeeding" or giving a vague error on a duplicate email);
  specific errors everywhere, including login.
- **Trade-off**: A fully generic registration response is a worse
  experience for a very small enumeration-risk reduction — duplicate-email
  detection on signup is normal, expected behavior in virtually every
  real product, unlike revealing account existence through login.
- **Outcome**: Generic `401` (`"invalid email or password"`) for login
  failures of any cause; specific `409` (`"email already registered"`)
  for registration only.

<!-- Add new entries above this line, most recent first. -->
