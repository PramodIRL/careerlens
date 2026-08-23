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

- **Date**: 2026-08-22
- **Decision**: For the browser flow, move the refresh token out of the
  JSON response entirely and into an `HttpOnly`/`Secure`/`SameSite=Lax`
  cookie scoped to `/api/v1/auth`; the access token stays in-memory only
  in the frontend (no `localStorage`, no cookie).
- **Problem**: Prompt 1.1's API returned the raw refresh token in the
  login/refresh JSON body. Any JavaScript on the page — including an
  XSS payload — could read that response and steal a long-lived
  credential. An `HttpOnly` cookie is invisible to JavaScript by design;
  keeping the token in the JSON response too would have defeated that
  protection for the one client (the browser) it matters most for.
- **Alternatives**: Keep returning the refresh token in JSON and let the
  browser store it (localStorage or an app-managed cookie); accept it
  from either a cookie or a JSON body on `/refresh`/`/logout` for
  backward compatibility with non-browser clients.
- **Trade-off**: A cookie-only design means the API now depends on
  `allow_credentials=True` CORS and a same-site deployment (or, later,
  matching cookie `Domain` values across subdomains) to work at all —
  more coupling between web and API than a bearer-token-everywhere
  design would have. Accepted deliberately: the security property (raw
  token never JS-readable) is worth more than that flexibility for this
  product's browser-first flow. Non-browser clients aren't a real use
  case yet.
- **Outcome**: `AccessTokenResponse` (Prompt 1.1's `TokenPairResponse`,
  minus `refresh_token`); `/refresh` and `/logout` read the cookie only,
  no JSON-body alternative — verified live (browser `document.cookie`
  returns `""`, response body confirmed to omit the field) and by
  `test_login_response_never_includes_the_raw_refresh_token` and
  `test_login_sets_an_httponly_secure_samesite_cookie_scoped_to_auth`.

- **Date**: 2026-08-22
- **Decision**: Deduplicate concurrent calls to the frontend's
  `refresh()` into a single in-flight request (`api-client.ts`).
