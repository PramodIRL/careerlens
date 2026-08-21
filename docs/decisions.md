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

<!-- Add new entries above this line, most recent first. -->
