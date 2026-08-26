# CareerLens

CareerLens is a responsive web app that helps early-career software candidates
turn a resume and GitHub activity into evidence-backed skills, explainable job
matches, skill gaps, and a learning roadmap. See
[`docs/project-brief.md`](docs/project-brief.md) for the full product
definition and [`CLAUDE.md`](CLAUDE.md) for the working rules followed in this
repository.

## Repository layout

- **`apps/web/`** — Next.js (App Router, TypeScript strict, Tailwind)
  frontend. Runs directly on the host during development.
- **`apps/api/`** — FastAPI backend (Python, type-hinted). Runs directly on
  the host during development.
- **`infra/`** — Infrastructure support files, e.g. the Postgres init script
  that enables the `pgvector` extension for the Docker Compose data services.
- **`docs/`** — Project documentation:
  [`project-brief.md`](docs/project-brief.md) (product/architecture) and
  [`decisions.md`](docs/decisions.md) (decision log).
- **`docker-compose.yml`** — Starts only the *data* services (PostgreSQL with
  pgvector, Redis) for local development. The web and API apps run on the
  host directly, not in Docker, for easier debugging.
- **`.env.example`** — Names of environment variables used across the stack.
  Copy to `.env` and fill in local values; never commit real secrets.
- **`Makefile`** — Shortcuts for formatting, linting, typechecking, testing,
  and starting/stopping services. Run `make help` to list them.
- **`.github/workflows/`** — CI: `web-ci.yml` and `api-ci.yml` each run only
  when files under their app change, checking format, lint, typecheck, and
  tests. No credentials or deployment steps.
- **`scripts/smoke.sh`** — End-to-end developer smoke test: starts data
  services, migrates, starts the API and web app, verifies them, shuts
  everything down. Run via `make smoke`.

## First run

### 1. Start data services

```bash
cp .env.example .env
docker compose up -d
```

This starts PostgreSQL (with the `vector` extension enabled) on
`localhost:5432` and Redis on `localhost:6379`.

### 2. Run the API

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
cd apps/api
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

- Liveness: http://localhost:8000/api/v1/health — the API process is up.
- Readiness: http://localhost:8000/api/v1/health/db — the database is
  reachable. Returns `200 {"status": "ok"}`, or `503
  {"status": "degraded", "detail": "database unavailable"}` if not (never
  including connection details or credentials in the response).

### 3. Run the web app

```bash
cd apps/web
npm install
npm run dev
```

Open http://localhost:3000 — the home page shows whether it can reach the API
health endpoint. If your API runs on a non-default URL, create
`apps/web/.env.local` with `NEXT_PUBLIC_API_URL=<url>`.

## Smoke test

`make smoke` (or `./scripts/smoke.sh`) proves the whole foundation works
with one command: it starts the Docker data services, applies migrations,
starts the API and web app, checks the API's liveness and readiness
endpoints, checks the API's CORS policy would actually let the web page's
browser reach it, checks the web page serves the expected health-check
markup — then shuts everything back down, whether it passed or failed.

```bash
make smoke
# or, to use different ports if the defaults are busy:
API_PORT=8001 WEB_PORT=3001 ./scripts/smoke.sh
```

**What it does *not* prove:** the script never executes JavaScript, so it
can't see the web page's fetch actually resolve to "API is healthy" the way
a real browser would — and by the time it prints its summary, it's already
shutting the servers back down, so there's nothing left running to look at
anyway. That final visual confirmation is a separate, manual step:

1. `make start` (data services)
2. `make start-api` (separate terminal)
3. `make start-web` (separate terminal)
4. Open http://localhost:3000 and confirm it shows **"API is healthy"**

Unlike the smoke test, these stay running until you stop them yourself
(`make stop`, then `Ctrl-C` the other two).

## Demo

To show the resume-to-skills flow, generate the fictional sample resumes
first — never demo with a real resume, your own included, since an upload
stores the file on disk and its full extracted text in the database:

```bash
make sample-resumes
```

