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

- **Date**: 2026-08-23
- **Decision**: Add `"PATCH"` to `CORSMiddleware`'s `allow_methods` in
  `apps/api/app/main.py`.
- **Problem**: Found live via the browser: Prompt 1.3's profile update
  endpoint uses `PATCH /api/v1/profiles/{user_id}`, but
  `allow_methods` was still `["GET", "POST"]` from Prompt 1.1 (auth-only
  at the time). Starlette's `CORSMiddleware` rejects a preflight whose
  `Access-Control-Request-Method` isn't in `allow_methods` with a plain
  400 *before* the request reaches routing — so every "Save profile"
  click failed at the preflight, the real `PATCH` was never sent, and
  the route/ownership/validation code (already covered by
  `tests/test_profile.py`) was never at fault or even reached.
- **Alternatives**: `allow_methods=["*"]` (wildcard everything the app
  will ever need, present or future); route around CORS by proxying
  `/api` through the Next.js dev server instead of calling `localhost:8000`
  directly.
- **Trade-off**: A wildcard never has this class of bug again, but also
  silently allows any future method (e.g. `DELETE`) without a deliberate
  decision to expose it — listing methods explicitly means adding one is
  a visible, one-line change each time, matching how `POST` was added
  explicitly in Prompt 1.1. Proxying through Next.js would remove the
  cross-origin request (and this whole bug class) entirely, but is a
  bigger architectural change than this fix warrants right now.
- **Outcome**: `allow_methods=["GET", "POST", "PATCH"]`. Verified live
  (preflight now returns `200` with `PATCH` in
  `Access-Control-Allow-Methods`; a real browser "Save profile" now
  completes and persists across reload) and by
  `tests/test_cors.py` (`test_preflight_allows_patch_on_the_profile_endpoint`,
  plus non-regression/negative coverage for the existing auth `POST`
  preflight and an unconfigured origin).

