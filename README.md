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

That writes three invented resumes (`example.com` addresses, `555-01xx`
numbers, made-up employers) to `apps/api/var/samples/`. It writes **files
only** — no users, no database rows — so the demo goes through the real
upload → extraction → review path. `apps/api/var/` is gitignored, so a
generated document cannot be committed.

`docs/demo.md` is the full walkthrough: what to click, what to point out,
and what the system deliberately refuses to infer.

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