That writes four invented resumes (`example.com` addresses, `555-01xx`
numbers, made-up employers) to `apps/api/var/samples/`. It writes **files
only** — no users, no database rows — so the demo goes through the real
upload → extraction → review path. `apps/api/var/` is gitignored, so a
generated document cannot be committed.

`docs/demo.md` is the full walkthrough: what to click, what to point out,
and what the system deliberately refuses to infer.

`docs/phase-4-acceptance.md` is the whole-MVP walkthrough — resume,
GitHub, saved jobs, match, gaps and the mutations that update them — with
the score worked out by hand so a viewer can check it, plus the Phase 4
release checklist. Its GitHub half needs no real account:

```bash
make demo-github EMAIL=ada.sample@example.com
```

That imports a fictional account (`apps/api/scripts/sample_github.py`)
through the real ingestion pipeline, making no network request. The user
must already exist — the script will not create one.

## Eligibility

Postings state two different kinds of requirement, and CareerLens keeps them
apart. Skills go to `skill_match_v1` / `skill_gap_v1`; academic and
experience bars — CGPA, Class 10/12 marks, degree, field of study,
graduation year, years of experience — go to a separate `eligibility_v1`
result that is **never folded into the skill score**.

```
GET /api/v1/saved-jobs/{id}/eligibility-requirements   what the posting asks
GET /api/v1/saved-jobs/{id}/eligibility                how you compare
GET|PATCH /api/v1/qualifications                       your own facts
DELETE   /api/v1/qualifications/{fact}                 "that one isn't mine"
```

**One profile, every job.** Your qualifications are entered once on the
dashboard and compared against every saved job's entry requirements — there
is no qualification form inside a saved job, and no per-job copy of the data.
Editing the profile updates every job's eligibility straight away.

**Only what you have asserted counts.** Eligibility is computed from values
you have actually confirmed; anything blank reads as *unknown*, never as a
failure. A saved job whose requirements you have not answered says
"Qualification profile not set up" and links to the form, rather than
implying you fall short.

Resume upload does **not** populate qualifications — it extracts skills and
evidence, as before. The deterministic qualification extractor remains in the
codebase, dormant and tested, for a future explicit "import from resume".

Extraction is deterministic regex over a curated vocabulary — no LLM, no
embeddings. Ambiguous prose produces no requirement rather than a guess.

Four states, and two of them are not failures:

| State | Meaning |
|---|---|
| `satisfied` | your value clears the bar |
| `not_satisfied` | your value does not — the only state that blocks you |
| `unknown` | you have not declared this yet |
| `undetermined` | the posting cannot be evaluated as written |

**A blank field never counts against you.** An undeclared CGPA is `unknown`,
not a failure — the system supplies a missing *scale*, never a missing value.

For the scale itself, an explicit one always wins: `"CGPA: 8.2/10"` reads as
10 and `"3.6/4.0"` as 4. Where a posting writes no scale at all
(`"minimum CGPA 7.5"`), it is stored as **/10** — the default is written onto
the row and shown in the response, so you can see what was assumed. Say so on
your profile if yours is out of 4.

## GitHub connection

A candidate can connect a **public** GitHub username, under
`/api/v1/github-connection`:

| Endpoint | Purpose |
|---|---|
| `GET` | The caller's connection, or `null`. "Not connected" is a normal state, not a 404. |
| `PUT` | Verify a public username against GitHub and store it. `201` when newly connected, `200` when it replaced an existing connection. |
| `DELETE` | Disconnect. Idempotent — `204` whether or not there was a connection. |

**No credential is involved anywhere in this flow.** CareerLens never
asks for a GitHub password, never issues or stores an OAuth token or
personal access token, and never requests access to a private
repository. The API reads one unauthenticated public endpoint
(`GET /users/{username}`) and stores four facts: GitHub's numeric
account id, the canonical username, the public repository count, and
when it was last verified. The request body accepts `username` and
nothing else — a request carrying any extra field is rejected with a
422 rather than having it quietly ignored.

