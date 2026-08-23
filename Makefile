.PHONY: help format format-check lint typecheck test start start-web start-api start-worker services-up stop services-down migrate migration migrate-status requeue-stuck-resumes seed-skills smoke

help:
	@echo "CareerLens — available commands:"
	@echo "  make format         Auto-format web (Prettier) and API (Ruff)"
	@echo "  make format-check   Check formatting without writing changes"
	@echo "  make lint           Lint web (ESLint) and API (Ruff)"
	@echo "  make typecheck      Typecheck web (tsc) and API (mypy)"
	@echo "  make test           Run web (Vitest) and API (pytest) tests"
	@echo "  make start          Start Postgres + Redis via Docker Compose"
	@echo "  make start-web      Run the Next.js dev server (host, foreground)"
	@echo "  make start-api      Run the FastAPI dev server (host, foreground)"
	@echo "  make start-worker   Run the Celery resume-extraction worker (host, foreground)"
	@echo "  make stop           Stop the Docker Compose data services"
	@echo "  make migrate        Apply all pending Alembic migrations"
	@echo "  make migrate-status Show the current Alembic migration"
	@echo "  make migration name=\"...\"  Create a new (blank) migration"
	@echo "  make requeue-stuck-resumes  Re-enqueue resumes stuck in 'queued' (safe to re-run)"
	@echo "  make seed-skills    Seed/update the canonical skill taxonomy (safe to re-run)"
	@echo "  make smoke          Run the end-to-end developer smoke test"

format:
	cd apps/web && npm run format
	cd apps/api && uv run ruff format .

format-check:
	cd apps/web && npm run format:check
	cd apps/api && uv run ruff format --check .

lint:
	cd apps/web && npm run lint
	cd apps/api && uv run ruff check .

typecheck:
	cd apps/web && npm run typecheck
	cd apps/api && uv run mypy app scripts

test:
	cd apps/web && npm run test
	cd apps/api && uv run pytest

# "start" brings up the data services only. The web and API apps run on the
# host directly (not in Docker) for easier debugging — start them with
# `make start-web` / `make start-api` / `make start-worker` in separate
# terminals.
start: services-up
	@echo "Postgres and Redis are up. In separate terminals, run:"
	@echo "  make start-web"
	@echo "  make start-api"
	@echo "  make start-worker  (only needed to process resume text extraction)"

start-web:
	cd apps/web && npm run dev

start-api:
	cd apps/api && uv run uvicorn app.main:app --reload --port 8000

# Processes resume text-extraction jobs (Prompt 2.2) — without this
# running, uploaded resumes stay "queued" forever; nothing else in the
# app blocks on it. See apps/api/app/worker.py.
start-worker:
	cd apps/api && uv run celery -A app.worker worker --loglevel=info

services-up:
	docker compose up -d

stop: services-down

services-down:
	docker compose down

migrate:
	cd apps/api && uv run alembic upgrade head

migrate-status:
	cd apps/api && uv run alembic current

migration:
	cd apps/api && uv run alembic revision -m "$(name)"

# One-off reconciliation for the Prompt 2.1 -> 2.2 migration gap: any
# resume still in status "queued" (most likely a legacy row the
# migration renamed from "uploaded", or an upload whose enqueue silently
# failed) is re-enqueued through the normal extraction path. Safe to run
# more than once — see apps/api/scripts/requeue_stuck_resumes.py.
requeue-stuck-resumes:
	cd apps/api && uv run python -m scripts.requeue_stuck_resumes

# Seeds (or re-seeds) the canonical skill taxonomy from
# apps/api/app/seeds/skill_taxonomy.py. Safe and deterministic to run
# repeatedly — edit the seed file, re-run this. Skills coined by users
# are never touched. See apps/api/scripts/seed_skills.py.
seed-skills:
	cd apps/api && uv run python -m scripts.seed_skills

smoke:
	./scripts/smoke.sh
