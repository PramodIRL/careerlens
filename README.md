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

## Migrations

Schema changes are tracked with [Alembic](https://alembic.sqlalchemy.org/).
The only migration so far enables the `pgvector` Postgres extension — no
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
make start      # Postgres + Redis via Docker Compose
make start-web  # Next.js dev server (separate terminal)
make start-api  # FastAPI dev server (separate terminal)
make stop       # stop the Docker Compose data services
```
