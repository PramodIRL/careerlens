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

Health check: http://localhost:8000/api/v1/health

### 3. Run the web app

```bash
cd apps/web
npm install
npm run dev
```

Open http://localhost:3000 — the home page shows whether it can reach the API
health endpoint. If your API runs on a non-default URL, create
`apps/web/.env.local` with `NEXT_PUBLIC_API_URL=<url>`.

## Tests

```bash
cd apps/api
uv run pytest
```

## Checks

```bash
cd apps/api && uv run ruff check .
cd apps/web && npm run lint && npx tsc --noEmit
```