- **Date**: 2026-08-23
- **Decision**: For resume upload (Prompt 2.1): a `resumes` table with
  its own `id` (unlike `profiles`, addressed by the owner's `user_id`,
  a resume is 1-of-many so it needs its own identity); a small
  `ResumeStorage` interface (`save`/`read`/`delete`, `app/storage/`)
  with a `LocalResumeStorage` implementation writing under a configured
  directory, keyed by an opaque, server-generated `storage_key`
  (`{user_id}/{resume_id}.{ext}`) that is never returned in any API
  response; and reject-before-persist validation — extension,
  declared-Content-Type-matches-extension, real file-signature ("magic
  bytes") check, size, and filename safety all run before anything is
  written to the database or disk, so a rejected upload leaves no row
  and no file behind.
- **Problem**: A file upload endpoint needs to (a) never trust a
  client's filename or Content-Type as ground truth for what a file
  actually is, (b) never leak where a file physically lives, since a
  path is both an internal implementation detail and, for local
  storage, a potential traversal/access surface, and (c) not hard-wire
  "local disk" into the resume domain code, since project-brief calls
  for S3-compatible storage in production.
- **Alternatives**: (1) Trust the browser's declared `Content-Type` and
  the filename extension alone — rejected: both are client-supplied and
  trivially spoofable (renaming `evil.exe` to `resume.pdf` satisfies
  both checks; only reading the file's actual first bytes catches it).
  (2) Store the file at a path derived from the original filename —
  rejected: makes path-traversal a live concern (`../../etc/passwd`)
  and collisions likely (two users, or two uploads, both naming a file
  `resume.pdf`). (3) Call `open()`/`Path.write_bytes()` directly from
  the route instead of a storage interface — simpler today, but bakes
  "local filesystem" into the route/domain code that Prompt 7.x's S3
  migration would then have to rewrite instead of swap.
- **Trade-off**: The storage interface is a small amount of extra
  indirection (a Protocol, one implementation, one dependency-injected
  factory) for code that only has one backend today — justified because
  the project brief explicitly commits to a second (S3-compatible)
  backend later, and FastAPI's `Depends()` makes the indirection nearly
  free (tests override it exactly like `get_db`, pointing at a temp
  directory instead of a real one). Reject-before-persist is simpler
  than a "store with a failed status" model, at the cost of not
  keeping any record that a rejected upload was ever attempted — judged
  fine, since an invalid upload was never really "the user's resume" to
  begin with.
- **Outcome**: `resumes` table (migration `5c4a2b275fdb`),
  `app/storage/{base,local}.py`, `POST`/`GET`/`GET
  {id}`/`DELETE {id}` under `/api/v1/resumes`, all requiring
  `get_current_user` and (for the two id-addressed routes) an explicit
  `resume.user_id == current_user.id` check — 404 if the id doesn't
  exist, 403 if it exists but isn't the caller's. `allow_methods` in
  `app/main.py` gained `"DELETE"` up front this time, applying the
  lesson from the Prompt 1.3 CORS bug before it could repeat. Covered
  by `tests/test_resume.py` (including
  `test_cannot_get_another_users_resume` and
  `test_cannot_delete_another_users_resume`) and
  `tests/test_local_storage.py`; verified live via a real browser
  upload → list → delete cycle with fictional sample PDF/DOCX files.

- **Date**: 2026-08-23
- **Decision**: Remove the explicit `router.push("/login")` from
  `DashboardPage`'s `handleLogout` (`apps/web/src/app/dashboard/page.tsx`);
  the existing `useEffect` that redirects whenever `status ===
  "unauthenticated"` is now the sole place that navigates to `/login`.
- **Problem**: Found live in the real browser (not caught by the
  existing mocked-router unit tests): logging out fired **two**
  separate client-side navigations to `/login` — one from
  `handleLogout`'s own `router.push`, one from the protective effect
  reacting to `logout()` setting `status` to `"unauthenticated"`.
  Confirmed at the network level (two back-to-back, identical `GET
  /login?_rsc=...` requests per logout) on every repro attempt. Two
  overlapping App Router client transitions to the same href is exactly
  the kind of race that can leave the router's pending-navigation state
  stuck — reported symptom: the page visually stuck on the dev
  "Rendering…" indicator and non-interactive until a hard refresh,
  intermittent because it depends on how much the two transitions
  overlap (worse under dev-mode HMR/compile latency).
- **Alternatives**: (1) Guard against the double push with a ref/flag
  (e.g. `hasNavigatedRef`) — works, but treats the symptom rather than
  the cause, and leaves two competing "authorities" deciding when to
  redirect. (2) Keep the explicit push in `handleLogout` and remove the
  effect instead — rejected: the effect is also what protects
  `/dashboard` when a mount-time silent refresh finds no session at
  all, a case `handleLogout` never covers.
- **Trade-off**: None of substance — the effect already unconditionally
  handles every case that causes `status` to become `"unauthenticated"`,
  logout included, so removing the redundant call is strictly a
  simplification, not a behavior change from the user's perspective.
- **Outcome**: One redirect authority. Verified live, twice: exactly one
  `GET /login?_rsc=...` per logout, page interactive immediately after
  in both runs. `tests/dashboard/page.test.tsx`'s logout test
  strengthened to assert `pushMock` was called `toHaveBeenCalledTimes(1)`
  — confirmed this fails (`2 times`) against the old code and passes
  against the fix. Noted gap: every navigation test in this repo mocks
  `next/navigation`'s `useRouter` entirely, so no unit test exercises
  the real App Router transition machinery — this class of bug can only
  be caught by a call-count assertion (as added) or live browser
  verification, not by the mock alone.

- **Date**: 2026-08-23
- **Decision**: Add a `supersededRef` to `AuthProvider` (`auth-context.tsx`):
  set to `true` the instant an explicit `login()` starts, checked by the
  mount-time silent-refresh effect's `.then`/`.catch` before either
  applies its result. Once set, it's never reset.
- **Problem**: Found live (not by any existing test — this repo had no
  `auth-context` test at all): the mount-time `refresh()` effect and a
  manual `login()` are two independent flows that both write
  `status`/`user`/`accessToken` with no sequencing. If `refresh()`
  (started on page load, restoring a session from the HttpOnly cookie)
  was still pending or hadn't even fired yet when the user submitted the
  login form, and it settled *after* login already succeeded, its
  unconditional `setStatus("unauthenticated")` silently reverted a real,
  more recent session — no error, no console warning, just a reverted
  `status` that then bounced `/dashboard`'s protective effect back to
  `/login`, leaving the "Signing in…" button and the router's pending
  transition both stuck. Directly proved live by patching `fetch` to
  delay `/auth/refresh` and confirming `login()` could complete (200,
  200, `/dashboard` rendered) while the delayed `refresh()` was still
  unsettled — i.e., two independent writers to the same state, live,
  not just in theory.
- **Alternatives**: (1) A generation/version counter incremented on every
  auth-changing operation, checked by every writer — more general, but
  overkill for the one actual conflict (mount-refresh vs. login);
  nothing else currently races. (2) Cancel the mount effect's promise
  chain outright once `login()` starts (e.g. via `AbortController`) —
  more "correct" in the sense of not doing unnecessary work, but the
  network request has typically already been sent by the time a user
  can react, so aborting saves nothing and adds a fetch-abort-handling
  path for no behavioral difference from just ignoring the result.
- **Trade-off**: The ref never resets after first use, which is safe
  only because the mount effect's promise settles exactly once per
  `AuthProvider` mount (verified across: initial refresh, explicit
  login, logout, a later login on the same still-mounted provider, and
  a full page reload/remount, which creates a fresh ref via a fresh
  component instance) — a design that would break if the mount effect
  were ever changed to run more than once per mount.
- **Outcome**: `apps/web/src/lib/auth-context.tsx`. New
  `apps/web/src/lib/auth-context.test.tsx`, using a manually-sequenced
  promise (same technique as `api-client.test.ts`'s `refresh()` dedup
  test) to deterministically prove the fix — confirmed to fail against
  the old code (`status: unauthenticated`) and pass against the fix.
  Live-verified twice more: a normal login, and the same delayed-refresh
  race that previously exposed the bug, both ending on `/dashboard`
  without reverting.

- **Date**: 2026-08-23
- **Decision**: For resume text extraction (Prompt 2.2): Celery
  (broker: the already-provisioned Redis, no result backend — Postgres,
  the `resumes` row itself, is the only source of truth for job state),
  `pypdf`/`python-docx` for deterministic extraction, and `resumes.status`
  renamed `uploaded/processing/completed/failed` ->
  `queued/processing/succeeded/failed` to match this prompt's own
  wording. Duplicate/concurrent processing is prevented by an atomic
  claim (`UPDATE resumes SET status='processing' WHERE status='queued'`)
  rather than any queue-level dedup feature; retries distinguish
  transient failures (a storage read error — worth retrying) from
  permanent ones (a parse failure, or no extractable text — never
  worth retrying, since the same bytes fail the same way every time).
- **Problem**: Needed an async worker for a slow, request-blocking
  operation (parsing an uploaded file), using infrastructure this
  project already provisions, with job state a client can poll for,
  safe (non-leaking) error messages, and correctness under duplicate/
  concurrent triggering.
- **Alternatives**: (1) `arq` (an asyncio-native Redis queue) instead
  of Celery — would have avoided the sync/async bridge entirely
  (`asyncio.run(...)` inside a sync Celery task calling back into async
  SQLAlchemy), and was seriously considered — but `docs/project-brief.md`
  names Celery specifically ("Redis and Celery only for processing that
  should not block a request"), not just "a queue", so this follows
  that rather than substituting a technically-tidier personal
  preference. (2) A Celery result backend (also Redis) as the source of
  truth for job state instead of a Postgres column — rejected: it would
  create two places extraction state could live and disagree, for no
  benefit here (nothing ever calls `.get()` on a Celery `AsyncResult`).
  (3) arq's built-in `_job_id` dedup for duplicate prevention — not
  applicable to Celery, and the atomic claim guards the thing that
  actually matters (concurrent *execution*) regardless of what caused a
  duplicate trigger, not just duplicate enqueueing.
- **Trade-off**: Celery's task model is synchronous; bridging into
  async SQLAlchemy/`ResumeStorage` per task execution (via
  `asyncio.run`) is the accepted cost of using the framework the brief
  specifies rather than the one that would have fit this codebase's
  existing async-everywhere style most naturally. Internal retry state
  (`attempt_count`) mirrors Celery's own `self.request.retries` rather
  than being hand-maintained, keeping "how many times has this run" a
  single source of truth. `task_eager_propagates` is deliberately left
  off in tests — enabling it makes Celery's own internal retry
  (`self.retry()`) propagate as an exception instead of being handled,
  discovered by a real, reproducible test failure, not by reading docs
  closely enough the first time.
- **Outcome**: `apps/api/app/worker.py` (Celery app, task, atomic
  claim, `enqueue_extraction`), `apps/api/app/extraction.py` (pure
  pypdf/python-docx functions), new `resumes` columns
  (`extracted_text`, `error_message`, `attempt_count`, `processed_at`;
  migration `266984262a64`, which also data-migrates existing rows'
  `status` values to the new vocabulary). `GET /api/v1/resumes` and
  `GET /api/v1/resumes/{id}` (Prompt 2.1) serve as the status endpoint
  as-is — no new route. Dashboard polls every 2s while any resume is
  queued/processing. Covered by `tests/test_extraction.py` (successful
  PDF/DOCX, malformed document, transient-failure retry-then-succeed,
  retries-exhausted safe failure, duplicate-processing prevention,
  idempotent re-run) via Celery's `task_always_eager` mode — no real
  broker/worker/Redis connection needed for any of it.

- **Date**: 2026-08-23
- **Decision**: Reconcile the resumes that Prompt 2.2's migration left
  stranded in `queued` with a one-off, manually-run backfill script
  (`apps/api/scripts/requeue_stuck_resumes.py`, `make
  requeue-stuck-resumes`) rather than any automatic mechanism. The
  migration (`266984262a64`) renames existing Prompt 2.1 rows
  `uploaded -> queued` so legacy and new rows share one vocabulary and
  a never-processed resume looks the same however it got there — but
  that rename is pure SQL and deliberately does *not* enqueue Celery
  jobs: a migration must stay applicable without Redis/Celery being
  reachable (CI, a fresh environment, or any `alembic upgrade head` run
  before a broker exists), and coupling schema changes to a live broker
  would make migrations fail for reasons that have nothing to do with
  the schema. The consequence is a real gap — those rows have no Celery
  message and `enqueue_extraction` is only ever called from the upload
  endpoint — which the script closes by querying `status = 'queued'`
  and calling that same existing `enqueue_extraction`, duplicating no
  task logic.
- **Problem**: Legacy Prompt 2.1 resumes became `queued` with nothing
  to process them, and would have stayed stuck there permanently: there
  is no periodic sweep, no worker-startup hook, and no manual retry
  endpoint.
- **Alternatives**: (1) Enqueue from inside the migration — rejected
  for the broker-coupling reason above. (2) A `worker_ready` signal
  sweep on Celery worker startup — genuinely considered, and it would
  also self-heal the separate best-effort-enqueue gap, but it adds
  permanent runtime behavior (and, per Celery's `Consumer.start()`
  reconnect loop, re-fires on every broker reconnect, not just once per
  process) to fix what is a one-time historical data gap. (3) Celery
  Beat periodic reconciliation — a whole extra process to run and
  supervise, far past what this gap warrants.
- **Trade-off**: The fix is manual, so it only helps if someone
  actually runs it after migrating — accepted, because the gap is
  one-time and this project is local-only. The still-open, separately
  documented gap is that `enqueue_extraction` is best-effort at upload
  time (a Redis outage silently leaves a new resume `queued`); this
  script also fixes those rows when run, but nothing runs it
  automatically, so that remains a known limitation rather than a
  solved problem. The script is a sync entry point doing async DB work
  via `asyncio.run`, mirroring `app/worker.py`'s own split — enqueueing
  is synchronous broker I/O and belongs outside the event loop (this
  also happens to be what makes it testable under `task_always_eager`,
  where an enqueue inside a running loop would hit "asyncio.run()
  cannot be called from a running event loop").
- **Outcome**: The script is safe to rerun by construction, and needs
  no dedup logic of its own: it is read-only against `resumes` (it
  never changes status — the only transition out of `queued` stays the
  worker's atomic `UPDATE ... WHERE status = 'queued'` claim in
  `_claim_resume`), and its `status = 'queued'` filter already skips
  anything processing or finished. If the same resume is enqueued twice
  anyway — a rerun racing a still-unconsumed message, or two operators
  — Postgres serializes the two claim UPDATEs and only the first
  matches a row; the second no-ops without writing. So a duplicate
  costs one wasted task execution, never double processing. Covered by
  `tests/test_requeue_stuck_resumes.py` (migrated PDF/DOCX row
  re-enqueued and processed, rerun is a no-op, redundant in-flight
  enqueue is harmless, processing/succeeded/failed rows skipped,
  mixed-set filtering, and malformed/transient-retry/retries-exhausted
  behavior all still intact through this path).

- **Date**: 2026-08-23
- **Decision**: For the skill taxonomy and evidence schema (Prompt 2.3):
  EXTEND the existing `skills` table (Prompt 1.3, migration
  `8156d76f48cc`) with a nullable `category` plus new `skill_aliases`
  and `skill_relations` tables — rather than adding a second "canonical
  skills" table — and add `candidate_skills` (one row per
  candidate/skill, `UNIQUE(user_id, skill_id)`) and `skill_evidence`
  (hanging off `candidate_skills`). Aliases are a normalized table with
  a **globally** unique `alias_slug`; relations are an untyped,
  self-referential M:N with both directions materialized. The taxonomy
  is seeded by a repeatable command (`make seed-skills`) from a typed
  Python file, never by the migration.
- **Problem**: Needed somewhere to record which candidate has which
  skill and, per docs/project-brief.md's Evidence-First rule, why we
  believe it — with enough structure for Prompt 2.4's deterministic
  extraction to write into, without pre-building scoring or matching.
- **Alternatives**: (1) A new `canonical_skills` table alongside
  `skills` — rejected outright: `skills` is already the canonical
  vocabulary and is already referenced by `profile_target_skills`, so a
  second table would fork skill identity and break this file's own
  Prompt 1.3 commitment that "Python" and "python" resolve to one row
  across the whole system. Extending also means a skill a user already
  coined gets *adopted* by the seed rather than duplicated. (2) Aliases
  as a JSON/array column on `skills` — rejected: the entire purpose of
  an alias is indexed reverse lookup ("js" -> JavaScript), and JSON
  cannot enforce the guarantee that actually matters, that one alias
  resolves to exactly one skill. (3) A `relation_type` column on
  relations (prerequisite/parent/substitute) — deliberately omitted;
  see the trade-off below. (4) Denormalizing `user_id` onto
  `skill_evidence` — rejected: a copy can drift from its parent, and
  the join back through `candidate_skills` is cheap.
- **Trade-off**: **Relations are untyped and mean only "these commonly
  appear together".** They must never be read as prerequisite,
  parent/child, substitute, dependency, or hierarchy — Python <->
  Django asserts association, not "Django requires Python". Encoding
  direction would need a typed vocabulary and is an additive migration
  when a feature actually needs it. **Evidence's natural key is
  `(candidate_skill_id, source_type, source_identifier,
  extraction_method)`**, so there is one representative excerpt per
  candidate-skill/source/method: five mentions of "Python" in one
  resume produce one evidence row, not five. That is sufficient for the
  MVP because the excerpt exists to let a human verify the claim, and
  one clear quotation does that as well as five near-identical ones,
  while every question the MVP asks ("which sources support this?",
  "how confident?", "show me why") is answered per source rather than
  per mention; counting mentions is scoring, which belongs to Prompt
  4.x. Wanting multiple excerpts per source later is a contained,
  additive change: add an `excerpt_hash` (or `occurrence_index`) column
  and extend that unique constraint to include it — no change to
  ownership, cascades, or any other table. `source_identifier` is a
  polymorphic string, not an FK (no single FK can span resume/github/
  manual), so deleting a resume leaves evidence citing an id that no
  longer resolves — accepted for now, since nothing in 2.3 writes
  evidence. `source_type`/`extraction_method` get no database CHECK,
  matching `resumes.status`'s reasoning that a vocabulary should grow
  without a migration; `confidence` DOES get one, because a numeric
  range is a permanent invariant rather than a vocabulary.
- **Outcome**: Migration `610680fe7d6a` (schema only — no rows, for the
  same reason `266984262a64` did not enqueue Celery jobs). Taxonomy is
  33 skills across 7 categories in `app/seeds/skill_taxonomy.py`, a
  typed Python module rather than JSON so it is mypy-checked and can
  carry the alias-quality rule in comments: an alias must be an
  unambiguous technical identifier, never a common English word —
  "next", "rest" and "node" were rejected on that basis to avoid false
  matches in Prompt 2.4. `scripts/seed_skills.py` validates the file
  (no duplicate skills, no alias shadowing a canonical slug, no alias
  owned by two skills, no self- or dangling relations) before touching
  the database, then upserts by slug and reconciles aliases/relations
  by difference in one transaction — so a re-run writes nothing at all
  (verified: `updated_at <> created_at` matches zero rows after three
  runs) and skills absent from the file are never touched. Covered by
  `tests/test_skill_taxonomy.py` (constraints, cascades, RESTRICT,
  confidence bounds, multi-source evidence) and
  `tests/test_seed_skills.py` (idempotency asserted on ids and
  timestamps, adoption of a user-coined row, off-taxonomy survival,
  repair of hand-edits, alias/relation reconciliation, and an
  integration test proving a profile `PATCH` of "python" reuses the
  seeded row instead of coining a second).

<!-- Add new entries above this line, most recent first. -->