The numeric id is stored because a GitHub username can be renamed and
later reused by someone else; keying later ingestion on the string alone
would eventually point at a stranger's repositories. There is
deliberately **no** unique constraint on that id across users: this flow
verifies that an account exists, never that the caller owns it, so a
global unique would let whoever connects a username first lock out its
real owner. See `docs/decisions.md`.

Upstream failures each map to one status and one message a user can act
on: `404` no such public account, `422` unusable username or an
organization account, `503` GitHub unavailable or rate-limited (with
`Retry-After` when GitHub said when its window resets), `504` timeout.
No upstream status code, response body or exception text is ever
surfaced.

Repository ingestion, GitHub-derived skills and evidence are **not**
part of this — see the connection as the prerequisite for that work,
not the start of it.

## GitHub ingestion

Once an account is connected, a user can import their public
repositories, under the same prefix:

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/github-connection/ingestions` | Queue an import. `202` accepted; `409` if one is already running or no account is connected. |
| `GET /api/v1/github-connection/ingestions/latest` | The most recent run and its progress, or `null`. |
| `GET /api/v1/github-connection/repositories` | What was imported. |

The Celery worker (`make start-worker` — the same one that extracts
resume text) does the work: it refreshes the profile, walks every page
of the public repository listing, then fetches languages and the README
for the most recently pushed repositories.

**The 60-requests-per-hour budget shapes the design.** GitHub allows 60
unauthenticated requests per hour per IP, and each repository costs two
of them, so an import fetches full detail for at most
`GITHUB_MAX_REPOSITORIES` (default 20) repositories, most recently
pushed first, skipping forks. Every listed repository still gets its
basic details — that data is already in the listing response and costs
nothing extra. The UI reports all three numbers separately, so an
account with 47 public repositories is never told it has 20.

Reruns are idempotent. Repositories are keyed on GitHub's **numeric
repository id**, never on `owner/repo`, so a rename updates the existing
row instead of creating a duplicate. Languages and topics are reconciled
by difference, and a README whose SHA is unchanged is not rewritten.

Two failure rules are worth knowing:

- **Rate limiting pauses an import; it never counts as a failed
  repository.** The run stays `processing`, everything already imported
  stays imported, and the worker retries from GitHub's own reset time.
- **Repositories are only marked deleted from a complete listing.** If
  pagination stopped early, the repositories we did not see are unknown,
  not absent — reconciling then would let one timeout erase a user's
  history. Deletion is soft; the row is kept.

**Only the README is retained as a raw source snapshot** (truncated
text + SHA + original byte size + a truncation flag), because a later
prompt must quote it verbatim as evidence and a hash cannot be quoted.
Raw GitHub JSON payloads are deliberately **not** stored anywhere.

Disconnecting a GitHub account deletes the imported repositories and
runs along with the connection.

Ingestion writes **no** skills and **no** evidence — that is a later
prompt's work.

## Authentication

Email/password auth, under `/api/v1/auth`:

| Endpoint | Purpose |
|---|---|
| `POST /register` | Create an account. Duplicate email → `409`. |
| `POST /login` | Sets the refresh token as an `HttpOnly` cookie; returns `{access_token, token_type, expires_in}` in the body — never the raw refresh token. Wrong email *or* wrong password → identical generic `401`. |
| `POST /refresh` | Reads the refresh token from the cookie (no request body), rotates it (new cookie, old one revoked). Reusing an already-used refresh token revokes the *entire* session as a compromise signal. |
| `POST /logout` | Revokes and clears the refresh cookie. Idempotent — safe to call with no session. |
| `GET /me` | Protected route — requires `Authorization: Bearer <access_token>`. |

The refresh-token cookie is `HttpOnly` (invisible to JavaScript — the
whole point), `Secure`, `SameSite=Lax`, scoped to `/api/v1/auth`. The
access token lives in memory only in the web app, never `localStorage`
or a cookie. See `docs/decisions.md` for the full design rationale.

Register/login/refresh share an in-memory rate limit (10 requests/60s per
client IP by default) — see `app/rate_limit.py` for why this is
single-process only, not yet suitable for a multi-instance deployment.

Passwords are hashed with bcrypt; refresh tokens are stored only as a
SHA-256 hash of the random value handed to the client — see
`app/security.py` for why neither is ever stored as-is.

### Web pages

`/register`, `/login`, and the protected `/dashboard` (redirects to
`/login` if there's no valid session) live in `apps/web/src/app/`.
`src/lib/auth-context.tsx` holds the in-memory session and silently
retries a refresh once on mount, so a page reload doesn't look
logged-out as long as the cookie is still valid.

## Migrations

Schema changes are tracked with [Alembic](https://alembic.sqlalchemy.org/).
Two migrations exist so far: enabling the `pgvector` Postgres extension,
and creating the `users` / `refresh_tokens` tables (Prompt 1.1) — no other
product tables exist yet.

```bash
make migrate                    # apply all pending migrations
make migrate-status             # show the currently applied migration
make migration name="add x"     # create a new, empty migration to fill in
```

Or directly: `cd apps/api && uv run alembic upgrade head` (etc.). Migrations
read the database URL from `app/settings.py` (environment variables / the
repo-root `.env`), never from a hard-coded connection string in
`alembic.ini`.

## Quality gates

All commands work per-app or via the root `Makefile` (`make help` for the
full list).

| Gate | Web (`apps/web`) | API (`apps/api`) |
|---|---|---|
| Format | `npm run format` / `format:check` | `uv run ruff format .` / `--check` |
| Lint | `npm run lint` | `uv run ruff check .` |
| Typecheck | `npm run typecheck` (tsc) | `uv run mypy app` |
| Test | `npm run test` (Vitest) | `uv run pytest` |

`apps/api` tests need a reachable Postgres (`make start`, or
`docker compose up -d postgres`) — they run against an isolated
`careerlens_test` schema in the same database, created and dropped
automatically by the test suite, so they never touch dev data.

Or, from the repo root:

```bash
make format-check
make lint
make typecheck
make test
```

CI runs the same commands automatically in `.github/workflows/web-ci.yml` and
`api-ci.yml`, scoped to only run when files in the corresponding app change.
Neither workflow uses real credentials or deploys anything.

## Start / stop

```bash
make start        # Postgres + Redis via Docker Compose
make start-web    # Next.js dev server (separate terminal)
make start-api    # FastAPI dev server (separate terminal)
make start-worker # Celery resume-extraction worker (separate terminal;
                   # only needed to actually process uploaded resumes —
                   # everything else works without it)