- **Problem**: Found live, not in a mocked test: React's Strict Mode
  double-invokes effects in development, so `AuthProvider`'s mount-time
  silent-refresh fired twice almost simultaneously. The API rotates the
  refresh token on every use and revokes the *entire* session if an
  already-used token is presented again (Prompt 1.1's reuse-detection) —
  so the second, losing call presented the first call's now-rotated-away
  cookie, which the server correctly treated as a reuse/compromise
  signal and revoked the session the first call had just established.
  Not React-specific in principle: two browser tabs mounting at once
  could hit the same race.
- **Alternatives**: Weaken server-side reuse detection with a grace
  period for near-simultaneous reuse (used by some real-world rotating-
  refresh-token implementations); guard only the React effect itself
  (e.g. a mount ref) rather than the API client.
- **Trade-off**: Fixing it in the API client protects every caller
  (present and future), not just this one effect, and keeps the
  server's reuse detection maximally strict — the safer place to relax
  a security control is nowhere, if the client can simply not create
  the race in the first place.
- **Outcome**: `refresh()` caches its in-flight promise; concurrent
  callers share the one request. Covered by
  `api-client.test.ts` (`refresh > deduplicates concurrent calls...`).

- **Date**: 2026-08-22
- **Decision**: Tolerate reuse of a revoked refresh token — without
  cascading revocation — only when it is *structurally* the immediate,
  still-active predecessor of the current token (via a new
  `replaced_by_id` link) *and* was revoked within a short configurable
  window (`AUTH_REFRESH_REUSE_GRACE_SECONDS`, default 5s). Rotation now
  uses `SELECT ... FOR UPDATE` to serialize concurrent attempts on the
  same token row.
- **Problem**: The client-side fix above (deduplicating same-page
  concurrent calls) doesn't help across two independent JS contexts —
  two rapid page reloads, or two browser tabs, each still holding the
  same pre-rotation cookie when they fire their own `/refresh`. Live
  reproduction confirmed: the losing request's failure was cascading
  the whole session, including the token the winning request had just
  issued. Proven live and via `test_concurrent_refresh_with_same_token_
  one_wins_one_loses_and_session_survives`.
- **Alternatives considered**: (1) A pure time-based grace period with
  no structural check — tolerates *any* recently-revoked token, which
  in a rapid-reload loop could accidentally tolerate a token several
  generations old. (2) Structural adjacency with no time bound —
  doesn't decay: a long-idle session's one-hop-back predecessor would
  stay tolerable indefinitely, a wider window than a short grace period
  gives. (3) Transparently handing the race-loser the winner's live
  token pair instead of a 401 — fully seamless UX, but adds a new path
  that returns real credentials from what's structurally an error
  condition; rejected in favor of the smaller, lower-risk change.
- **Trade-off**: `AUTH_REFRESH_REUSE_GRACE_SECONDS` trades security for
  UX in one narrow, bounded way — a stolen token that is *both* the
  exact immediate predecessor *and* replayed within this window is
  tolerated as if benign. Kept deliberately short (default 5s): ample
  for realistic browser/network race timing, not for an attacker who
  wasn't already racing the legitimate client in real time. Anything
  structurally older, or replayed later, is unaffected — full cascade,
  exactly as before.
- **Outcome**: `refresh_tokens.replaced_by_id` (migration
  `6826a2b31fcd`), `with_for_update()` on rotation, benign-race check in
  `app/api/v1/auth.py`. No frontend change — the API contract (request/
  response shape) is unchanged. Regression tests cover the race
  (concurrent + sequential), multi-generation reuse, and reuse after the
  grace window, each still triggering the original cascade behavior.

- **Date**: 2026-08-23
- **Decision**: For the candidate profile (Prompt 1.3): a `profiles`
  table keyed 1:1 on `users.id` (its own `user_id` primary key, no
  separate surrogate id) for scalar fields; a canonical, deduplicated
  `skills` lookup table (`name` + a lowercased `slug` unique key) linked
  through a `profile_target_skills` join table for declared target
  skills; and a plain normalized `profile_target_roles` text table (no
  lookup table) for target roles. Endpoints are id-addressable —
  `GET`/`PATCH /api/v1/profiles/{user_id}` — with an explicit ownership
  check (`user_id != current_user.id` → 403) as the first line of both
  handlers, rather than an implicit self-only `/profile`.
- **Problem**: Six fields to store, two of them multi-valued
  (target roles, target skills), with no existing product tables to
  extend. Needed a normalization approach for the multi-valued fields,
  and an endpoint shape that actually demonstrates and tests ownership
  enforcement rather than making cross-user access structurally
  unreachable.
- **Alternatives**: (1) Comma-separated `target_roles`/`target_skills`
  text columns on `profiles` directly — violates 1NF, turns "does this
  profile want skill X" into string matching instead of a join, and
  can't dedupe/canonicalize. (2) A `roles` lookup table mirroring
  `skills`, for symmetry. (3) A self-only `/api/v1/profile` endpoint
  (no id in the path, always scoped to the caller), matching the
  existing `/api/v1/auth/me` idiom.
- **Trade-off**: Skills get a lookup table because Prompt 2.x's
  resume-extracted skills and later job-required skills are expected to
  match against this exact same canonical identity for evidence-first
  scoring (project brief's core rule) — "Python" and "python" must
  resolve to one row across the whole system, not just within one
  profile. Target roles have no known cross-entity matching need yet —
  they're a declared preference, not a scored signal — so a `roles`
  lookup table would be speculative abstraction; a plain normalized
  text table (still 1NF: one row per role, no comma-separated list)
  covers today's requirement without it. The id-addressable endpoint
  shape means ownership is a runtime-checked property with a real
  403 test, not a structural guarantee — marginally more code (one
  explicit comparison) than a self-only route, but it is what makes
  "ownership-enforced endpoints" and "a cross-user access failure test"
  (as asked for) meaningful rather than vacuous.
- **Outcome**: `profiles`, `skills`, `profile_target_roles`,
  `profile_target_skills` (migration `8156d76f48cc`). `PATCH` uses
  Pydantic's `exclude_unset` for partial updates; an omitted field means
  "unchanged", explicit `null` clears an optional scalar, and
  `target_roles`/`target_skills` fully replace the existing set when
  present. Covered by `tests/test_profile.py`, including
  `test_cannot_get_another_users_profile` and
  `test_cannot_patch_another_users_profile`.

<!-- Add new entries above this line, most recent first. -->