make stop         # stop the Docker Compose data services
```

## Troubleshooting

**Docker daemon not running / unavailable**
`docker info` (which `scripts/smoke.sh` checks first) fails with something
like `Cannot connect to the Docker daemon`. Open Docker Desktop and wait
for it to report "running," then retry.

**A port is already in use**
`Error: listen EADDRINUSE` (web), `[Errno 48] Address already in use` (API),
or Postgres/Redis failing to start on 5432/6379 usually means another
process already owns that port. Find and stop it, or use a different port:

```bash
lsof -i :3000          # find what's using a port (swap in 5432/6379/8000/etc.)
kill <PID>              # stop it, if that's safe to do
```

Or override the port instead of hunting down the process:
- Postgres/Redis: set `POSTGRES_PORT` / `REDIS_PORT` in `.env` before
  `docker compose up -d`.
- API/web (including the smoke test): set `API_PORT` / `WEB_PORT`, e.g.
  `API_PORT=8001 WEB_PORT=3001 make smoke`.

**A migration fails**
Run `make migrate-status` to see what's currently applied. If Postgres
isn't up yet (`make start` first), migrations will fail to connect —
that's the most common cause.

**`scripts/smoke.sh` seems to hang or leaves things running**
It has a 30–60 second timeout per step and always tears down on exit
(success, failure, or Ctrl-C) via a trap — but if it's ever killed with
`kill -9` (which can't be trapped), clean up manually:

```bash
make stop                        # stop Docker data services
lsof -ti :8000 | xargs kill       # stop a stray API process
lsof -ti :3000 | xargs kill       # stop a stray web process
```
